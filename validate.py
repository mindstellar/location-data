"""Sanity-check a freshly built dataset against the one currently published.

The refresh workflow runs unattended, so an upstream outage, a format change or a
truncated download must not be able to publish a gutted dataset. This compares the
new manifest with the committed one and fails loudly on a large regression.

Two of the checks are global sums (country count, total city count), which can pass
comfortably while individual countries are gutted -- a source swap with 8x overall
growth can still ship a country with 98% fewer cities than before. The per-country
gate exists for exactly that: it fails if any single country falls below
MIN_CITY_RATIO of its city count in PER_COUNTRY_BASELINE_REF, regardless of how well
the total is doing. A drop that is investigated and genuinely acceptable (real
Wikidata sparsity, not a bug) goes in validate_allowlist.txt with a one-line reason,
not a change to the threshold.

Growth is never an error. Only losses are.

Usage:
    python validate.py <build-dir>                compare against the published release
    python validate.py --baseline FILE             compare against a specific manifest
    python validate.py --per-country-baseline-ref REF   git ref for the per-country gate
"""

import argparse
import json
import os
import subprocess
import sys

from contracts import slugify

# The build directory, set from argv. Every path below hangs off it: this used
# to be the literal 'src/', which meant validating a build required symlinking
# it into place first -- refresh.py created and removed that symlink around
# every call purely to satisfy this.
BUILD_DIR = 'src'
MANIFEST_NAME = 'manifest.json'


def manifest_path():
    return os.path.join(BUILD_DIR, MANIFEST_NAME)

# A country dropping out means every install that offered it loses its location
# tree, so the bar is much tighter for countries than for individual cities.
MAX_COUNTRY_LOSS = 0.02
MAX_CITY_LOSS = 0.10
MIN_COORD_COVERAGE = 0.99

# Per-country floor: the two checks above are global sums, so a handful of
# countries can be gutted while overall growth still passes comfortably (this
# is exactly how a source swap can ship "39 cities in the Netherlands" -- an
# 8x global gain hides a 98% loss in one country). This one compares each
# country to its own prior count, not the total.
MIN_CITY_RATIO = 0.70
ALLOWLIST_PATH = 'validate_allowlist.txt'

# What "before" means. The published release, not a git ref.
#
# This used to be a branch name in a repository that no longer exists, so
# `git show <ref>:src/json-list.json` failed, load_ref_manifest returned None,
# and the gate printed one line about skipping and passed. Both regression
# gates were dead for weeks without anything going red -- which is the exact
# failure the per-country gate exists to prevent, turned on the gate itself.
#
# The published release is the right baseline anyway: it is what consumers
# actually have, it always carries i_cities, and it cannot drift out of date
# the way a hand-maintained ref does.
BASELINE_DEFAULT = 'r2'


