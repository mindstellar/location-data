"""Stage 2 of the dump pipeline: turn dump_scan.py's output into the canonical
per-country records, with no network access at all.

Stage 1 harvested facts without judging them. This stage does the judging, and
it has to reproduce the semantics the SPARQL extractor established, because the
two must agree on what a settlement is and which region contains it:

  * a settlement is anything whose P31 reaches Q486972 through P279* --
    the class hierarchy, not a fixed list of types
  * a settlement belongs to the admin-1 it reaches through P131+ , and where
    it reaches more than one, the numerically lowest wins
  * an admin-1 is selected by its P300 (ISO 3166-2) code starting "<ISO2>-",
    never by P17, because a P300 code encodes its own country while P17 is an
    assertion that can point somewhere unhelpful
  * a division carrying P576 (dissolved) or P582 (end time) is excluded, and a
    division that contains another P300-bearing non-settlement is excluded as
    not leaf-most

Neither graph is randomly accessed. Stage 1's edge arrays are not sorted (the
dump is nearly QID-ordered but has occasional adjacent transpositions, so any
binary search over them would be quietly wrong), and building a child->parents
dict over ~36M edges would cost more memory than this machine has spare. Both
closures are therefore computed by rescanning the flat int32 array until it
stops changing -- a few seconds per pass, converging in the depth of the
hierarchy rather than the size of it.

Usage:
    python dump_build.py --scan-dir dump-scan --out-dir dump-build
    python dump_build.py --scan-dir dump-scan --countries AL,IN,DE
"""

import argparse
import array
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

from classify import (
    CLOSURE_BLOCKED,
    not_a_place_classes,
    Q_ADMIN_TERRITORIAL_ENTITY,
    SETTLEMENT_ROOTS,
    exclusion_sets,
    is_settlement,
    load_edges,
    subclass_closure,
)
from boundaries import Admin1Boundaries, learn_code_map
from contain import (
    DEPTH_SCALE,
    CountryPlan,
    keep_root_most,
    propagate_containment,
    rescue_with_contains,
    select_admin1s,
    select_admin1s_by_p17,
    select_admin1s_under_country,
    select_admin1s_with_dissolved,
)
from countryblock import parse_point, single_timezone_by_country
from emit import (
    build_country,
    write_canonical_ndjson,
    write_country_csv,
    write_country_json,
)

# Re-exported, not used here. The decision functions moved out of this module
# but its name is where the tests and tools reach for them, and leaving those
# imports untouched is what makes the move verifiable: if a name changed
# meaning on the way out, they fail.
from classify import MAX_PROPAGATION_ROUNDS, is_excluded_division   # noqa: F401
from naming import _usable, alt_names_for, resolve_name   # noqa: F401

# --- the entity index -------------------------------------------------------

