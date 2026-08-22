"""Cut a tiny scan directory out of a full one, for tests that must not read 1.7 GB.

The golden master is decisive but costs six and a half minutes, which makes it a
gate rather than a feedback loop. A fixture scan gives the same end-to-end path
-- graphs, closures, index, tiers, propagation, grouping, emit -- over a handful
of countries in a few seconds.

What makes the fixture trustworthy is that the build over it reproduces the full
build's output for those countries *byte for byte*. That is not automatic, and
the trimming below is arranged to preserve it:

  entities/   whole shards for the chosen countries, plus -- from every other
              shard -- only what those countries actually reach: their own
              country items, the divisions carrying a "<ISO2>-" code together
              with everything above them in the containment graph, the capital,
              continent, currency and official-language items their country
              blocks name, and their direct P131 children, which is the pool
              tier 2 selects from. Original filenames and line order are kept,
              so the build's sorted(listdir) traversal sees the same sequence.

              Keeping every country and every division worldwide would be
              simpler and is what this did first; it produced a 27 MB fixture,
              almost all of it labels for places no chosen country can see.

  P279        every edge on a path from a class the fixture mentions up to its
              roots. The closure walks *down* from roots, so keeping the upward
              paths keeps membership identical for every class anything in the
              fixture is an instance of. Classes outside that set may be missing
              from the closure, but nothing ever asks about them.

  P131        every edge on a path from a fixture entity up through its
              containers, which is what nearest-seed propagation walks. Trimming
              can only remove paths, never shorten one, so an entity cannot
              attach closer here than it did in the full build.

The one difference the trimming cannot erase is that a fixture build seeds only
the chosen countries' divisions, while the full build seeds every country's. A
settlement whose nearest division belonged to some *other* country would attach
here where it did not there. That is a property of the country set, not of the
trim, so the check is empirical: build the fixture and compare against the
reference. Countries that differ do not belong in the fixture.

    python tools/make_fixture.py <full-scan> -o tests/fixtures/scan --countries GI,MT,VA
"""

import argparse
import array
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dump_build import load_edges  # noqa: E402


def ancestor_set(edges, seeds):
    """Every node reachable upward from `seeds` through child->parent edges,
    seeds included. Rescans the flat array until a pass adds nothing, the same
    shape as the closures in the build."""
    reached = set(seeds)
    while True:
        added = 0
        for i in range(0, len(edges), 2):
            child = edges[i]
            if child in reached:
                parent = edges[i + 1]
                if parent not in reached:
                    reached.add(parent)
                    added += 1
        if not added:
            return reached


def keep_edges(edges, children):
    """Edges whose child is in `children`, in the original order."""
    out = array.array('i')
    for i in range(0, len(edges), 2):
        if edges[i] in children:
            out.append(edges[i])
            out.append(edges[i + 1])
    return out


def shard_names(entities_dir):
    return sorted(n for n in os.listdir(entities_dir) if n.endswith('.jsonl'))


