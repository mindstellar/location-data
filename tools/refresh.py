"""The monthly refresh, start to finish, on whatever machine you are sitting at.

    python tools/refresh.py --work-dir ~/development/wikidata-dump

Five stages, each skippable and each idempotent, so an interrupted run is
resumed by running it again rather than started over:

    cache    pull the query cache from R2 if it is not already on disk
    dump     fetch the Wikidata truthy dump if it is not already on disk
    scan     stage 1, ~90 minutes
    build    stage 2, ~5 minutes
    publish  validate, then upload a release and back the cache up

This deliberately does not run in CI. The dump is 67 GB and the scan holds a
machine for an hour and a half; a hosted runner is the wrong shape for it, and
the diff a refresh produces -- real administrative divisions renamed and
reparented -- is meant to be read by a person before it ships.

Nothing here publishes without validate.py passing, and publish.py refuses a
release whose s_version already exists. The build is byte deterministic, so a
month in which Wikidata did not move produces no release at all.
"""

import argparse
import concurrent.futures
import os
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import r2  # noqa: E402

# dumps.wikimedia.org throttles to about 1.2 MB/s per connection, which is
# 15 hours for this file. This mirror carries wikidatawiki/entities/
# byte-identically and sustained 22-30 MB/s across eight parallel ranges,
# which is about 50 minutes. bytemark and umu.se 404 on entities/.
DUMP_URL = ('https://dumps.wikimedia.your.org/wikidatawiki/entities/'
            'latest-truthy.nt.gz')
DUMP_NAME = 'latest-truthy.nt.gz'
PARTS = 8


def run(command, **kwargs):
    print('  $ %s' % ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, **kwargs)
    if result.returncode:
        sys.exit('failed: %s' % ' '.join(command))
    return result


def stage_cache(args):
    """The cache is hours of querying a slow, rate-limiting endpoint. It is not
    needed to build from the dump, but it is what the SPARQL cross-check runs
    on, and losing it is expensive in wall-clock rather than in money."""
    target = os.path.expanduser(args.cache_dir)
    if os.path.isdir(target) and os.listdir(target):
        print('cache: %d files already at %s' % (len(os.listdir(target)), target))
        return
    pointer = r2.read_json('cache/latest.json')
    if pointer is None:
        print('cache: nothing in R2 to restore, and nothing on disk. Skipping.')
        return
    print('cache: restoring %s (%d files, %.0f MB) from R2'
          % (pointer['key'], pointer['files'], pointer['bytes'] / 1e6), flush=True)
    archive = os.path.join(os.path.dirname(target.rstrip('/')), 'cache-restore.tar.gz')
    r2.get(pointer['key'], archive)
    if pointer.get('sha256') and r2.sha256(archive) != pointer['sha256']:
        sys.exit('cache: sha256 mismatch on the restored archive; refusing to unpack')
    os.makedirs(target, exist_ok=True)
    with tarfile.open(archive) as tar:
        # Refuse anything that would land outside the target. The archive is
        # ours, but an unpack that trusts its paths is an unpack that can be
        # made to write anywhere.
        for member in tar.getmembers():
            if member.name.startswith('/') or '..' in member.name.split('/'):
                sys.exit('cache: refusing suspicious path in archive: %s' % member.name)
        tar.extractall(target)
    os.unlink(archive)
    print('cache: restored to %s' % target)


def _fetch_range(url, path, start, end, index):
    """One byte range to its own file, retried from the start of the range.

    Never appends on retry. A chunked fetcher that combines curl --retry with
    >> re-requests from the range's original offset and appends the bytes a
    second time; that produced a 75.8 GB file from a 71.1 GB source, and the
    parts had already been concatenated and deleted, so it cost a full
    re-download to find out.
    """
    import requests
    expected = end - start + 1
    for attempt in range(6):
        try:
            with requests.get(url, headers={'Range': 'bytes=%d-%d' % (start, end)},
                              stream=True, timeout=(30, 300)) as response:
                response.raise_for_status()
                with open(path, 'wb') as handle:      # 'wb', not 'ab'
                    for chunk in response.iter_content(1 << 20):
                        handle.write(chunk)
            got = os.path.getsize(path)
            if got == expected:
                return index, got
            print('  part %d: got %d of %d bytes, retrying' % (index, got, expected),
                  flush=True)
        except Exception as error:                     # noqa: BLE001
            print('  part %d: %s, retrying' % (index, error), flush=True)
        time.sleep(2 ** attempt)
    sys.exit('part %d never completed' % index)