def load_r2_manifest():
    """The manifest of the currently published release.

    Returns None when nothing has been published yet, which is a real state on
    a first run and legitimately means "no baseline". Anything else -- missing
    credentials, an unreachable bucket -- raises, because a gate that quietly
    disables itself when it cannot reach its baseline is worse than no gate.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
    import r2
    pointer = r2.read_json('releases/latest.json')
    if pointer is None:
        return None
    return r2.read_json(pointer['manifest'])


def resolve_baseline(spec):
    """A manifest to compare against, from one of:

        r2         the currently published release (default)
        none       explicitly no baseline, for a first build
        <path>     a manifest file
        <git-ref>  a git ref carrying src/json-list.json
    """
    if spec in (None, '', 'none'):
        return None
    if spec == 'r2':
        return load_r2_manifest()
    if os.path.exists(spec):
        with open(spec, encoding='utf-8') as handle:
            return json.load(handle)
    return load_ref_manifest(spec)


def load_ref_manifest(ref):
    """Same idea as load_baseline(), but against an explicit git ref rather
    than HEAD -- the per-country gate needs a manifest that has i_cities,
    which is not guaranteed of whatever HEAD happens to be.
    """
    try:
        blob = subprocess.check_output(['git', 'show', '%s:src/%s' % (ref, MANIFEST_NAME)],
                                       stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None
    return json.loads(blob.decode('utf-8'))


def load_allowlist(path=ALLOWLIST_PATH):
    """CODE: one-line reason, one per country. Blank lines and lines
    starting with # are ignored. Missing file means an empty allowlist, not
    an error -- the gate must still work before anyone has needed one.
    """
    allowed = {}
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                code, _, reason = line.partition(':')
                code = code.strip().upper()
                reason = reason.strip()
                if code:
                    allowed[code] = reason or '(no reason given)'
    except FileNotFoundError:
        pass
    return allowed


def countries_of(manifest):
    """code -> settlement count, from either manifest shape.

    The published release carries the neutral manifest; a baseline given as a
    path may still be an older json-list.json, and a regression gate that
    cannot read the thing it is comparing against is a gate that skips.
    """
    if 'countries' in manifest:
        return {e['code']: e.get('settlements', 0) for e in manifest['countries']}
    return {e['s_country_code']: e.get('i_cities', 0) for e in manifest.get('locations', ())}


def per_country_regression(new, baseline):
    """Countries whose city count fell below MIN_CITY_RATIO of their count in
    baseline_ref. Returns (blocking, allowlisted) -- two lists of
    (code, old_n, new_n, ratio), worst ratio first. Only 'blocking' should
    fail the build; 'allowlisted' is printed too, so the allowlist stays
    visible rather than silently suppressing the signal.
    """
    if baseline is None:
        print('no published release to compare against -- skipping the per-country gate')
        return [], []

    old_by_code = countries_of(baseline)
    new_by_code = countries_of(new)
    allowlist = load_allowlist()

    blocking, allowlisted = [], []
    for code, old_n in old_by_code.items():
        if old_n <= 0:
            continue
        new_n = new_by_code.get(code, 0)
        ratio = new_n / old_n
        if ratio < MIN_CITY_RATIO:
            row = (code, old_n, new_n, ratio)
            (allowlisted if code in allowlist else blocking).append(row)

    blocking.sort(key=lambda r: r[3])
    allowlisted.sort(key=lambda r: r[3])

    if allowlisted:
        print('\ncountries below %.0f%% of baseline city count, allowlisted (%s):'
              % (100 * MIN_CITY_RATIO, ALLOWLIST_PATH))
        for code, old_n, new_n, ratio in allowlisted:
            print('  %-4s %6d -> %6d cities  (%.2fx)  -- %s' % (code, old_n, new_n, ratio, allowlist[code]))

    if blocking:
        print('\ncountries below %.0f%% of baseline city count (NOT allowlisted):'
              % (100 * MIN_CITY_RATIO))
        for code, old_n, new_n, ratio in blocking:
            print('  %-4s %6d -> %6d cities  (%.2fx)' % (code, old_n, new_n, ratio))

    return blocking, allowlisted


# A count-based gate cannot see this: a country can hold thousands of villages
# and still be missing its own capital, and every total stays healthy. The
# capital is the single row most likely to be looked up and the least
# acceptable to lose, so its absence is checked directly. Verified case: the
# leaf-most admin-1 rule selected Madrid province while Madrid the city points
# at the Community of Madrid, so the city attached to nothing and vanished --
# along with London, Athens, Dublin, Tallinn and Tirana -- while every count
# check passed.
MAX_MISSING_CAPITALS = 0.05


def capital_presence(manifest, data_dir=None, json_dir=None):
    """(present, missing) over countries whose canonical record names a capital.

    The capital name comes from the canonical stream, which carries the
    country-level block; presence is checked against the published per-country
    JSON, which is what a consumer actually receives.

    This asks whether the capital is really in the data. Nothing is synthesised
    to make it pass: a country whose capital is missing from the published file
    is reported, and a city-state whose only settlement is the capital counts
    because that settlement is real.

    Returns (0, []) when there is no canonical output to read.
    """
    data_dir = data_dir if data_dir is not None else os.path.join(BUILD_DIR, 'data')
    json_dir = json_dir if json_dir is not None else os.path.join(BUILD_DIR, 'json')
    if not os.path.isdir(data_dir):
        return 0, []
    by_code = {e['code']: os.path.basename(e['files']['json']) for e in manifest['countries']}
    present, missing = 0, []
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith('.ndjson'):
            continue
        code = name[:-7]
        capital = None
        with open(os.path.join(data_dir, name), encoding='utf-8') as handle:
            for line in handle:
                row = json.loads(line)
                if row.get('type') == 'country':
                    capital = row.get('capital_name')
                    break
        if not capital:
            continue
        published = by_code.get(code)
        slugs = set()
        if published:
            with open(os.path.join(json_dir, published), encoding='utf-8') as handle:
                country = json.load(handle)
            for region in country['regions']:
                for city in region['settlements']:
                    slugs.add(city.get('slug'))
        if slugify(capital) in slugs:
            present += 1
        else:
            missing.append((code, capital))
    return present, missing


def coord_coverage():
    """Fraction of cities across all country files that carry a coordinate."""
    with open(manifest_path(), encoding='utf-8') as handle:
        manifest = json.load(handle)
    total = missing = 0
    for entry in manifest['countries']:
        with open(os.path.join(BUILD_DIR, entry['files']['json']), encoding='utf-8') as handle:
            country = json.load(handle)
        for region in country['regions']:
            for row in region['settlements']:
                total += 1
                if row.get('latitude') is None or row.get('longitude') is None:
                    missing += 1
    return total, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build_dir', nargs='?', default='src',
                        help='the build directory to check (default: src)')
    parser.add_argument('--baseline', default=BASELINE_DEFAULT,
                        help="what to compare against: 'r2' (the published "
                             "release, default), 'none', a manifest path, or a "
                             "git ref")
    args = parser.parse_args()

    global BUILD_DIR
    BUILD_DIR = args.build_dir
    if not os.path.exists(manifest_path()):
        sys.exit('%s has no %s -- is it a build directory?'
                 % (BUILD_DIR, MANIFEST_NAME))

    with open(manifest_path(), encoding='utf-8') as handle:
        new = json.load(handle)

    failures = []

    total, missing = coord_coverage()
    coverage = (total - missing) / total if total else 0.0
    print('cities: %d, with coordinates: %d (%.2f%%)'
          % (total, total - missing, 100 * coverage))
    if coverage < MIN_COORD_COVERAGE:
        failures.append('coordinate coverage %.2f%% is below the %.0f%% floor'
                        % (100 * coverage, 100 * MIN_COORD_COVERAGE))

    baseline = resolve_baseline(args.baseline)
    if baseline is None:
        print('no baseline to compare against; skipping regression checks')
    else:
        old_countries = set(countries_of(baseline))
        new_countries = set(countries_of(new))
        dropped = sorted(old_countries - new_countries)
        print('countries: %d -> %d (%d dropped, %d added)'
              % (len(old_countries), len(new_countries), len(dropped),
                 len(new_countries - old_countries)))
        if dropped:
            print('  dropped: ' + ', '.join(dropped))
        if old_countries and len(dropped) / len(old_countries) > MAX_COUNTRY_LOSS:
            failures.append('%d of %d countries disappeared'
                            % (len(dropped), len(old_countries)))

        old_cities = sum(countries_of(baseline).values())
        new_cities = sum(countries_of(new).values())
        if old_cities:
            print('cities in manifest: %d -> %d (%+.1f%%)'
                  % (old_cities, new_cities, 100 * (new_cities - old_cities) / old_cities))
            if (old_cities - new_cities) / old_cities > MAX_CITY_LOSS:
                failures.append('city count fell by more than %.0f%%'
                                % (100 * MAX_CITY_LOSS))

    present, missing_capitals = capital_presence(new)
    if present or missing_capitals:
        total = present + len(missing_capitals)
        rate = len(missing_capitals) / total
        print('capitals: %d of %d countries contain their own (%.1f%% missing)'
              % (present, total, 100 * rate))
        if missing_capitals:
            print('  missing: ' + ', '.join('%s=%s' % row for row in missing_capitals[:20]))
        if rate > MAX_MISSING_CAPITALS:
            failures.append('%d of %d countries do not contain their own capital'
                            % (len(missing_capitals), total))

    blocking, _allowlisted = per_country_regression(new, baseline)
    if blocking:
        failures.append('%d countries fell below %.0f%% of their baseline city count (see list above)'
                        % (len(blocking), 100 * MIN_CITY_RATIO))

    if failures:
        print('\nFAILED:')
        for failure in failures:
            print('  - ' + failure)
        return 1

    print('\nOK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
