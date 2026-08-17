"""Publish a build to R2 as a release, and back the query cache up beside it.

    python tools/publish.py release <build-dir> [--version 2026-08-14T0446Z]
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

A release also tags the pipeline commit that produced it and opens a GitHub
release pointing at the data, because the dataset's headline property is that
the same Wikidata state and the same code produce the same bytes -- which is
only checkable if you know which code. The tag is named for the release, so the
git tag and the bucket prefix are the same string. Nothing is attached to the
GitHub release: 1.8 GB of data belongs where a consumer can fetch one country
of it.

<version> is the UTC time to the minute, so every release is its own immutable
prefix and publishing twice in a day does not overwrite anything. That matters
at the edge rather than in the bucket: release paths are cached for 30 days
with nothing to expire them, so an overwritten path serves bytes that no longer
match the manifest's sha256.

Publishing is idempotent in the way that matters: the build is byte
deterministic, so a rebuild over an unchanged Wikidata state produces the same
s_version, and this refuses to publish it whatever the path would have been
called. That is what keeps a monthly refresh from minting releases that contain
nothing new.
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
# The repository, not tools/: git and gh both have to run from it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cdn  # noqa: E402
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


def default_version():
    """The release path, to the minute rather than to the day.

    A date alone collides with itself the second time you publish in one day,
    and the only way past a collision is --force, which overwrites paths the
    CDN is holding for 30 days with nothing to expire them. Minutes make every
    release its own immutable prefix, so the overwrite case stops arising and
    a purge is only ever needed for the pointer.

    UTC, matching the 'released' stamp. Seconds would be false precision: a
    build takes seven minutes and an upload several more.
    """
    return (datetime.datetime.now(datetime.timezone.utc)
                    .strftime('%Y-%m-%dT%H%MZ'))


def build_version(build_dir):
    with open(os.path.join(build_dir, MANIFEST), encoding='utf-8') as handle:
        manifest = json.load(handle)
        return manifest.get('s_version') or manifest['version']


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


# The pipeline files whose contents decide what the build produces. A tag
# claims "this code made that data", so it is only honest if these match the
# tree the build ran from.
BUILD_FILES = ('classify.py', 'contain.py', 'contracts.py', 'countryblock.py',
               'emit.py', 'naming.py', 'dump_build.py', 'dump_scan.py')

RELEASE_NOTES = """\
**%(countries)d countries · %(regions)s administrative divisions · \
%(settlements)s settlements**, every one with coordinates.

`s_version` **`%(s_version)s`** -- the content fingerprint. The build is byte
deterministic, so this tag plus the same Wikidata dump reproduces it exactly.

### Getting the data

The data is **not attached here**. It is %(gb).1f GB across %(files)d files and lives in
object storage, where a consumer can fetch one country instead of all of them:

```bash
curl -s %(host)s/releases/latest.json
curl -s --compressed %(host)s/releases/%(version)s/manifest.json
curl -s --compressed %(host)s/releases/%(version)s/json/MT.json
```

Every file's sha256 is in the manifest. Three formats per country:
`data/<CC>.ndjson` to stream, `json/<CC>.json` nested, `csv/<CC>.csv` flat.

### What this tag is for

The dataset's headline property is that the same Wikidata state and the same
code produce the same bytes. That is only checkable if you know which code --
this tag is that link.

### Licence

