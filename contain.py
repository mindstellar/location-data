"""Which division a settlement belongs to, and which divisions a country has.

Two halves of the same question. Selecting the divisions decides what the
propagation is even aiming at, and the propagation decides whether the
selection was any good -- a country can pick a perfectly reasonable tier that
nothing can actually reach, which is why the fallbacks in pipeline.py exist.

Everything here reads the flat P131 array. It is never indexed or sorted: the
dump is only nearly QID-ordered, so a binary search over it is quietly wrong,
and a child->parents dict over 15M edges costs more memory than the machine
has. Rescanning until nothing changes converges in the depth of the containment
graph, which is single digits.
"""

from classify import (
    MAX_PROPAGATION_ROUNDS,
    Q_COUNTRY_CODE_DATASET,
    Q_ISO_STANDARD,
    is_excluded_division,
    is_not_a_place,
    is_settlement,
)

# Depth and admin-1 packed into one int so the propagation map stays a plain
# int->int dict; comparing packed values orders by depth first, then by QID.
# A tuple would be correct too and cost several hundred MB more across ~13.5M
# entries.
DEPTH_SCALE = 200_000_000


class CountryPlan:
    """Everything decided about one country before a single settlement is read:
    which divisions it has, which it kept in reserve, and how it got them.

    One object rather than four dicts keyed on ISO2. The dicts had to be held
    in step by hand, and the cost of missing one is on record: the country-level
    region was added to the coarse map and seeded for propagation, but the
    country's own record was never added to the division records, so the region
    could not resolve a name, was dropped in silence, and took every settlement
    filed under it -- twenty of the twenty-one missing capitals -- with it.

    `leaf` are the divisions settlements should attach to. `coarse` are the ones
    the leaf-most rule set aside, seeded further out so they catch only what
    would otherwise reach nothing. `mode` is 'admin1' unless the country has no
    usable tier at all, in which case one synthesised region stands for it.
    """

    __slots__ = ('iso2', 'country_qid', 'leaf', 'coarse', 'mode', 'tier')

    def __init__(self, iso2, country_qid, leaf, coarse, tier):
        self.iso2 = iso2
        self.country_qid = country_qid
        self.leaf = leaf
        self.coarse = coarse
        self.mode = 'admin1'
        self.tier = tier

    def _make_country_resolvable(self, country_records, admin1_candidates):
        """Let the country's own record stand in as a division record.

        Division records hold only P300-bearing items and a country carries
        P297, so without this the region standing for the country has no record
        to resolve a name from. Every caller that makes the country into a
        region needs it, which is exactly why it lives here rather than at the
        call sites.
        """
        if self.country_qid in country_records:
            admin1_candidates.setdefault(self.country_qid,
                                         country_records[self.country_qid])

    def add_country_as_coarse(self, country_records, admin1_candidates):
        """Last resort for a country that does have a division tier: a
        country-level region, for the settlements whose P131 points straight at
        their country with no intermediate division recorded. It becomes a real
        region only if something actually lands in it.
        """
        self.coarse[self.country_qid] = None
        self._make_country_resolvable(country_records, admin1_candidates)

    def use_country_as_region(self, country_records, admin1_candidates):
        """Tier 4: no usable division tier exists, so one synthesised region
        stands for the country and settlements come from its own P17 rather
        than from containment. Reached twice -- when no tier is found at all,
        and when a tier is found but nothing can reach it.
        """
        self.leaf = {self.country_qid: None}
        self.mode = 'country'
        self.tier = 4
        self._make_country_resolvable(country_records, admin1_candidates)


def propagate_containment(p131, seeds):
    """Map every entity that reaches a seed through P131+ to its *nearest*
    seed, measured in P131 hops.

    `seeds` maps a division QID to its packed starting value, so a caller can
    start some divisions one level "further away" than others -- which is how a
    non-leaf division is made to lose to a leaf one wherever both are
    reachable, while still catching what would otherwise attach to nothing.

    Nearest, not lowest-QID. The SPARQL extractor could break ties on the
    lowest QID because its VALUES clause had already restricted candidates to
    a single country; here every country's divisions are in flight at once, and
    lowest-QID lets a foreign division outrank the real one. The verified case
    is Puerto Rico: "US-PR" is a valid ISO 3166-2 code, so a Puerto Rican
    settlement reaches both its own municipality (one hop) and
    Puerto-Rico-as-a-US-state (two hops), and picking by QID handed every one
    of them to the United States -- leaving Puerto Rico with 99 regions and no
    cities. Depth first, then lowest QID for determinism at equal depth.
    """
    assign = dict(seeds)
    for round_n in range(MAX_PROPAGATION_ROUNDS):
        changed = 0
        for i in range(0, len(p131), 2):
            packed_parent = assign.get(p131[i + 1])
            if packed_parent is None:
                continue
            candidate = packed_parent + DEPTH_SCALE
            child = p131[i]
            current = assign.get(child)
            if current is None or candidate < current:
                assign[child] = candidate
                changed += 1
        if not changed:
            return {qid: packed % DEPTH_SCALE for qid, packed in assign.items()}, round_n + 1
    raise RuntimeError('P131 propagation did not converge in %d rounds' % MAX_PROPAGATION_ROUNDS)


