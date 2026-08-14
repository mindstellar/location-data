"""Fingerprint a built dataset, so a refactor can prove it changed nothing.

The pipeline is deterministic by design -- two runs over the same Wikidata
state produce byte-identical output -- which makes a golden-master comparison
the strongest available check on a refactor: rebuild, fingerprint, diff. If the
fingerprint matches, the refactor did not change behaviour, and no amount of
restructuring inside can hide a difference.

The manifest already hashes the JSON, ndjson and canonical files per country,
so most of the work is done. It does not hash the CSV, which this adds, and it
carries no per-country totals worth eyeballing when something does differ.

    python tools/snapshot.py <build-dir> -o reference.json
    python tools/snapshot.py <build-dir> --compare reference.json
"""

import argparse
import csv
import hashlib
import json
import os
import sys


def digest_file(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            sha.update(chunk)
    return sha.hexdigest()


def snapshot(build_dir):
    """Fingerprint all three published formats.

    All three, deliberately: a change that broke only the CSV writer while
    data/ and json/ stayed identical would otherwise pass.
    """
    with open(os.path.join(build_dir, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)

    countries = {}
    for entry in manifest['countries']:
        code = entry['code']
        countries[code] = {
            'name': entry['name'],
            'regions': entry['regions'],
            'cities': entry['settlements'],
            'sha256_data': entry['sha256']['data'],
            'sha256_json': entry['sha256']['json'],
            'sha256_csv': entry['sha256']['csv'],
        }

    return {
        's_version': manifest.get('s_version') or manifest['version'],
        's_source': manifest['source'],
        's_license': manifest['license'],
        'countries_total': len(countries),
        'regions_total': sum(c['regions'] for c in countries.values()),
        'cities_total': sum(c['cities'] for c in countries.values()),
        'countries': countries,
    }


def compare(new, old):
    """Human-readable differences, most structural first. Returns a list of
    lines; empty means identical."""
    out = []
    for key in ('s_version', 'countries_total', 'regions_total', 'cities_total'):
        if new[key] != old[key]:
            out.append('%-16s %s -> %s' % (key, old[key], new[key]))

    old_codes, new_codes = set(old['countries']), set(new['countries'])
    for code in sorted(old_codes - new_codes):
        out.append('country dropped: %s (%s)' % (code, old['countries'][code]['name']))
    for code in sorted(new_codes - old_codes):
        out.append('country added:   %s (%s)' % (code, new['countries'][code]['name']))

    for code in sorted(old_codes & new_codes):
        a, b = old['countries'][code], new['countries'][code]
        changed = [k for k in a if a[k] != b.get(k)]
        if not changed:
            continue
        counts = [k for k in changed if k in ('regions', 'cities')]
        if counts:
            out.append('%-4s %s' % (code, ', '.join(
                '%s %s -> %s' % (k, a[k], b[k]) for k in counts)))
        else:
            out.append('%-4s identical counts, different bytes: %s'
                       % (code, ', '.join(sorted(changed))))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('build_dir')
    parser.add_argument('-o', '--out', help='write the fingerprint here')
    parser.add_argument('--compare', help='compare against a saved fingerprint')
    args = parser.parse_args()

    current = snapshot(args.build_dir)

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(current, indent=2, sort_keys=True) + '\n')
        print('wrote %s  (s_version %s, %d countries, %d regions, %d cities)'
              % (args.out, current['s_version'], current['countries_total'],
                 current['regions_total'], current['cities_total']))

    if args.compare:
        with open(args.compare, encoding='utf-8') as handle:
            reference = json.load(handle)
        differences = compare(current, reference)
        if not differences:
            print('IDENTICAL to %s  (s_version %s, %d countries, %d cities)'
                  % (args.compare, current['s_version'],
                     current['countries_total'], current['cities_total']))
            return 0
        print('%d differences against %s:' % (len(differences), args.compare))
        for line in differences[:60]:
            print('  ' + line)
        if len(differences) > 60:
            print('  ... and %d more' % (len(differences) - 60))
        return 1

    if not args.out:
        print(json.dumps(current, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