def build_index(entities_dir):
    """One pass over every shard, collecting the entities that identify
    countries, divisions and languages. These are scattered across shards
    because a shard is keyed on P17, which a country item generally does not
    carry for itself.

    Returns (countries, country_records, admin1_candidates, lang_codes):
      countries          iso2 -> qid
      country_records    qid  -> record, for the country's own name and P37
      admin1_candidates  qid  -> record, for anything carrying P300
      lang_codes         qid  -> Wikimedia language code, for anything with P424
    """
    countries = {}
    country_records = {}
    admin1_candidates = {}
    lang_codes = {}
    files = sorted(os.listdir(entities_dir))
    for name in files:
        if not name.endswith('.jsonl'):
            continue
        with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                iso1 = record.get('iso_3166_1')
                if iso1:
                    country_records[record['id']] = record
                    for code in iso1:
                        # Lowest QID wins so a re-run cannot flip which item a
                        # duplicated code resolves to.
                        if code not in countries or record['id'] < countries[code]:
                            countries[code] = record['id']
                if record.get('iso_3166_2'):
                    admin1_candidates[record['id']] = record
                code = record.get('wm_lang_code')
                if code:
                    lang_codes[record['id']] = code
    return countries, country_records, admin1_candidates, lang_codes


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scan-dir', default='dump-scan')
    parser.add_argument('--out-dir', default='dump-build')
    parser.add_argument('--countries', help='comma-separated ISO2 codes; default all')
    parser.add_argument('--bucket-dir',
                        help='keep the grouped settlements here instead of in a '
                             'temporary directory that is removed on success')
    parser.add_argument('--boundaries',
                        help='Natural Earth admin-1 zip, for placing settlements '
                             'whose containment reaches no division at all. '
                             'Optional: without it those settlements ship in the '
                             'region named after their country, as they always have')
    args = parser.parse_args()

    started = time.monotonic()
    entities_dir = os.path.join(args.scan_dir, 'entities')

    single_zone = single_timezone_by_country()
    print('IANA tzdb: %d single-timezone countries' % len(single_zone), flush=True)

    # Loaded first, so a bad path fails in a second rather than ninety minutes
    # in. Public domain, and the only non-Wikidata source that decides anything
    # about a place here -- see boundaries.py for why it is allowed to and what
    # keeps it narrow.
    boundaries = None
    if args.boundaries:
        boundaries = Admin1Boundaries(args.boundaries)
        print('Natural Earth: %d admin-1 polygons carrying an ISO 3166-2 code, '
              'across %d countries' % (len(boundaries), len(boundaries.countries())),
              flush=True)

    print('loading graphs...', flush=True)
    p279 = load_edges(os.path.join(args.scan_dir, 'graph-p279.i32'))
    p131 = load_edges(os.path.join(args.scan_dir, 'graph-p131.i32'))
    # Optional, so a scan taken before P150 was harvested still builds -- it
    # simply builds without the rescue, which is what every release up to now
    # did. Missing is not an error; silently behaving differently would be, so
    # it is printed either way.
    p150_path = os.path.join(args.scan_dir, 'graph-p150.i32')
    p150 = load_edges(p150_path) if os.path.exists(p150_path) else array.array('i')
    print('  P279 %d edges, P131 %d edges, P150 %d edges%s'
          % (len(p279) // 2, len(p131) // 2, len(p150) // 2,
             '' if p150 else ' (this scan has none)'), flush=True)

    print('computing settlement class closure...', flush=True)
    settlement_classes = subclass_closure(p279, list(SETTLEMENT_ROOTS))
    print('  %d classes reach a settlement root through P279*' % len(settlement_classes), flush=True)
    exclude_classes = exclusion_sets(p279)
    not_a_place = not_a_place_classes(p279)
    print('  %d classes a region must not be (constituencies, dioceses, parks, '
          'time zones)' % len(not_a_place), flush=True)
    print('  excluded: %d categorical, %d former, %d soft (overridable by %d '
          'city/administrative classes)'
          % (len(exclude_classes.hard), len(exclude_classes.former),
             len(exclude_classes.soft), len(exclude_classes.rescue)), flush=True)

    print('indexing countries and divisions...', flush=True)
    countries, country_records, admin1_candidates, lang_codes = build_index(entities_dir)
    print('  %d countries by P297, %d P300-bearing divisions, %d language codes'
          % (len(countries), len(admin1_candidates), len(lang_codes)), flush=True)

    # Refuse a scan taken before the country properties were extracted. Such a
    # scan builds cleanly and produces 255 countries whose block -- capital,
    # currency, continent, calling code, demonym, ISO alpha-3 -- is entirely
    # null, and the counts, coordinates and per-country regressions all pass.
    # It cost a full build and most of a debugging session to notice. Checked
    # against the data rather than against a version stamp, so it also catches
    # a scan written before any stamp existed.
    with_capital = sum(1 for record in country_records.values() if record.get('capital'))
    if country_records and with_capital < len(country_records) * 0.5:
        sys.exit('only %d of %d country records carry P36, so this scan predates the '
                 'country-property extraction and every country block would be null. '
                 'Rescan, or point --scan-dir at a scan taken with the current '
                 'dump_scan.py.' % (with_capital, len(country_records)))

    wanted = sorted(countries)
    if args.countries:
        wanted = [c.strip().upper() for c in args.countries.split(',') if c.strip()]

    # Skip states that no longer exist. Wikidata still carries ISO 3166-1
    # codes for the German Democratic Republic, Yugoslavia, the Netherlands
    # Antilles and the Trust Territory of the Pacific Islands, and a location
    # picker offering East Germany is a defect. Detected from the country
    # item's own dissolution date rather than a hardcoded list, so exceptional
    # reservations for real places -- Ascension, Sark, Tristan da Cunha,
    # Clipperton, Diego Garcia -- are untouched, as they carry no such date.
    defunct = []
    live = []
    for iso2 in wanted:
        record = country_records.get(countries.get(iso2)) or {}
        if record.get('dissolved') or record.get('end_time'):
            defunct.append(iso2)
        else:
            live.append(iso2)
    if defunct:
        print('  skipping %d dissolved states: %s' % (len(defunct), ', '.join(defunct)), flush=True)
    wanted = live

    # Parents, for deciding which divisions have another division above them.
    #
    # Built over the divisions *and everything above them*, not the divisions
    # alone. A chain from a division up to its parent division often passes
    # through something that is not itself ISO-coded, and a map that only
    # records divisions' own parents stops at the first such link -- which
    # made Lithuania's 60 municipalities all look top-level, and left Czechia
    # with 31 regions instead of 14 and Britain with 137 instead of four.
    #
    # Closing upward is cheap: a few thousand divisions reach a few thousand
    # ancestors, not the 13.9M entities the full graph holds.
    division_ids = set(admin1_candidates)
    reachable = set(division_ids)
    while True:
        added = 0
        for i in range(0, len(p131), 2):
            if p131[i] in reachable and p131[i + 1] not in reachable:
                reachable.add(p131[i + 1])
                added += 1
        if not added:
            break
    parents = {}
    for i in range(0, len(p131), 2):
        child = p131[i]
        if child in reachable:
            parents.setdefault(child, []).append(p131[i + 1])

    def ancestors_of(qid, depth=12):
        seen, frontier = set(), [qid]
        for _ in range(depth):
            nxt = []
            for node in frontier:
                for parent in parents.get(node, ()):
                    if parent not in seen:
                        seen.add(parent)
                        nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        return seen

    print('selecting admin-1 tiers...', flush=True)
    plans = {}
    for iso2 in wanted:
        candidates = select_admin1s(iso2, admin1_candidates, settlement_classes,
                                    not_a_place)
        selected = keep_root_most(candidates, ancestors_of)
        # Nothing is held back as a coarse fallback any more. That existed
        # because the leaf-most rule selected Madrid *province* while Madrid's
        # own P131 pointed at the Community of Madrid, so the capital reached
        # no selected division at all -- which is how London, Madrid, Athens,
        # Dublin, Tallinn and Tirana were lost. Selecting the root-most
        # removes the cause: the coarser division is now the selected one.
        #
        # Keeping the finer divisions as coarse seeds would actively hurt
        # here. They would sit one level out, and a settlement inside a
        # departement reaches the departement and its region at the same
        # depth, so the tie would break on QID and scatter settlements between
        # the two levels. The country-level region is added below and is the
        # only coarse entry.
        plans[iso2] = CountryPlan(
            iso2, countries.get(iso2), selected, {}, 1 if selected else 0)

    # Tiers 2-4 run only for the countries tier 1 resolved to nothing: a
    # handful of ISO2 codes whose divisions are coded under a parent country's
    # ISO 3166-2 list, a code with no ISO 3166-1 entry, or a country whose
    # whole admin tier is dissolved. A country that resolves at tier 1 is
    # untouched by any of this.
    # Capital, continent and currency are stored as QIDs; the name behind
    # each lives on the referenced item, which sits in whatever shard its own
    # P17 put it in. Collect those alongside the fallback candidates so both
    # are served by a single pass over the shards.
    ref_qids = set()
    for record in country_records.values():
        for field in ('capital', 'continent', 'currency'):
            for value in record.get(field) or []:
                if isinstance(value, str) and value.startswith('Q'):
                    ref_qids.add(int(value[1:]))

    needs_fallback = [c for c in wanted if not plans[c].leaf]
    fallback_records = {}
    country_children = {}
    # Blocked, like every other closure here: without it "military area" makes
    # gun batteries administrative divisions. Computed whether or not any
    # country needs a fallback tier now, because the attachment fallback below
    # asks for it long after this point and cannot know in advance.
    admin_classes = subclass_closure(p279, [Q_ADMIN_TERRITORIAL_ENTITY],
                                     CLOSURE_BLOCKED)

    if needs_fallback:
        print('  %d countries have no P300 tier: %s'
              % (len(needs_fallback), ', '.join(needs_fallback)), flush=True)

        fallback_qids = {countries[c] for c in needs_fallback if c in countries}
        country_children = {}
        for i in range(0, len(p131), 2):
            parent = p131[i + 1]
            if parent in fallback_qids:
                country_children.setdefault(parent, []).append(p131[i])

    # Gathered before the tiers run, because tier 2 needs these records to
    # decide which children are administrative entities.
    wanted_records = ref_qids | {q for kids in country_children.values() for q in kids}
    refs = {}
    if wanted_records:
        print('resolving %d referenced items...' % len(wanted_records), flush=True)
        for name in sorted(os.listdir(entities_dir)):
            if not name.endswith('.jsonl'):
                continue
            with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
                for line in handle:
                    record = json.loads(line)
                    if record['id'] in wanted_records:
                        refs[record['id']] = record
        fallback_records.update(refs)

    def divisions_the_settlements_claim(country_qid):
        """Tier 4: what this country's own settlements say contains them.

        Everything involved is in one file. dump_scan.py shards on P17, and a
        division of the Faroes carries P17 = Faroe Islands exactly as its
        villages do, so the whole hierarchy -- and nothing else -- is in the
        country's own shard.

        Returns (selected, records, assignment): the divisions, the shard, and
        where each of its settlements lands, which the caller needs when this
        runs too late for containment propagation to do it.
        """
        shard = os.path.join(entities_dir, '%d.jsonl' % country_qid)
        if not os.path.exists(shard):
            return {}, {}, {}
        claimed = {}
        with open(shard, encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                claimed[record['id']] = record
        settlements = [record for record in claimed.values()
                       if is_settlement(record, settlement_classes, exclude_classes)]

        def parents_of(qid):
            for parent in (claimed.get(qid) or {}).get('located_in', ()):
                if parent.startswith('Q'):
                    number = int(parent[1:])
                    if number in claimed:
                        yield number

        ancestors = {}
        frontier = [record['id'] for record in settlements]
        for _hop in range(12):
            nxt = []
            for node in frontier:
                for parent in parents_of(node):
                    if parent not in ancestors:
                        ancestors[parent] = claimed[parent]
                        nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        # Walked through, never selected. A Faroese village sits in a
        # municipality and the municipality in a syssla, and a municipality is
        # a settlement by this pipeline's own rule -- so the chain has to pass
        # through it to reach the division above. Selecting it instead is how
        # the Cook Islands came out with Avarua, a town, as a region holding
        # the suburb next to it.
        containers = {qid: record for qid, record in ancestors.items()
                      if not is_settlement(record, settlement_classes, exclude_classes)}
        selected = select_admin1s_by_p17(country_qid, containers, admin_classes,
                                         not_a_place)
        if not selected:
            return {}, claimed, {}

        def all_ancestors(qid, depth=12):
            seen, frontier = set(), [qid]
            for _ in range(depth):
                nxt = []
                for node in frontier:
                    for parent in parents_of(node):
                        if parent not in seen:
                            seen.add(parent)
                            nxt.append(parent)
                if not nxt:
                    break
                frontier = nxt
            return seen

        selected = keep_root_most(selected, all_ancestors)

        # Nearest first, so a settlement inside a municipality inside a syssla
        # lands in the syssla and not in whatever else is above it. Ties break
        # on the lowest id, as everywhere.
        assignment = {}
        for record in settlements:
            frontier, seen = [record['id']], {record['id']}
            for _ in range(12):
                hit = sorted(node for node in frontier if node in selected)
                if hit:
                    assignment[record['id']] = hit[0]
                    break
                nxt = []
                for node in frontier:
                    for parent in parents_of(node):
                        if parent not in seen:
                            seen.add(parent)
                            nxt.append(parent)
                if not nxt:
                    break
                frontier = nxt
        return selected, claimed, assignment

    if needs_fallback:
        for iso2 in needs_fallback:
            plan = plans[iso2]
            if plan.country_qid is None:
                continue
            tier2 = select_admin1s_under_country(plan.country_qid, country_children,
                                                 fallback_records, admin_classes,
                                                 not_a_place)
            if tier2:
                plan.leaf = tier2
                plan.tier = 2
                admin1_candidates.update({q: fallback_records[q] for q in tier2
                                          if q in fallback_records})
                continue
            tier3 = select_admin1s_with_dissolved(iso2, admin1_candidates, not_a_place)
            tier3 = keep_root_most(tier3, ancestors_of)
            if tier3:
                plan.leaf = tier3
                plan.tier = 3
                continue

            # Tier 4: the divisions its own settlements name. Reached here
            # only when tier 2 found nothing at all; a tier that was found and
            # turns out to reach nothing is caught after grouping, where the
            # attachment fallback can see that it attached nothing.
            tier4, records, _assignment = divisions_the_settlements_claim(plan.country_qid)
            if tier4:
                plan.leaf = tier4
                plan.tier = 4
                admin1_candidates.update({q: records[q] for q in tier4})
                continue

            plan.use_country_as_region(country_records, admin1_candidates)

    seeds = {}
    for iso2 in wanted:
        if plans[iso2].mode != 'admin1':
            continue
        for qid in plans[iso2].leaf:
            seeds[qid] = qid
    for iso2 in wanted:
        if plans[iso2].mode != 'admin1':
            continue
        for qid in plans[iso2].coarse:
            # One level further out, so a leaf division still wins wherever
            # both are reachable and this only catches the otherwise-orphaned.
            seeds.setdefault(qid, qid + DEPTH_SCALE)

    # What the P150 rescue is allowed to answer with: the real divisions, and
    # nothing else. Taken here, before the country-level seeds are added below,
    # so a rescue can move a settlement from its country to a division and
    # never the other way about.
    division_seeds = dict(seeds)

    # Last resort: the country itself, three levels out so it loses to every
    # real division. A great many settlements -- including twenty of the
    # twenty-one capitals that were missing, Sanaa and Brazzaville and Kingston
    # among them -- carry a P131 that points straight at their country with no
    # intermediate division recorded at all. They are eligible, they have
    # coordinates, and they reached nothing, so they were dropped. Filing them
    # under a country-level region keeps them findable; the region appears only
    # if something actually lands in it.
    for iso2 in wanted:
        plan = plans[iso2]
        if plan.country_qid is None:
            continue
        if plan.mode == 'admin1':
            plan.add_country_as_coarse(country_records, admin1_candidates)
        # Seeded for country-mode countries too. Those take their settlements
        # from P17, which is the one signal here that cannot be trusted -- a
        # dependent territory's places name the parent state. Without a seed,
        # containment leads nowhere for them and P17 is all that is left: 24
        # Falkland settlements point P131 straight at the Falklands and carry
        # P17 = United Kingdom, so when the Falklands lost its only two
        # "divisions" -- an electoral unit and a church parish -- Britain took
        # them. With a seed, containment answers first and P17 only catches
        # what has no containment at all.
        seeds.setdefault(plan.country_qid, plan.country_qid + 3 * DEPTH_SCALE)

    tiers = {}
    for iso2 in wanted:
        tiers[plans[iso2].tier] = tiers.get(plans[iso2].tier, 0) + 1
    print('  %d divisions selected across %d countries; tiers: %s'
          % (sum(len(p.leaf) for p in plans.values()), len(plans),
             ', '.join('tier %d: %d' % (t, n) for t, n in sorted(tiers.items()))), flush=True)

    print('propagating containment...', flush=True)
    assign, rounds = propagate_containment(p131, seeds)
    print('  %d entities attached to a division in %d rounds' % (len(assign), rounds), flush=True)

    # The same walk again with P150 read backwards alongside P131, for the
    # settlements the first pass leaves at their country. Kept in its own map
    # rather than merged here: which of the two answers wins is decided per
    # settlement below, where the country it belongs to is known and a P150
    # edge that crosses a border can be refused.
    contains_assign = {}
    if p150:
        print('re-walking with P150 for what reached no division...', flush=True)
        contains_assign = rescue_with_contains(p131, p150, division_seeds)
        print('  %d entities reachable when a parent may state the link'
              % len(contains_assign), flush=True)

    # Settlements are grouped by the country of the division that contains
    # them, not by the shard they were stored in. A shard is keyed on P17, and
    # for a dependent territory P17 names the parent state: Guadeloupe's
    # communes carry P17 = France, Puerto Rico's carry P17 = United States. So
    # reading a country's own shard finds nothing for exactly the territories
    # the fallback tiers just rescued. Containment is the authority here;
    # storage location is an implementation detail.
    print('grouping settlements by containment...', flush=True)
    admin1_country = {}
    for iso2 in wanted:
        plan = plans[iso2]
        if plan.mode == 'admin1':
            for qid in plan.leaf:
                admin1_country[qid] = iso2
            for qid in plan.coarse:
                admin1_country.setdefault(qid, iso2)
        elif plan.country_qid is not None:
            # A country-mode country takes its settlements from P17, which is
            # the one thing here that cannot be trusted: a dependent
            # territory's places name the parent state. 24 Falkland
            # settlements have P17 = United Kingdom and containment pointing
            # straight at the Falklands, and when the Falklands lost its only
            # divisions -- they were an electoral unit and a church parish --
            # tier 4 handed them to Britain. Containment is the authority for
            # these too; P17 stays as the additional catch for settlements
            # that have no containment at all.
            admin1_country.setdefault(plan.country_qid, iso2)
    # The regions that stand for a whole country rather than a division of one.
    # These are seeded three hops out, so two of them are always the same
    # distance from a settlement that reaches neither country's divisions, and
    # the tie breaks on the lower QID. That let the United Kingdom's take 24
    # Falkland settlements the moment their own region was removed: Q145 is
    # simply a smaller number than Q9854. A country-level region must not
    # capture a settlement that another country claims.
    country_level = {plans[iso2].country_qid: iso2 for iso2 in wanted
                     if plans[iso2].mode == 'admin1' and plans[iso2].country_qid is not None}

    # (ISO2, ISO 3166-2) -> division, which is the whole vocabulary the boundary
    # lookup is allowed to answer in. A polygon whose code is not in here names
    # a division this build does not ship -- Natural Earth carries Nepal's old
    # zones and Ivory Coast's old regions -- and its answer is dropped rather
    # than approximated.
    division_by_code = {}
    for iso2 in wanted:
        for qid, code in plans[iso2].leaf.items():
            if code:
                division_by_code[(iso2, code)] = qid

    tier4_country = {}
    for iso2 in wanted:
        if plans[iso2].mode == 'country':
            tier4_country[countries[iso2]] = iso2

    # Scratch, ~370 MB of it, and it does not belong beside the published
    # files. Placed next to the output directory rather than under /tmp so it
    # lands on the same filesystem: the output here sits on a 1.4 TB mount
    # while the container root has 25 GB, and a scan twice this size would
    # fill it.
    #
    # Deleted after the manifest is written, not in a finally: a build that
    # crashes leaves them, and they are worth having. Reading them is how the
    # Gibraltar defect was traced -- the settlements were in GB's bucket, which
    # is what showed the containment was wrong rather than the classification.
    if args.bucket_dir:
        bucket_dir = args.bucket_dir
        if os.path.isdir(bucket_dir):
            for stale in os.listdir(bucket_dir):
                os.remove(os.path.join(bucket_dir, stale))
        os.makedirs(bucket_dir, exist_ok=True)
    else:
        beside = os.path.dirname(os.path.abspath(args.out_dir))
        os.makedirs(beside, exist_ok=True)
        bucket_dir = tempfile.mkdtemp(prefix='.buckets-', dir=beside)

    # QID -> ISO2, for the countries being built. Replaces a linear scan over
    # every country that ran once per unattached settlement.
    country_of = {qid: code for code, qid in countries.items() if code in plans}

    handles = {}
    orphans = {}
    seen_counts = {}
    rescued_by_contains = {}
    placed_by_boundary = {}
    # P460, "said to be the same as", read only in the one direction that is
    # safe: a settlement upstream calls the same thing as another one, and
    # which of the two has a division decides which is the real row. Recorded
    # here rather than after the rescues below, because containment is the
    # question -- a duplicate that a boundary lookup can place is still a
    # duplicate, and placing it files it beside the row it duplicates under a
    # different name, where nothing will merge them.
    same_as_rows = {}
    same_as_targets = set()
    # Per country, for build-stats.json. Counted where the decision is made,
    # so these are placements attempted rather than rows shipped -- a settlement
    # placed here can still be dropped later for having no usable name.
    by_contains = {}
    by_boundary = {}
    for name in sorted(os.listdir(entities_dir)):
        if not name.endswith('.jsonl'):
            continue
        with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                if not is_settlement(record, settlement_classes, exclude_classes):
                    continue
                assigned = assign.get(record['id'])
                if record.get('same_as') and (assigned is None or assigned in country_level):
                    targets = [int(q[1:]) for q in record['same_as'] if q.startswith('Q')]
                    if targets:
                        same_as_rows[record['id']] = targets
                        same_as_targets.update(targets)
                iso2 = admin1_country.get(assigned)
                home = record.get('country')
                home_qid = int(home[1:]) if home and home.startswith('Q') else None
                # Nothing but the country, or nothing at all: ask the P150
                # graph, and take its answer only for a division of the country
                # this settlement already belongs to. Containment stated by a
                # parent is weaker evidence than containment stated by the
                # child, so it must not be able to move a place across a border
                # -- the failure the depth ordering exists to prevent, arriving
                # by another route.
                if assigned is None or assigned in country_level:
                    better = contains_assign.get(record['id'])
                    home_iso2 = (admin1_country.get(assigned) or tier4_country.get(home_qid)
                                 or country_of.get(home_qid))
                    if better is not None and admin1_country.get(better) == home_iso2:
                        rescued_by_contains[record['id']] = better
                        by_contains[home_iso2] = by_contains.get(home_iso2, 0) + 1
                        assigned = better
                        iso2 = home_iso2
                    elif boundaries is not None and home_iso2 is not None:
                        # Nothing states where this is, from either end. It has
                        # a point, and divisions have boundaries: 863 of India's
                        # 966 unplaced rows are here, carrying a country, a
                        # class and a coordinate and nothing else. The code has
                        # to name a division of the country the settlement
                        # already belongs to or it is not used.
                        lat, lng = parse_point(record.get('coord'))
                        if lat is not None:
                            in_division = division_by_code.get(
                                (home_iso2, boundaries.code_at(lat, lng)))
                            if in_division is not None:
                                placed_by_boundary[record['id']] = in_division
                                by_boundary[home_iso2] = by_boundary.get(home_iso2, 0) + 1
                                assigned = in_division
                                iso2 = home_iso2
                if iso2 is None and home_qid is not None:
                    iso2 = tier4_country.get(home_qid)
                if iso2 is None:
                    # Reaches no division at all. It still knows its country,
                    # so hand it to that country's bucket and let the
                    # country-level region catch it rather than dropping a real
                    # place on the floor. 57,887 settlements were lost here,
                    # among them the 31 Aruban districts -- Cashero, Calabas,
                    # Catiri -- that Aruba stopped shipping the moment its own
                    # divisions became reachable.
                    #
                    # Still counted as an orphan, because the attachment
                    # fallback reads this to decide whether a country's tier is
                    # reachable at all, and that question is unchanged.
                    iso2 = country_of.get(home_qid)
                    if iso2 is None:
                        continue
                    orphans[iso2] = orphans.get(iso2, 0) + 1
                seen_counts[iso2] = seen_counts.get(iso2, 0) + 1
                out = handles.get(iso2)
                if out is None:
                    out = handles[iso2] = open(os.path.join(bucket_dir, '%s.jsonl' % iso2),
                                               'a', encoding='utf-8')
                out.write(line)
    for out in handles.values():
        out.close()
    print('  %d settlements grouped into %d countries'
          % (sum(seen_counts.values()), len(handles)), flush=True)

    # Merged only now, and only for settlements: the rescue is a decision about
    # places, not a correction to the containment graph, and build_country
    # reads this same map to file each row under a region.
    if rescued_by_contains:
        assign.update(rescued_by_contains)
        print('  %d of them reached a division only because a parent listed them'
              % len(rescued_by_contains), flush=True)
    if placed_by_boundary:
        assign.update(placed_by_boundary)
        print('  %d more state no containment at all and were placed by their '
              'coordinates' % len(placed_by_boundary), flush=True)

    # Attachment fallback. A country can select a perfectly good division tier
    # and still attach almost nothing to it, when its settlements' P131 points
    # somewhere the selected divisions cannot be reached from. Singapore is the
    # clearest case: 90 eligible settlements, 5 selected regions, and not one
    # settlement reaching any of them -- so the country shipped empty while the
    # places sat in the shard unused.
    #
    # Where that happens, a synthesised country-level region is a better answer
    # than nothing: every place is still findable and still carries its own
    # coordinates, it is simply not filed under a subdivision. This is the same
    # shape as tier 4, applied after the fact rather than for want of a tier.
    #
    # The threshold is deliberately blunt -- fewer than a fifth of a country's
    # settlements attaching at all. Sri Lanka sits at 629 attached against
    # 2,243 stranded and must NOT trip it: its 25 regions are real and hold
    # real places, and flattening them into one country-level region would be a
    # clear regression. This fires only where a division tier is present but
    # effectively unreachable.
    #
    # The floor is low because the countries this rescues are mostly tiny.
    # Niue has twelve settlements that attach to nothing and regions whose
    # labels are unusable, and shipped with no regions and no cities at all
    # until the floor came down to five.
    rescued = []
    claimed_tier4 = []
    for iso2 in wanted:
        plan = plans[iso2]
        if plan.mode == 'country':
            continue
        attached = seen_counts.get(iso2, 0) - orphans.get(iso2, 0)
        stranded = orphans.get(iso2, 0)
        if stranded >= 5 and attached < stranded * 0.25:
            if plan.country_qid is None:
                continue
            # Before flattening the country, ask tier 4's question. A tier that
            # was selected and then attached nothing is the same situation as
            # no tier at all, and it is the commoner one: the Faroe Islands
            # reach tier 2, because a handful of administrative items do point
            # P131 at the country, and then 0 of 155 settlements attach to them
            # -- while 147 sit under one of 33 municipalities, every
            # municipality inside a syssla, none of which says anything about
            # the country except P17.
            #
            # Too late for containment propagation, which has already run, so
            # the assignment comes back with the divisions. It can: everything
            # in the chain is in the one shard, and the walk is a few hundred
            # settlements rather than the whole graph.
            claimed_leaf, records, assignment = divisions_the_settlements_claim(
                plan.country_qid)
            if claimed_leaf and len(assignment) > attached:
                plan.leaf = claimed_leaf
                plan.tier = 4
                admin1_candidates.update({q: records[q] for q in claimed_leaf})
                assign.update(assignment)
                claimed_tier4.append((iso2, len(assignment), attached + stranded))
                continue
            plan.use_country_as_region(country_records, admin1_candidates)
            tier4_country[plan.country_qid] = iso2
            rescued.append((iso2, attached, stranded))

    if claimed_tier4:
        print('  %d countries selected a tier that reached nothing and take their '
              'divisions from what their settlements name instead: %s'
              % (len(claimed_tier4), ', '.join(
                  '%s (%d of %d)' % (c, n, total) for c, n, total in claimed_tier4)),
              flush=True)

    if rescued:
        print('  %d countries attached almost nothing and fall back to a country-level '
              'region: %s' % (len(rescued), ', '.join(
                  '%s (%d of %d)' % (c, a, a + s) for c, a, s in rescued)), flush=True)
        redo = {c for c, _, _ in rescued}
        handles = {}
        for iso2 in redo:
            path = os.path.join(bucket_dir, '%s.jsonl' % iso2)
            if os.path.exists(path):
                os.remove(path)
            seen_counts[iso2] = 0
            orphans[iso2] = 0
        redo_qids = {countries[c]: c for c in redo if c in countries}
        # These countries take their settlements from P17 rather than from
        # containment, and dump_scan.py files every record in a shard named for
        # exactly that P17 -- `shard = country[1:]`. So each one's settlements
        # are in a single named file and provably nowhere else. This read all
        # 1.7 GB of shards to collect a few dozen records.
        for country_qid, iso2 in sorted(redo_qids.items()):
            path = os.path.join(entities_dir, '%d.jsonl' % country_qid)
            if not os.path.exists(path):
                continue
            with open(path, encoding='utf-8') as handle:
                for line in handle:
                    record = json.loads(line)
                    if not is_settlement(record, settlement_classes, exclude_classes):
                        continue
                    home = record.get('country')
                    if not home or not home.startswith('Q'):
                        continue
                    if redo_qids.get(int(home[1:])) != iso2:
                        continue
                    seen_counts[iso2] = seen_counts.get(iso2, 0) + 1
                    out = handles.get(iso2)
                    if out is None:
                        out = handles[iso2] = open(os.path.join(bucket_dir, '%s.jsonl' % iso2),
                                                   'a', encoding='utf-8')
                    out.write(line)
        for out in handles.values():
            out.close()

    # One distribution, in field names that belong to this dataset rather than
    # to any consumer of it: data/ streams, json/ and csv/ are the same record
    # for consumers who are not, and manifest.json is the catalog. Named by ISO
    # code alone, so a country renamed upstream cannot move a URL.
    #
    # There was a second family here in a consumer's own column conventions,
    # including 518 cities synthesised for regions that have none of their own.
    # Inventing places is a presentation decision and belongs to whoever is
    # presenting them; a dataset published as a reference should contain what
    # the source contains and nothing else.
    data_dir = os.path.join(args.out_dir, 'data')
    json_dir = os.path.join(args.out_dir, 'json')
    csv_dir = os.path.join(args.out_dir, 'csv')
    manifest_path = os.path.join(args.out_dir, 'manifest.json')

    for directory in (data_dir, json_dir, csv_dir):
        os.makedirs(directory, exist_ok=True)
        for stale in os.listdir(directory):
            os.remove(os.path.join(directory, stale))

    # The division directly below the region, for every settlement that has
    # one. It is what tells two same-named places in one region apart. The
    # buckets are read rather than the passes above hooked, because both of
    # them feed the buckets and this is the one point that sees the final set.
    #
    # The same read also answers a second question, because reading 1.8 GB of
    # buckets twice to ask two things about the same rows is a minute of the
    # build for nothing: which settlements still have no division, and -- for
    # the countries that have any -- what Natural Earth's codes mean here.
    #
    # That mapping cannot be known earlier. It is learned from the settlements
    # containment has already placed, so it needs every placement made and the
    # tier-4 rescue settled, which is exactly now. See learn_code_map().
    admin2_wanted = set()
    translated = {}
    by_code_map = {}
    placed_targets = set()
    country_qids = {plans[iso2].country_qid for iso2 in wanted}
    for iso2 in wanted:
        path = os.path.join(bucket_dir, '%s.jsonl' % iso2)
        if not os.path.exists(path):
            continue
        placed, unplaced = [], []
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                located = record.get('located_in') or []
                if located and located[0].startswith('Q'):
                    admin2_wanted.add(int(located[0][1:]))
                division = assign.get(record['id'])
                if record['id'] in same_as_targets and division is not None \
                        and division not in country_qids:
                    placed_targets.add(record['id'])
                if boundaries is None or plans[iso2].mode == 'country':
                    continue
                if division is not None and division not in country_qids:
                    placed.append((record['id'], record.get('coord'), division))
                else:
                    unplaced.append((record['id'], record.get('coord')))
        if boundaries is None or not unplaced or not placed:
            continue
        # Codes for the unplaced first. If none of them fall in a polygon at
        # all there is nothing a mapping could be used for, and the country's
        # placed settlements -- 154,051 of them for Russia -- are not walked.
        wanted_codes = {}
        for qid, point in unplaced:
            lat, lng = parse_point(point)
            code = boundaries.code_at(lat, lng) if lat is not None else None
            if code is not None:
                wanted_codes[qid] = code
        if not wanted_codes:
            continue
        samples = []
        for _qid, point, division in placed:
            lat, lng = parse_point(point)
            if lat is None:
                continue
            code = boundaries.code_at(lat, lng)
            if code is not None:
                samples.append((code, division))
        learned = learn_code_map(samples)
        for qid, code in wanted_codes.items():
            division = learned.get(code)
            if division is not None:
                translated[qid] = division
                by_code_map[iso2] = by_code_map.get(iso2, 0) + 1
    # One row per place. A settlement that reaches no division and that
    # upstream says is the same thing as one that does is that one, recorded
    # twice: "Warszawa" beside Warsaw, "Cochin" beside Kochi, and an item
    # labelled "do not use" two hundred metres from Stuttgart. The row with the
    # division is the one with the evidence, and it is the one that ships.
    #
    # Distance is deliberately not consulted. P460 links confusable places as
    # readily as identical ones -- Hoya in Lower Saxony to La Hoya in Salamanca,
    # 1,778 km apart -- and every such pair has a division on both sides, so
    # asking which side is placed refuses them without measuring anything.
    duplicate_of = {}
    for qid, targets in same_as_rows.items():
        for target in targets:
            if target in placed_targets:
                duplicate_of[qid] = target
                break
    if duplicate_of:
        print('  %d settlements state no division and are said to be the same as one '
              'that does; the placed row is the one kept' % len(duplicate_of), flush=True)

    if translated:
        assign.update(translated)
        print('  %d settlements placed by reading Natural Earth\'s codes against the '
              'divisions this build ships' % len(translated), flush=True)

    admin2_records = {q: admin1_candidates[q] for q in admin2_wanted
                      if q in admin1_candidates}
    missing = admin2_wanted - set(admin2_records)
    if missing:
        print('resolving %d admin-2 names...' % len(missing), flush=True)
        for name in sorted(os.listdir(entities_dir)):
            if not name.endswith('.jsonl'):
                continue
            with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
                for line in handle:
                    record = json.loads(line)
                    if record['id'] in missing:
                        admin2_records[record['id']] = record

    print('building countries...', flush=True)
    summary = []
    neutral = []
    for iso2 in wanted:
        plan = plans[iso2]
        if plan.country_qid is None:
            print('!!! %s -- no Wikidata item carries this ISO 3166-1 code' % iso2, flush=True)
            continue
        # Starts at zero, not at the grouping pass's orphan count: those are
        # now handed to the country-level region rather than dropped, so what
        # this counts is what build_country could genuinely not place.
        stats = {'orphan': 0, 'no_label': 0, 'no_coord': 0, 'country_region': 0,
                 'said_to_be_a_duplicate': 0,
                 'empty_regions': 0, 'region_no_label': 0, 'coarse_regions': 0,
                 'merged_duplicates': 0, 'ambiguous_names': 0,
                 'stripped_qualifiers': 0, 'upstream_qualifier_kept': 0,
                 'settlements_seen': seen_counts.get(iso2, 0), 'native_lang': ''}
        country = build_country(
            iso2, plan.country_qid,
            [os.path.join(bucket_dir, '%s.jsonl' % iso2)],
            plan.leaf, admin1_candidates, assign,
            settlement_classes, lang_codes, country_records, stats,
            mode=plan.mode, refs=refs, single_zone=single_zone,
            exclude_classes=exclude_classes, coarse=plan.coarse,
            admin2_records=admin2_records, duplicate_of=duplicate_of)

        data_filename, data_digest, data_bytes = write_canonical_ndjson(country, data_dir)
        json_filename, json_digest, json_bytes = write_country_json(country, json_dir)
        csv_filename, csv_digest, csv_bytes = write_country_csv(country, csv_dir)
        regions = len(country['regions'])
        cities = sum(len(r['settlements']) for r in country['regions'])
        neutral.append({
            'code': iso2,
            'name': country['name'],
            'slug': country['slug'],
            'id': country['id'],
            'source': country['source'],
            'regions': regions,
            'settlements': cities,
            'files': {
                'data': 'data/' + data_filename,
                'json': 'json/' + json_filename,
                'csv': 'csv/' + csv_filename,
            },
            'sha256': {
                'data': data_digest,
                'json': json_digest,
                'csv': csv_digest,
            },
            'bytes': {
                'data': data_bytes,
                'json': json_bytes,
                'csv': csv_bytes,
            },
        })
        seen = stats['settlements_seen']
        summary.append({
            'code': iso2, 'name': country['name'], 'file': json_filename,
            'regions': regions, 'cities': cities,
            'settlements_seen': seen, 'orphan': stats['orphan'],
            'orphan_rate': round(stats['orphan'] / seen, 4) if seen else None,
            'no_division': orphans.get(iso2, 0),
            # What `no_division` does not say. It counts a failure of
            # containment; this counts the rows that ship in the country-named
            # region however they got there, so it can be checked against the
            # published file rather than trusted.
            'country_region': stats['country_region'],
            # How the settlements that P131 could not place were placed, if
            # they were: by a division stating it contains them, and by their
            # own coordinates falling inside one.
            'placed_by_contains': by_contains.get(iso2, 0),
            'placed_by_boundary': by_boundary.get(iso2, 0),
            'placed_by_code_map': by_code_map.get(iso2, 0),
            'said_to_be_a_duplicate': stats['said_to_be_a_duplicate'],
            'no_label': stats['no_label'], 'no_coord': stats['no_coord'],
            'empty_regions': stats['empty_regions'], 'native_lang': stats['native_lang'],
            'merged_duplicates': stats['merged_duplicates'],
            'ambiguous_names': stats['ambiguous_names'],
            'stripped_qualifiers': stats['stripped_qualifiers'],
            'upstream_qualifier_kept': stats['upstream_qualifier_kept'],
            'tier': plan.tier,
        })
        print('%-4s %-38s %5d regions %8d cities   (seen %d, orphan %d, no_label %d, '
              'no_coord %d, merged %d, ambiguous %d, in country region %d)'
              % (iso2, country['name'][:38], len(country['regions']), cities,
                 seen, stats['orphan'], stats['no_label'], stats['no_coord'],
                 stats['merged_duplicates'], stats['ambiguous_names'],
                 stats['country_region']), flush=True)

    # The manifest a consumer reads first. Sorted by country name and
    # fingerprinted from the per-file hashes rather than the clock, so two runs
    # over the same Wikidata state produce an identical s_version -- a
    # scheduled refresh must not open a pull request when nothing changed.
    neutral.sort(key=lambda entry: entry['code'])
    # Over every published file, not just the canonical one. The version is
    # what publish.py refuses to republish and what a consumer caches on, so
    # anything that changes a byte anyone can fetch has to move it. Fingerprinting
    # data/ alone meant compacting the JSON writer -- 500 MB off the release --
    # left the version identical and the change unpublishable.
    fingerprint = hashlib.sha256('\n'.join(
        '%s:%s' % (e['files'][fmt], e['sha256'][fmt])
        for e in neutral for fmt in ('data', 'json', 'csv')
    ).encode('utf-8')).hexdigest()[:16]

    manifest = {
        # 's_version', not 'version'. The release pointer uses 'version' for
        # the release label -- 2026-08-14T0715Z -- and 's_version' for this
        # hash, and the manifest calling the hash 'version' meant one field
        # name carried two different things across two documents a consumer
        # reads together. Both are emitted here for a release, so anything
        # already reading 'version' keeps working.
        's_version': fingerprint,
        'version': fingerprint,
        'license': 'CC0-1.0',
        'source': 'https://www.wikidata.org/ (Wikidata truthy dump, CC0)',
        'countries': neutral,
    }
    with open(manifest_path, 'w', encoding='utf-8') as out:
        out.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')

    summary.sort(key=lambda entry: entry['code'])
    cities_total = sum(entry['cities'] for entry in summary)
    in_country_region = sum(entry['country_region'] for entry in summary)
    placed_by_contains = sum(entry['placed_by_contains'] for entry in summary)
    placed_by_boundary = sum(entry['placed_by_boundary'] for entry in summary)
    placed_by_code_map = sum(entry['placed_by_code_map'] for entry in summary)
    duplicates_dropped = sum(entry['said_to_be_a_duplicate'] for entry in summary)
    with open(os.path.join(args.out_dir, 'build-stats.json'), 'w', encoding='utf-8') as out:
        out.write(json.dumps({
            'settlement_classes': len(settlement_classes),
            'countries_indexed': len(countries),
            'divisions_selected': sum(len(p.leaf) for p in plans.values()),
            'entities_attached': len(assign),
            'propagation_rounds': rounds,
            'countries_written': len(summary),
            's_version': fingerprint,
            'cities_total': cities_total,
            'in_country_region': in_country_region,
            'placed_by_contains': placed_by_contains,
            'placed_by_boundary': placed_by_boundary,
            'placed_by_code_map': placed_by_code_map,
            'said_to_be_a_duplicate': duplicates_dropped,
            'elapsed_seconds': round(time.monotonic() - started, 1),
            'countries': summary,
        }, indent=4, sort_keys=True))

    if args.bucket_dir:
        print('grouped settlements kept in %s' % bucket_dir, flush=True)
    else:
        shutil.rmtree(bucket_dir, ignore_errors=True)

    print('\n%d countries, %d cities written to %s in %.0fs'
          % (len(summary), cities_total, args.out_dir, time.monotonic() - started))
    print('%d cities (%.1f%%) ship in the region named after their country, across %d '
          'countries' % (in_country_region,
                         100.0 * in_country_region / max(1, cities_total),
                         sum(1 for e in summary if e['country_region'])))
    if placed_by_contains or placed_by_boundary or placed_by_code_map:
        print('%d would have been there but for a division stating it contains them, '
              '%d more but for their coordinates, and %d more but for what those '
              'coordinates turned out to mean'
              % (placed_by_contains, placed_by_boundary, placed_by_code_map))
    print('manifest: %s  (s_version %s)' % (manifest_path, fingerprint))


if __name__ == '__main__':
    main()