def stage_dump(args):
    import requests
    target = os.path.join(os.path.expanduser(args.work_dir), DUMP_NAME)
    if os.path.exists(target) and os.path.getsize(target) > 1 << 30:
        print('dump: already at %s (%.1f GB)' % (target, os.path.getsize(target) / 1e9))
        return target

    os.makedirs(os.path.dirname(target), exist_ok=True)
    head = requests.head(DUMP_URL, allow_redirects=True, timeout=60)
    head.raise_for_status()
    total = int(head.headers['Content-Length'])
    if head.headers.get('Accept-Ranges') != 'bytes':
        sys.exit('dump: the mirror will not serve ranges; fetch it by hand')
    print('dump: %.1f GB from %s in %d parts' % (total / 1e9, DUMP_URL, PARTS), flush=True)

    size = total // PARTS
    bounds = [(i * size, (total - 1) if i == PARTS - 1 else ((i + 1) * size - 1))
              for i in range(PARTS)]
    parts = ['%s.part%d' % (target, i) for i in range(PARTS)]
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=PARTS) as pool:
        futures = [pool.submit(_fetch_range, DUMP_URL, parts[i], lo, hi, i)
                   for i, (lo, hi) in enumerate(bounds)]
        for future in concurrent.futures.as_completed(futures):
            index, got = future.result()
            print('  part %d done (%.1f GB)' % (index, got / 1e9), flush=True)

    # Verify every part *before* concatenating. Assembling first and checking
    # after destroys the parts, which turns a resumable failure into a full
    # re-download.
    for i, (lo, hi) in enumerate(bounds):
        expected = hi - lo + 1
        actual = os.path.getsize(parts[i])
        if actual != expected:
            sys.exit('part %d is %d bytes, expected %d -- parts kept for retry'
                     % (i, actual, expected))
    with open(target, 'wb') as out:
        for path in parts:
            with open(path, 'rb') as handle:
                while True:
                    chunk = handle.read(1 << 24)
                    if not chunk:
                        break
                    out.write(chunk)
    if os.path.getsize(target) != total:
        sys.exit('assembled file is %d bytes, expected %d' % (os.path.getsize(target), total))
    for path in parts:
        os.unlink(path)
    print('dump: %.1f GB in %.0f min' % (total / 1e9, (time.monotonic() - started) / 60))
    return target


def stage_scan(args):
    work = os.path.expanduser(args.work_dir)
    scan_dir = os.path.join(work, 'scan')
    if os.path.isdir(os.path.join(scan_dir, 'entities')) and not args.rescan:
        print('scan: reusing %s (pass --rescan to redo it)' % scan_dir)
        return scan_dir
    dump = os.path.join(work, DUMP_NAME)
    print('scan: this takes about 90 minutes', flush=True)
    # zcat feeds the scanner; Python is the bottleneck, not gzip.
    scanner = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'dump_scan.py'), '--out-dir', scan_dir],
        stdin=subprocess.PIPE, cwd=ROOT)
    unzip = subprocess.Popen(['zcat', dump], stdout=scanner.stdin)
    scanner.stdin.close()
    unzip.wait()
    if scanner.wait():
        sys.exit('scan failed')
    return scan_dir


def stage_build(args, scan_dir):
    work = os.path.expanduser(args.work_dir)
    build_dir = os.path.join(work, 'build')
    run([sys.executable, os.path.join(ROOT, 'dump_build.py'),
         '--scan-dir', scan_dir, '--out-dir', build_dir])
    return build_dir


def stage_publish(args, build_dir):
    # validate.py reads src/, so point it at the build for the duration. A
    # symlink rather than a copy: this is 377 MB.
    link = os.path.join(ROOT, 'src')
    replaced = os.path.islink(link)
    if replaced:
        os.unlink(link)
    os.symlink(os.path.abspath(build_dir), link)
    try:
        run([sys.executable, os.path.join(ROOT, 'validate.py')])
    finally:
        os.unlink(link)

    publish = [sys.executable, os.path.join(ROOT, 'tools', 'publish.py')]
    run(publish + ['release', build_dir] + (['--dry-run'] if args.dry_run else []))
    cache = os.path.expanduser(args.cache_dir)
    if os.path.isdir(cache) and os.listdir(cache):
        run(publish + ['cache', cache] + (['--dry-run'] if args.dry_run else []))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work-dir', default='~/development/wikidata-dump')
    parser.add_argument('--cache-dir', default='~/development/wikidata-cache-backup')
    parser.add_argument('--rescan', action='store_true',
                        help='redo stage 1 even if a scan is present')
    parser.add_argument('--dry-run', action='store_true',
                        help='do everything except write to R2')
    parser.add_argument('--skip', default='', help='comma-separated stages to skip')
    args = parser.parse_args()
    skip = {s.strip() for s in args.skip.split(',') if s.strip()}

    if 'cache' not in skip:
        stage_cache(args)
    if 'dump' not in skip:
        stage_dump(args)
    scan_dir = (os.path.join(os.path.expanduser(args.work_dir), 'scan')
                if 'scan' in skip else stage_scan(args))
    build_dir = (os.path.join(os.path.expanduser(args.work_dir), 'build')
                 if 'build' in skip else stage_build(args, scan_dir))
    if 'publish' not in skip:
        stage_publish(args, build_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
