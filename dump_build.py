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
import glob
import hashlib
import json
import os
import shutil
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
from contain import (
    DEPTH_SCALE,
    CountryPlan,
    keep_root_most,
    propagate_containment,
    select_admin1s,
    select_admin1s_under_country,
    select_admin1s_with_dissolved,
)
from countryblock import single_timezone_by_country
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
    args = parser.parse_args()

    started = time.monotonic()
    entities_dir = os.path.join(args.scan_dir, 'entities')

    single_zone = single_timezone_by_country()
    print('IANA tzdb: %d single-timezone countries' % len(single_zone), flush=True)

    print('loading graphs...', flush=True)
    p279 = load_edges(os.path.join(args.scan_dir, 'graph-p279.i32'))
    p131 = load_edges(os.path.join(args.scan_dir, 'graph-p131.i32'))
    print('  P279 %d edges, P131 %d edges' % (len(p279) // 2, len(p131) // 2), flush=True)

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
    admin_classes = set()
    if needs_fallback:
        print('  %d countries have no P300 tier: %s'
              % (len(needs_fallback), ', '.join(needs_fallback)), flush=True)

        # Blocked, like every other closure here: without it "military area"
        # makes gun batteries administrative divisions.
        admin_classes = subclass_closure(p279, [Q_ADMIN_TERRITORIAL_ENTITY],
                                         CLOSURE_BLOCKED)
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
    for name in sorted(os.listdir(entities_dir)):
        if not name.endswith('.jsonl'):
            continue
        with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                if not is_settlement(record, settlement_classes, exclude_classes):
                    continue
                assigned = assign.get(record['id'])
                iso2 = admin1_country.get(assigned)
                home = record.get('country')
                home_qid = int(home[1:]) if home and home.startswith('Q') else None
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
    for iso2 in wanted:
        plan = plans[iso2]
        if plan.mode == 'country':
            continue
        attached = seen_counts.get(iso2, 0) - orphans.get(iso2, 0)
        stranded = orphans.get(iso2, 0)
        if stranded >= 5 and attached < stranded * 0.25:
            if plan.country_qid is None:
                continue
            plan.use_country_as_region(country_records, admin1_candidates)
            tier4_country[plan.country_qid] = iso2
            rescued.append((iso2, attached, stranded))

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
    admin2_wanted = set()
    for iso2 in wanted:
        path = os.path.join(bucket_dir, '%s.jsonl' % iso2)
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                located = json.loads(line).get('located_in') or []
                if located and located[0].startswith('Q'):
                    admin2_wanted.add(int(located[0][1:]))

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
        stats = {'orphan': 0, 'no_label': 0, 'no_coord': 0,
                 'empty_regions': 0, 'region_no_label': 0, 'coarse_regions': 0,
                 'merged_duplicates': 0, 'ambiguous_names': 0,
                 'settlements_seen': seen_counts.get(iso2, 0), 'native_lang': ''}
        country = build_country(
            iso2, plan.country_qid,
            [os.path.join(bucket_dir, '%s.jsonl' % iso2)],
            plan.leaf, admin1_candidates, assign,
            settlement_classes, lang_codes, country_records, stats,
            mode=plan.mode, refs=refs, single_zone=single_zone,
            exclude_classes=exclude_classes, coarse=plan.coarse,
            admin2_records=admin2_records)

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
            'no_label': stats['no_label'], 'no_coord': stats['no_coord'],
            'empty_regions': stats['empty_regions'], 'native_lang': stats['native_lang'],
            'merged_duplicates': stats['merged_duplicates'],
            'ambiguous_names': stats['ambiguous_names'],
            'tier': plan.tier,
        })
        print('%-4s %-38s %5d regions %8d cities   (seen %d, orphan %d, no_label %d, '
              'no_coord %d, merged %d, ambiguous %d)'
              % (iso2, country['name'][:38], len(country['regions']), cities,
                 seen, stats['orphan'], stats['no_label'], stats['no_coord'],
                 stats['merged_duplicates'], stats['ambiguous_names']), flush=True)

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
        'version': fingerprint,
        'license': 'CC0-1.0',
        'source': 'https://www.wikidata.org/ (Wikidata truthy dump, CC0)',
        'countries': neutral,
    }
    with open(manifest_path, 'w', encoding='utf-8') as out:
        out.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')

    summary.sort(key=lambda entry: entry['code'])
    with open(os.path.join(args.out_dir, 'build-stats.json'), 'w', encoding='utf-8') as out:
        out.write(json.dumps({
            'settlement_classes': len(settlement_classes),
            'countries_indexed': len(countries),
            'divisions_selected': sum(len(p.leaf) for p in plans.values()),
            'entities_attached': len(assign),
            'propagation_rounds': rounds,
            'countries_written': len(summary),
            's_version': fingerprint,
            'cities_total': sum(entry['cities'] for entry in summary),
            'elapsed_seconds': round(time.monotonic() - started, 1),
            'countries': summary,
        }, indent=4, sort_keys=True))

    if args.bucket_dir:
        print('grouped settlements kept in %s' % bucket_dir, flush=True)
    else:
        shutil.rmtree(bucket_dir, ignore_errors=True)

    print('\n%d countries, %d cities written to %s in %.0fs'
          % (len(summary), sum(entry['cities'] for entry in summary),
             args.out_dir, time.monotonic() - started))
    print('manifest: %s  (s_version %s)' % (manifest_path, fingerprint))


if __name__ == '__main__':
    main()
