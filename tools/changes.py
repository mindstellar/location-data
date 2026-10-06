"""What moved between two releases, counted place by place.

    python tools/changes.py compare <old-dir> <new-dir> [-o changes.json] [--upload]
    python tools/changes.py fetch <version> [--into .release-cache]
    python tools/changes.py readme [--releases 3]

A release directory is anything with a manifest.json and data/<CC>.ndjson
beside it: a build directory, or a published release fetched with `fetch`.

The totals in latest.json already say a release grew by 1,240 settlements. They
cannot say that 1,530 arrived and 290 left, or that 37 were renamed, because a
net figure hides every change that cancels out. So this matches records by
their Wikidata id, the one key that survives a rename or a move.

The output is derived only from the two releases it names, so like the releases
themselves it is byte deterministic: no timestamps, sorted keys.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cdn  # noqa: E402

HOST = 'https://%s' % cdn.PUBLIC_HOST
CACHE = os.path.join(ROOT, '.release-cache')
README = os.path.join(ROOT, 'README.md')

# A coordinate correction under this is Wikidata tidying a decimal, not a place
# that was in the wrong spot.
MOVED_KM = 1.0

START = '<!-- releases:start -->'
END = '<!-- releases:end -->'


def _get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'location-data-changes/1'})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(version, into=CACHE):
    """Download a published release's manifest and ndjson, verifying each sha256."""
    target = os.path.join(into, version)
    os.makedirs(os.path.join(target, 'data'), exist_ok=True)
    manifest_path = os.path.join(target, 'manifest.json')
    if not os.path.exists(manifest_path):
        with open(manifest_path, 'wb') as handle:
            handle.write(_get('%s/releases/%s/manifest.json' % (HOST, version)))
    with open(manifest_path, encoding='utf-8') as handle:
        manifest = json.load(handle)

    for entry in manifest['countries']:
        path = os.path.join(target, entry['files']['data'])
        expected = entry['sha256']['data']
        if os.path.exists(path) and _sha256(path) == expected:
            continue
        body = _get('%s/releases/%s/%s' % (HOST, version, entry['files']['data']))
        if hashlib.sha256(body).hexdigest() != expected:
            sys.exit('%s %s: sha256 does not match the manifest' % (version, entry['code']))
        with open(path, 'wb') as handle:
            handle.write(body)
    return target


def _km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = math.pi / 180
    h = (math.sin((lat2 - lat1) * r / 2) ** 2
         + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1.0, h)))


