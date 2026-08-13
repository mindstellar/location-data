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


def select_admin1s(iso2, candidates, settlement_classes):
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
        selected[qid] = min(codes)
    return selected


def select_admin1s_with_dissolved(iso2, candidates):
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
        for cls in record.get('instance_of', ()):
            if cls in ('Q%d' % Q_ISO_STANDARD, 'Q%d' % Q_COUNTRY_CODE_DATASET):
                break
        else:
            selected[qid] = min(codes)
    return selected


def select_admin1s_under_country(country_qid, country_children, records,
                                 admin_classes):
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
        for cls in record.get('instance_of', ()):
            if cls.startswith('Q') and int(cls[1:]) in admin_classes:
                selected[child] = None
                break
    return selected


def drop_non_leaf(selected, candidates, p131_parents_of, settlement_classes, exclude_classes=None):
    """Remove any selected division that contains another selected division
    which is not itself a settlement."""
    has_sub_area = set()
    for qid in selected:
        record = candidates.get(qid)
        if record is None or is_settlement(record, settlement_classes, exclude_classes):
            continue
        for ancestor in p131_parents_of(qid):
            if ancestor in selected and ancestor != qid:
                has_sub_area.add(ancestor)
    return {q: c for q, c in selected.items() if q not in has_sub_area}