Data **CC0-1.0**. Pipeline **GPL-3.0**.
"""


def _git(*args):
    """Run git and return stdout, or None if it fails or git is absent."""
    try:
        done = subprocess.run(('git',) + args, cwd=ROOT, capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def tag_release(version, pointer):
    """Tag the pipeline commit that produced this data and open a GitHub
    release for it.

    Best effort, and deliberately after the upload: the data is already in R2,
    and a missing tag is a missing cross-reference rather than a broken
    release. Publishing from a clone without push rights, or without gh, is a
    normal thing to do.

    The tag is named for the release, not vN.N.N, so the git tag and the bucket
    prefix are the same string and there is no mapping to keep.
    """
    if _git('rev-parse', '--git-dir') is None:
        print('not a git checkout, so no tag was made')
        return

    # A tag on a dirty pipeline claims something untrue: the commit it points
    # at is not what built the data. Everything else -- README, docs, the
    # allowlist -- can be dirty without affecting a byte of output.
    dirty = _git('status', '--porcelain', '--', *BUILD_FILES)
    if dirty:
        print('NOT tagging: the pipeline has uncommitted changes, so no commit '
              'describes what built this release.')
        for line in dirty.splitlines():
            print('    %s' % line)
        return

    if _git('rev-parse', '-q', '--verify', 'refs/tags/%s' % version) is not None:
        print('tag %s already exists' % version)
        return

    message = ('Data release %s (s_version %s)\n\n'
               '%d countries, %s administrative divisions, %s settlements.\n\n'
               'This is the pipeline commit that produced that data.'
               % (version, pointer['s_version'], pointer['countries'],
                  '{:,}'.format(pointer['regions']),
                  '{:,}'.format(pointer['settlements'])))
    if _git('tag', '-a', version, '-m', message) is None:
        print('could not create the tag')
        return
    print('tagged %s' % version)

    if _git('push', 'origin', version) is None:
        print('  tag made locally but not pushed -- push it when you have rights')
        return
    print('  pushed')

    notes = RELEASE_NOTES % {
        'countries': pointer['countries'],
        'regions': '{:,}'.format(pointer['regions']),
        'settlements': '{:,}'.format(pointer['settlements']),
        's_version': pointer['s_version'],
        'version': version,
        'gb': pointer['bytes'] / 1e9,
        'files': pointer.get('files') or 768,
        'host': 'https://%s' % cdn.PUBLIC_HOST,
    }
    title = '%s — %s settlements' % (version, '{:,}'.format(pointer['settlements']))
    try:
        done = subprocess.run(
            ['gh', 'release', 'create', version, '--title', title,
             '--notes-file', '-', '--verify-tag'],
            cwd=ROOT, input=notes, capture_output=True, text=True)
    except OSError:
        print('  gh is not installed, so no GitHub release was opened')
        return
    if done.returncode:
        print('  gh could not open the release: %s' % done.stderr.strip().splitlines()[-1:])
        return
    print('  release %s' % done.stdout.strip())


def publish_release(args):
    build_dir = args.path
    manifest_path = os.path.join(build_dir, MANIFEST)
    if not os.path.exists(manifest_path):
        sys.exit('%s has no %s -- is it a build directory?' % (build_dir, MANIFEST))

    s_version = build_version(build_dir)
    version = args.version or default_version()
    prefix = 'releases/%s' % version

    existing = r2.read_json('releases/latest.json')
    if existing and existing.get('s_version') == s_version and not args.force:
        print('nothing to publish: %s already carries s_version %s'
              % (existing.get('version'), s_version))
        print('the build is deterministic, so this means Wikidata has not moved.')
        return 0
    # MANIFEST, not the json-list.json this used to look for -- that name came
    # from the layout this replaced and no build has written it since, so the
    # guard never fired. A second publish on one day would have overwritten the
    # first silently, without --force being involved at all, which is the one
    # way to leave the edge holding bytes that no longer match the manifest.
    if r2.exists('%s/%s' % (prefix, MANIFEST)) and not args.force:
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

    if not args.no_tag:
        tag_release(version, pointer)

    # Best effort, not a precondition. A bucket does not have to have a CDN in
    # front of it, and the release itself is already in R2 by this point --
    # refusing to publish for want of a purge token would be refusing the thing
    # that matters for want of the thing that follows it.
    if args.no_purge or not cdn.configured():
        if not args.no_purge:
            print('CDN not purged: CF_API_TOKEN and CF_ZONE_ID are not set '
                  '(see .env.example).')
        print('  releases/latest.json clears itself within 60s. Anything '
              'overwritten under %s does not -- purge it by hand.' % prefix)
        return 0
    # A new version writes paths the edge has never seen, so only the pointer
    # needs clearing. --force overwrites paths the edge is holding for 30 days
    # and nothing expires them, so the whole host goes.
    if args.force:
        print('purging the CDN for %s (--force overwrote cached paths)' % cdn.PUBLIC_HOST)
        cdn.purge_host()
    else:
        for url in cdn.purge_urls(['releases/latest.json', '%s/%s' % (prefix, MANIFEST)]):
            print('  purged %s' % url)
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
            node.add_argument('--version',
                              help='defaults to the UTC time to the minute')
            node.add_argument('--force', action='store_true')
            node.add_argument('--dry-run', action='store_true')
        if name == 'release':
            node.add_argument('--no-purge', action='store_true',
                              help='skip the CDN purge (only correct if no CDN '
                                   'is in front of this bucket)')
            node.add_argument('--no-tag', action='store_true',
                              help='skip the git tag and GitHub release')
        node.set_defaults(handler=handler)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == '__main__':
    sys.exit(main())
