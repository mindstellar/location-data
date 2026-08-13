"""Publish a build to R2 as a release, and back the query cache up beside it.

    python tools/publish.py release <build-dir> [--version 2026-08-13]
    python tools/publish.py cache <cache-dir>
    python tools/publish.py status

Layout in the bucket:

    releases/<version>/manifest.json       the catalog a consumer reads first
    releases/<version>/LICENSE             CC0, beside the data it applies to
    releases/<version>/data/<CC>.ndjson    canonical records, streamable
    releases/<version>/json/<CC>.json      the same record, nested
    releases/<version>/csv/<CC>.csv        the same record, flat
    releases/latest.json                   which version is current

and in a second bucket, which never gets a public domain:

    cache/<version>.tar.gz                 the query cache, one object
    cache/latest.json

Uncompressed on purpose. The manifest carries a sha256 of each file's exact
bytes, so a consumer fetching one country can verify what it received without
decompressing. 1.9 GB of storage is about three cents a month and egress is
free. See docs/RELEASING.md for why gzip is not used and what to do instead if
the consumer's bandwidth ever matters more.

Publishing is idempotent in the way that matters: the build is byte
deterministic, so a rebuild over an unchanged Wikidata state produces the same
s_version, and this refuses to publish a version that is already there unless
told to overwrite. That is what keeps a monthly refresh from minting releases
that contain nothing new.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import r2  # noqa: E402

CC0 = """This data is dedicated to the public domain under CC0 1.0 Universal.

    https://creativecommons.org/publicdomain/zero/1.0/