def survey(entities_dir, wanted):
    """One pass collecting what the trim decisions need: the chosen countries'
    own records, and the id of every division carrying a "<ISO2>-" code for one
    of them."""
    prefixes = tuple(c + '-' for c in wanted)
    country_records = {}
    country_qids = {}
    divisions = set()
    for name in shard_names(entities_dir):
        with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                for code in record.get('iso_3166_1') or ():
                    if code not in wanted:
                        continue
                    country_records[record['id']] = record
                    # Lowest QID wins, matching the build's index.
                    if code not in country_qids or record['id'] < country_qids[code]:
                        country_qids[code] = record['id']
                for code in record.get('iso_3166_2') or ():
                    if code.startswith(prefixes):
                        divisions.add(record['id'])
                        break
    return country_records, country_qids, divisions


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('scan_dir')
    parser.add_argument('-o', '--out', required=True)
    parser.add_argument('--countries', required=True,
                        help='comma-separated ISO2 codes')
    args = parser.parse_args()

    entities_dir = os.path.join(args.scan_dir, 'entities')
    wanted = [c.strip().upper() for c in args.countries.split(',') if c.strip()]

    p279 = load_edges(os.path.join(args.scan_dir, 'graph-p279.i32'))
    p131 = load_edges(os.path.join(args.scan_dir, 'graph-p131.i32'))
    p150_path = os.path.join(args.scan_dir, 'graph-p150.i32')
    p150 = load_edges(p150_path) if os.path.exists(p150_path) else array.array('i')
    print('full graphs: P279 %d edges, P131 %d edges, P150 %d edges'
          % (len(p279) // 2, len(p131) // 2, len(p150) // 2))

    print('surveying countries and divisions...')
    country_records, country_qids, divisions = survey(entities_dir, set(wanted))
    missing = [c for c in wanted if c not in country_qids]
    if missing:
        parser.error('no country item for %s' % ', '.join(missing))
    print('  %s' % ', '.join('%s=Q%d' % (c, country_qids[c]) for c in wanted))

    chosen_shards = {'%d.jsonl' % qid for qid in country_qids.values()}
    present = set(shard_names(entities_dir))
    for shard in sorted(chosen_shards):
        if shard not in present:
            print('  note: no shard %s -- that country stores nothing under its own P17' % shard)

    # The leaf-most rule walks upward from a selected division through P131 and
    # asks whether it meets another selected division. Cutting the chain short
    # would let a division survive here that the full build dropped.
    needed = ancestor_set(p131, divisions)

    # Capital, continent, currency and official language sit on items the
    # country block resolves names from; a country's direct children are the
    # pool tier 2 draws its divisions from.
    for record in country_records.values():
        for field in ('capital', 'continent', 'currency', 'official_language'):
            for value in record.get(field) or []:
                if isinstance(value, str) and value.startswith('Q'):
                    needed.add(int(value[1:]))
    chosen_qids = set(country_qids.values())
    needed |= chosen_qids
    children = 0
    for i in range(0, len(p131), 2):
        if p131[i + 1] in chosen_qids and p131[i] not in needed:
            needed.add(p131[i])
            children += 1
    print('  %d divisions by P300, %d items reachable above them, %d further '
          'children of the chosen countries'
          % (len(divisions), len(needed) - children - len(divisions), children))

    out_entities = os.path.join(args.out, 'entities')
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(out_entities)

    print('writing shards...')
    kept_ids = set()
    classes = set()
    shards_written = lines_written = 0
    for name in shard_names(entities_dir):
        whole = name in chosen_shards
        out_lines = []
        with open(os.path.join(entities_dir, name), encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                if not (whole or record['id'] in needed):
                    continue
                out_lines.append(line)
                kept_ids.add(record['id'])
                for cls in record.get('instance_of') or ():
                    if cls.startswith('Q'):
                        classes.add(int(cls[1:]))
        if not out_lines:
            continue
        with open(os.path.join(out_entities, name), 'w', encoding='utf-8') as out:
            out.writelines(out_lines)
        shards_written += 1
        lines_written += len(out_lines)
    print('  %d shards, %d records, %d distinct classes'
          % (shards_written, lines_written, len(classes)))

    print('trimming graphs...')
    contained = ancestor_set(p131, kept_ids)
    small_p279 = keep_edges(p279, ancestor_set(p279, classes))
    small_p131 = keep_edges(p131, contained)
    # Kept on the same rule and over the same set, because the build walks the
    # two together: a P150 edge whose child the fixture never sees can rescue
    # nothing, and one whose child it does see must survive or the fixture
    # places fewer settlements than the full build.
    small_p150 = keep_edges(p150, contained)
    for filename, edges in (('graph-p279.i32', small_p279),
                            ('graph-p131.i32', small_p131),
                            ('graph-p150.i32', small_p150)):
        with open(os.path.join(args.out, filename), 'wb') as out:
            edges.tofile(out)
        print('  %s: %d edges' % (filename, len(edges) // 2))

    with open(os.path.join(args.out, 'scan-stats.json'), 'w', encoding='utf-8') as out:
        out.write(json.dumps({
            # The scan's name, not its path: this file is committed and an
            # absolute path from whichever machine cut the fixture is noise.
            'fixture_of': os.path.basename(os.path.normpath(args.scan_dir)),
            'countries': wanted,
            'entities_kept': lines_written,
            'shards': shards_written,
            'p279_edges': len(small_p279) // 2,
            'p131_edges': len(small_p131) // 2,
        }, indent=2, sort_keys=True) + '\n')

    total = sum(os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(args.out) for f in files)
    print('fixture at %s -- %.1f MB' % (args.out, total / 1e6))
    return 0


if __name__ == '__main__':
    sys.exit(main())