# --- selecting a country's divisions ----------------------------------------

# Hand-maintained. The root-most rule is right almost everywhere; these are the
# places where ISO itself carries something that is not a first-level division
# of the country, and no rule distinguishes them from one that is. Additive,
# and each entry says why.
REGION_EXCLUDE_CODES = frozenset(
    {
        # ISO groupings that overlap the four countries rather than dividing
        # anything. England, Scotland, Wales and Northern Ireland are the
        # divisions; these two are legal jurisdictions spanning them.
        'GB-EAW',   # England and Wales
        'GB-GBN',   # Great Britain
        # Indonesia's seven geographical units, the same shape: ISO lists them
        # beside the 38 provinces, and Wikidata does not record the provinces
        # as contained in them, so both levels survive the root-most rule.
        'ID-JW',    # Java
        'ID-KA',    # Kalimantan
        'ID-ML',    # Maluku Islands
        'ID-NU',    # Lesser Sunda Islands
        'ID-PP',    # Western New Guinea
        'ID-SL',    # Sulawesi
        'ID-SM',    # Sumatra
        # Prefectures superseded by Morocco's 2015 regions, MA-01 to MA-12.
        'MA-MMD',   # Marrakech-Medina
        'MA-MMN',   # Marrakech-Menara
    }
    # Lithuania's 60 municipalities. ISO lists them beside the 10 counties
    # (LT-AL, LT-KU, ...), and Wikidata does not record them as contained in a
    # county -- the county administrations were abolished in 2010 -- so the
    # root-most rule cannot tell the two levels apart.
    | {'LT-%02d' % n for n in range(1, 61)}
)

# Whole division types that are not first-level divisions of the country they
# get selected for. Excluding the class rather than listing codes catches the
# ones nobody has noticed yet: Greece was shipping five abolished prefectures
# and only three were obvious enough to spot by hand.
REGION_EXCLUDE_CLASSES = frozenset({
    202595,     # prefecture of Greece -- all 51 abolished by Kallikratis, 2011
    # The French overseas territories have no ISO 3166-2 code of their own, so
    # their divisions come from tier 2 -- every administrative P131 child of the
    # territory -- and that reaches three parallel tiers at once. The communes
    # are the one that holds settlements; the other two shipped 97 regions
    # between them with nothing in any of them.
    18524218,   # canton of France -- an electoral constituency, not a place.
                # Its items are not even named as places: "canton of
                # Saint-Denis-1", "canton of Le Tampon-2".
    194203,     # arrondissement of France -- a real division, but it sits
                # above the communes rather than beside them, so selecting both
                # partitions the territory twice and settlements attach to the
                # commune. Mainland France is unaffected: it has FR- codes and
                # never reaches tier 2.
})

# Same idea where two items share one code and the lower QID is the wrong one.
REGION_EXCLUDE_QIDS = frozenset({
    209706,     # Lulua District, superseded by Kasai-Central; both claim CD-KC
})


def is_country_item(record):
    """Whether this item is a country in its own right, i.e. carries an
    ISO 3166-1 code.

    32 of them also carry an ISO 3166-2 code, which makes them look like an
    ordinary division of some other country. Most are dependent territories
    coded under a parent -- Aruba is NL-AW, Guadeloupe FR-971, Puerto Rico
    US-PR -- and one is not a territory at all: the United Kingdom carries
    GB-UKM, so Q145 is a subdivision of itself.

    Selecting any of them as a division does two kinds of damage. It seeds them
    for containment at depth 0, one hop above every settlement in the
    territory, so those settlements resolve to the parent state: Gibraltar's
    P131 points at Q145, which is how Catalan Bay, Westside and Gibraltar
    itself came to ship under GB while GI shipped 117 gun batteries. And it
    emits the territory twice -- once as a region of the parent, once as its
    own country -- so the same place exists under two ids.
    """
    return bool(record.get('iso_3166_1'))