It is derived from Wikidata (https://www.wikidata.org/), which is itself CC0.
No attribution is required and no share-alike obligation travels with it. That
is the entire point of the project: the widely used alternative,
dr5hn/countries-states-cities-database, is ODbL, whose share-alike follows the
data through every consumer forever.

Nothing here is derived from any source carrying attribution or share-alike
terms. The timezone field comes from the IANA time zone database, which is
public domain. Flag emoji and ccTLDs are computed from the ISO 3166-1 code
rather than looked up. Names in non-Latin scripts are machine transliterated,
which is a mechanical transformation of CC0 input and carries no new rights.
"""


MANIFEST = 'manifest.json'


def build_version(build_dir):
    with open(os.path.join(build_dir, MANIFEST), encoding='utf-8') as handle:
        return json.load(handle)['version']


def collect(build_dir):
    """Every file that belongs in a release, as (local path, key suffix)."""
    out = []
    for name in (MANIFEST, 'build-stats.json'):
        path = os.path.join(build_dir, name)
        if os.path.exists(path):
            out.append((path, name))
    for directory in ('data', 'json', 'csv'):
        source = os.path.join(build_dir, directory)
        if not os.path.isdir(source):
            continue
        for name in sorted(os.listdir(source)):
            path = os.path.join(source, name)
            if os.path.isfile(path):
                out.append((path, '%s/%s' % (directory, name)))
    return out


def publish_release(args):
    build_dir = args.path
    manifest_path = os.path.join(build_dir, MANIFEST)
    if not os.path.exists(manifest_path):
        sys.exit('%s has no %s -- is it a build directory?' % (build_dir, MANIFEST))

    s_version = build_version(build_dir)
    version = args.version or datetime.date.today().isoformat()
    prefix = 'releases/%s' % version

    existing = r2.read_json('releases/latest.json')
    if existing and existing.get('s_version') == s_version and not args.force:
        print('nothing to publish: %s already carries s_version %s'
              % (existing.get('version'), s_version))
        print('the build is deterministic, so this means Wikidata has not moved.')
        return 0
    if r2.exists('%s/json-list.json' % prefix) and not args.force:
        sys.exit('%s already exists. Pass --force to overwrite it, or use a '
                 'different --version.' % prefix)

    files = collect(build_dir)
    total = sum(os.path.getsize(p) for p, _ in files)
    print('publishing %s -> r2://%s/%s' % (build_dir, r2.BUCKET, prefix))
    print('  %d files, %.0f MB, s_version %s' % (len(files), total / 1e6, s_version))
    if args.dry_run:
        for path, key in files[:5]:
            print('    would put %s' % key)
        print('    ... and %d more' % (len(files) - 5))
        return 0

    r2.put_bytes(CC0.encode('utf-8'), '%s/LICENSE' % prefix, content='text/plain')
    r2.put_many([(p, '%s/%s' % (prefix, k)) for p, k in files])

    with open(manifest_path, encoding='utf-8') as handle:
        manifest = json.load(handle)
    pointer = {
        'version': version,
        's_version': s_version,
        'license': manifest.get('license'),
        'source': manifest.get('source'),
        'countries': len(manifest['countries']),
        'settlements': sum(e['settlements'] for e in manifest['countries']),
        'regions': sum(e['regions'] for e in manifest['countries']),
        'bytes': total,
        'manifest': '%s/%s' % (prefix, MANIFEST),
        # Stamped after the build, never during it: a timestamp inside the
        # build would change the fingerprint on every run and make a refresh
        # publish a release that contains nothing new.
        'released': datetime.datetime.now(datetime.timezone.utc)
                            .replace(microsecond=0).isoformat(),
    }
    r2.put_bytes(json.dumps(pointer, indent=2, sort_keys=True).encode('utf-8'),
                 'releases/latest.json')
    print('published. releases/latest.json now points at %s' % version)
    return 0


def publish_cache(args):
    """The query cache as one object. 3,413 small JSON files is hours of
    querying against an endpoint that answers slowly and rate-limits; as
    separate objects it would be 3,413 round trips to restore."""
    cache_dir = args.path
    if not os.path.isdir(cache_dir):
        sys.exit('%s is not a directory' % cache_dir)
    version = args.version or datetime.date.today().isoformat()
    key = 'cache/%s.tar.gz' % version

    if r2.exists(key, r2.CACHE_BUCKET) and not args.force:
        sys.exit('%s already exists in %s. Pass --force to overwrite.'
                 % (key, r2.CACHE_BUCKET))

    entries = sorted(os.listdir(cache_dir))
    print('packing %d cache files...' % len(entries), flush=True)
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        temp_path = tmp.name
    try:
        # Sorted, and with mtimes and ownership dropped, so packing the same
        # cache twice produces the same bytes.
        with tarfile.open(temp_path, 'w:gz') as tar:
            for name in entries:
                info = tar.gettarinfo(os.path.join(cache_dir, name), arcname=name)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                with open(os.path.join(cache_dir, name), 'rb') as handle:
                    tar.addfile(info, handle)
        size = os.path.getsize(temp_path)
        print('  %.0f MB packed, uploading...' % (size / 1e6), flush=True)
        if args.dry_run:
            return 0
        r2.put(temp_path, key, r2.CACHE_BUCKET)
        r2.put_bytes(json.dumps({
            'version': version, 'key': key, 'files': len(entries), 'bytes': size,
            'sha256': r2.sha256(temp_path),
            'backed_up': datetime.datetime.now(datetime.timezone.utc)
                                 .replace(microsecond=0).isoformat(),
        }, indent=2, sort_keys=True).encode('utf-8'), 'cache/latest.json',
            r2.CACHE_BUCKET)
        print('backed up to r2://%s/%s' % (r2.CACHE_BUCKET, key))
    finally:
        os.unlink(temp_path)
    return 0


def status(args):
    for name, key, bucket in (('release', 'releases/latest.json', r2.BUCKET),
                              ('cache', 'cache/latest.json', r2.CACHE_BUCKET)):
        pointer = r2.read_json(key, bucket)
        if pointer is None:
            print('%-8s nothing published' % name)
            continue
        print('%-8s %s' % (name, json.dumps(pointer, indent=2, sort_keys=True)
                                     .replace('\n', '\n         ')))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, handler, needs_path in (('release', publish_release, True),
                                      ('cache', publish_cache, True),
                                      ('status', status, False)):
        node = sub.add_parser(name)
        if needs_path:
            node.add_argument('path')
            node.add_argument('--version', help='defaults to today')
            node.add_argument('--force', action='store_true')
            node.add_argument('--dry-run', action='store_true')
        node.set_defaults(handler=handler)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == '__main__':
    sys.exit(main())