def read(release_dir):
    """Everything compared, as compact tuples: two releases of dicts do not fit."""
    with open(os.path.join(release_dir, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)

    countries = {}
    country_ids = set()
    regions = {}
    settlements = {}
    for entry in manifest['countries']:
        path = os.path.join(release_dir, entry['files']['data'])
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                kind = record.get('type')
                if kind == 'country':
                    countries[record['code']] = record['name']
                    country_ids.add(record['id'])
                elif kind == 'region':
                    regions[record['id']] = (
                        record['name'], record['country_code'],
                        record.get('iso_3166_2'), bool(record.get('unassigned')))
                elif kind == 'settlement':
                    settlements[record['id']] = (
                        record['name'], record['country_code'], record.get('admin1_id'),
                        float(record['latitude']), float(record['longitude']),
                        record.get('population'))
    # Releases before the unassigned flag existed published the bucket as a
    # division carrying the country's own id, so that is recognised too.
    for key, value in regions.items():
        if key in country_ids and not value[3]:
            regions[key] = value[:3] + (True,)
    return manifest, countries, regions, settlements


RELEASE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{4}Z$')


def release_name(release_dir):
    """The release path, which a build directory does not know: publish names it."""
    name = os.path.basename(os.path.normpath(release_dir))
    return name if RELEASE.match(name) else None


def _summary(version, manifest, regions, settlements):
    # Counted as latest.json counts them, so a table built from this agrees
    # with every badge and pointer that quotes the release.
    unassigned = {key for key, value in regions.items() if value[3]}
    return {
        'version': version,
        's_version': manifest['s_version'],
        'countries': len(manifest['countries']),
        'regions': sum(entry['regions'] for entry in manifest['countries']),
        'settlements': sum(entry['settlements'] for entry in manifest['countries']),
        'with_population': sum(1 for value in settlements.values() if value[5] is not None),
        'regions_with_iso': sum(1 for value in regions.values() if value[2] and not value[3]),
        'unassigned': sum(1 for value in settlements.values() if value[2] in unassigned),
    }


def compare(old_dir, new_dir, old_version=None, new_version=None):
    old_manifest, old_countries, old_regions, old_places = read(old_dir)
    new_manifest, new_countries, new_regions, new_places = read(new_dir)

    per_country = {}

    def bump(code, field):
        row = per_country.setdefault(code, {'added': 0, 'removed': 0, 'renamed': 0, 'moved': 0})
        row[field] += 1

    totals = {'added': 0, 'removed': 0, 'renamed': 0, 'moved': 0,
              'reassigned': 0, 'population_changed': 0}
    for key, before in old_places.items():
        after = new_places.get(key)
        if after is None:
            totals['removed'] += 1
            bump(before[1], 'removed')
            continue
        if before[0] != after[0]:
            totals['renamed'] += 1
            bump(after[1], 'renamed')
        if _km(before[3:5], after[3:5]) > MOVED_KM:
            totals['moved'] += 1
            bump(after[1], 'moved')
        if before[2] != after[2]:
            totals['reassigned'] += 1
        if before[5] != after[5]:
            totals['population_changed'] += 1
    for key, after in new_places.items():
        if key not in old_places:
            totals['added'] += 1
            bump(after[1], 'added')

    real_old = {key for key, value in old_regions.items() if not value[3]}
    real_new = {key for key, value in new_regions.items() if not value[3]}
    regions = {
        'added': len(real_new - real_old),
        'removed': len(real_old - real_new),
        'renamed': sum(1 for key in real_old & real_new
                       if old_regions[key][0] != new_regions[key][0]),
    }

    def counts(places, regions_by_id):
        out = {}
        for value in places.values():
            out.setdefault(value[1], [0, 0])[1] += 1
        for value in regions_by_id.values():
            out.setdefault(value[1], [0, 0])[0] += 1
        return out

    old_counts = counts(old_places, old_regions)
    new_counts = counts(new_places, new_regions)
    countries = []
    for code in sorted(set(old_counts) | set(new_counts) | set(per_country)):
        before = old_counts.get(code, [0, 0])
        after = new_counts.get(code, [0, 0])
        moved = per_country.get(code, {})
        if before == after and not any(moved.values()):
            continue
        row = {
            'code': code,
            'name': new_countries.get(code) or old_countries.get(code) or code,
            'regions': [before[0], after[0]],
            'settlements': [before[1], after[1]],
        }
        row.update({field: moved.get(field, 0) for field in ('added', 'removed', 'renamed', 'moved')})
        countries.append(row)

    return {
        'from': _summary(old_version or release_name(old_dir), old_manifest,
                         old_regions, old_places),
        'to': _summary(new_version or release_name(new_dir), new_manifest,
                       new_regions, new_places),
        'settlements': totals,
        'regions': regions,
        'countries_added': sorted(set(new_countries) - set(old_countries)),
        'countries_removed': sorted(set(old_countries) - set(new_countries)),
        'countries': countries,
        'moved_km': MOVED_KM,
    }


def dumps(changes):
    return json.dumps(changes, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False) + '\n'


def history(count):
    """The newest releases, newest first, each with the changes that made it."""
    pointer = json.loads(_get('%s/releases/latest.json' % HOST))
    version = pointer['version']
    out = []
    while version and len(out) < count:
        try:
            changes = json.loads(_get('%s/releases/%s/changes.json' % (HOST, version)))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            manifest = json.loads(_get('%s/releases/%s/manifest.json' % (HOST, version)))
            out.append({'version': version, 'countries': len(manifest['countries']),
                        'regions': sum(e['regions'] for e in manifest['countries']),
                        'settlements': sum(e['settlements'] for e in manifest['countries'])})
            break
        out.append(dict(changes['to'], changes=changes))
        version = changes['from']['version']
    return out


def _signed(n):
    return '+{:,}'.format(n) if n > 0 else ('−{:,}'.format(-n) if n < 0 else '0')


def table(releases):
    lines = [
        '| Release | Countries | Divisions | Settlements | Added | Removed | Renamed | Moved > 1 km |',
        '|---|--:|--:|--:|--:|--:|--:|--:|',
    ]
    for release in releases:
        changes = release.get('changes')
        if changes:
            before = changes['from']
            moved = changes['settlements']
            cells = [
                '{:,} ({})'.format(release['countries'], _signed(release['countries'] - before['countries'])),
                '{:,} ({})'.format(release['regions'], _signed(release['regions'] - before['regions'])),
                '{:,} ({})'.format(release['settlements'], _signed(release['settlements'] - before['settlements'])),
                '{:,}'.format(moved['added']), '{:,}'.format(moved['removed']),
                '{:,}'.format(moved['renamed']), '{:,}'.format(moved['moved']),
            ]
        else:
            cells = ['{:,}'.format(release['countries']), '{:,}'.format(release['regions']),
                     '{:,}'.format(release['settlements']), 'first', '', '', '']
        lines.append('| [`%s`](%s/releases/%s/manifest.json) | %s |'
                     % (release['version'], HOST, release['version'], ' | '.join(cells)))
    return '\n'.join(lines)


def write_readme(releases):
    with open(README, encoding='utf-8') as handle:
        text = handle.read()
    if START not in text or END not in text:
        sys.exit('README.md has no %s ... %s block to fill' % (START, END))
    block = '%s\n%s\n%s' % (START, table(releases), END)
    text = re.sub(re.escape(START) + '.*?' + re.escape(END), lambda _: block, text, flags=re.S)
    with open(README, 'w', encoding='utf-8') as handle:
        handle.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)

    one = sub.add_parser('compare', help='compare two release directories')
    one.add_argument('old')
    one.add_argument('new')
    one.add_argument('-o', '--out', help='write here instead of stdout')
    one.add_argument('--old-version', help='release name, if the directory is not named for it')
    one.add_argument('--new-version', help='release name, if the directory is not named for it')
    one.add_argument('--upload', action='store_true',
                     help='also put it at releases/<new>/changes.json in the bucket')

    two = sub.add_parser('fetch', help='download a published release, verified')
    two.add_argument('version')
    two.add_argument('--into', default=CACHE)

    three = sub.add_parser('readme', help='rewrite the release table in README.md')
    three.add_argument('--releases', type=int, default=3)

    args = parser.parse_args()
    if args.command == 'compare':
        changes = compare(args.old, args.new, args.old_version, args.new_version)
        if not (changes['from']['version'] and changes['to']['version']):
            sys.exit('name both releases with --old-version and --new-version')
        body = dumps(changes)
        if args.upload:
            import r2
            key = 'releases/%s/changes.json' % changes['to']['version']
            r2.put_bytes(body.encode('utf-8'), key)
            print('uploaded %s' % key, file=sys.stderr)
        if args.out:
            with open(args.out, 'w', encoding='utf-8') as handle:
                handle.write(body)
        else:
            sys.stdout.write(body)
    elif args.command == 'fetch':
        print(fetch(args.version, args.into))
    elif args.command == 'readme':
        write_readme(history(args.releases))


if __name__ == '__main__':
    main()