def select_admin1s(iso2, candidates, settlement_classes, not_a_place=frozenset()):
    """The leaf-most live divisions whose P300 code starts '<ISO2>-'.

    A candidate is dropped if another candidate for the same country sits
    below it in the containment graph and is not itself a settlement. The
    settlement exemption matters: some countries give a P300 code to both a
    county and to individual cities inside it as two parallel partitions, not
    a real hierarchy, and treating the city as a reason to drop the county
    throws away the county and every ordinary settlement under it.
    """
    prefix = iso2 + '-'
    selected = {}
    for qid, record in candidates.items():
        codes = [c for c in record.get('iso_3166_2', ()) if c.startswith(prefix)]
        if not codes:
            continue
        if is_excluded_division(record, settlement_classes):
            continue
        if is_country_item(record):
            continue
        if is_not_a_place(record, not_a_place):
            continue
        if qid in REGION_EXCLUDE_QIDS or set(codes) & REGION_EXCLUDE_CODES:
            continue
        if any(c.startswith('Q') and int(c[1:]) in REGION_EXCLUDE_CLASSES
               for c in record.get('instance_of', ())):
            continue
        selected[qid] = min(codes)

    # One code, one division. Six codes are claimed by two items -- Sevastopol
    # and "administrative and municipal division of Ukraine" both hold UA-40,
    # Thessaly appears twice -- and shipping both puts one place under two ids.
    # Lowest QID wins, the same rule the country index uses, so a rebuild
    # cannot flip which one survives.
    by_code = {}
    for qid, code in selected.items():
        if code not in by_code or qid < by_code[code]:
            by_code[code] = qid
    keep = set(by_code.values())
    selected = {q: c for q, c in selected.items() if q in keep}
    return selected


def select_admin1s_with_dissolved(iso2, candidates, not_a_place=frozenset()):
    """Tier 3: the same P300 selection as tier 1 with the dissolved and
    end-time exclusions lifted. For a country whose entire admin tier was
    abolished -- Madagascar's provinces, gone since 2009 -- every division is
    dissolved, so excluding them leaves nothing at all and a dissolved tier
    beats no tier.
    """
    prefix = iso2 + '-'
    selected = {}
    for qid, record in candidates.items():
        codes = [c for c in record.get('iso_3166_2', ()) if c.startswith(prefix)]
        if not codes:
            continue
        if is_country_item(record):
            continue
        if is_not_a_place(record, not_a_place):
            continue
        for cls in record.get('instance_of', ()):
            if cls in ('Q%d' % Q_ISO_STANDARD, 'Q%d' % Q_COUNTRY_CODE_DATASET):
                break
        else:
            selected[qid] = min(codes)
    return selected


def select_admin1s_under_country(country_qid, country_children, records,
                                 admin_classes, not_a_place=frozenset()):
    """Tier 2: administrative territorial entities that are direct P131
    children of the country, selected without reference to P300 at all.

    This is what recovers the dependent territories -- French overseas
    departments, UK crown dependencies, US territories, Hong Kong -- whose
    divisions are coded under a parent country's ISO 3166-2 list and so carry
    no "<ISO2>-" code of their own.
    """
    selected = {}
    for child in country_children.get(country_qid, ()):
        record = records.get(child)
        if record is None:
            continue
        if record.get('dissolved') or record.get('end_time'):
            continue
        if is_country_item(record):
            continue
        if is_not_a_place(record, not_a_place):
            continue
        if child in REGION_EXCLUDE_QIDS:
            continue
        classes = [int(c[1:]) for c in record.get('instance_of', ()) if c.startswith('Q')]
        if set(classes) & REGION_EXCLUDE_CLASSES:
            continue
        for cls in classes:
            if cls in admin_classes:
                selected[child] = None
                break
    return selected


def keep_root_most(selected, p131_parents_of):
    """Keep only divisions with no other selected division above them.

    ISO 3166-2 is a flat list per country, but for about a fifth of countries
    it describes two levels at once: France has regions and departements,
    Spain autonomous communities and provinces, Czechia kraje and okresy. A
    division whose ISO-coded ancestor is also in the list is the lower of the
    two, so dropping it leaves exactly the first level.

    This is the "no ISO-coded parent" rule, and it is the only definition of
    level 1 that is consistent across countries. Natural Earth, which is a
    reasonable cross-check, is not: it picks departements for France and
    autonomous communities for Spain, which is per-country judgement rather
    than a rule.

    Measured against ISO: India 36, Spain 19, Italy 20, Czechia 14, Bangladesh
    8, Germany 16, United States 51, France 13, and Britain resolves to
    England, Scotland, Wales and Northern Ireland. France is 13 rather than 18
    because its five overseas regions ship as their own countries, and the
    regions abolished in 2016 carry dissolution dates and were already gone.

    The rule this replaced kept the leaf-most instead, which gave 109 regions
    for Czechia and 74 for Bangladesh -- their districts, not their regions or
    divisions.
    """
    return {q: c for q, c in selected.items()
            if not (set(p131_parents_of(q)) - {q}) & set(selected)}
