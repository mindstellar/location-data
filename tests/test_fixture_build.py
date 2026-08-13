"""Build the fixture scan end to end and check it against the full reference.

The unit tests in test_decisions.py cover the decisions in isolation; this
covers everything between them -- loading the graphs, closing the class
hierarchies, indexing, selecting division tiers, propagating containment,
grouping, naming, emitting, hashing. Nothing here is mocked.

What makes it worth trusting is the assertion: the fixture build must match
the *full* build's output for its eight countries, byte for byte, not merely
match itself. tests/fixtures/scan was cut so that holds, and a refactor that
changes any of it will change a sha256 here.

Eight countries, a couple of seconds, and between them they exercise:

    MT  tier 1, a real P300 division tier with 69 regions
    MC  tier 1 where 6 settlements have no coordinates
    VA  tier 2, divisions taken from the country's P131 children
    NU  the attachment fallback, at the floor that put Niue back
    CK  the attachment fallback again, above the floor
    AL  the coarse-region rescue that recovered Tirana
    TJ  Cyrillic romanisation, and the names it still refuses

Regenerate with tools/make_fixture.py after a rescan. If a country stops
matching the reference after that, the honest reading is that the upstream
data moved -- recapture the reference deliberately rather than relaxing this.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, 'fixtures', 'scan')
REFERENCE = os.path.join(HERE, 'reference', 'build.json')

# Countries the fixture cannot reproduce exactly, and by how much.
#
# make_fixture.py takes whole shards for the chosen countries, and a shard is
# named for the P17 the scan read off the record. That misses a settlement
# whose P17 names something else entirely while its containment lands in a
# chosen country. Exactly one does here: Khait (Q4494683) is a town in
# Tajikistan whose P17 is the Soviet Union, so it sits in shard 15180 and the
# fixture never sees it, while the full build reads every shard and files it
# under TJ.
#
# Country choice is also constrained by the reverse problem -- containment
# crossing borders. Jamaica and Kyrgyzstan were dropped from this fixture
# because the full build files five Jamaican places under Haiti and Antigua and
# three Kyrgyz ones under Uzbekistan, so without those neighbours the fixture
# ships more than the full build. Adding neighbours does not converge: adding
# Uzbekistan fixed Kyrgyzstan and left Uzbekistan contested by the next country
# out.
#
# So the delta is declared rather than chased. A change in it still fails,
# which is the property that mattered.
CONTESTED = {'TJ': -1}

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from snapshot import snapshot  # noqa: E402


def fixture_countries():
    with open(os.path.join(FIXTURE, 'scan-stats.json'), encoding='utf-8') as handle:
        return json.load(handle)['countries']


class FixtureBuild(unittest.TestCase):
    """One build shared by every assertion below -- it takes about a second,
    and running it per-test would say nothing extra."""

    @classmethod
    def setUpClass(cls):
        cls.countries = fixture_countries()
        cls.out = tempfile.mkdtemp(prefix='fixture-build-')
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, 'dump_build.py'),
             '--scan-dir', FIXTURE, '--out-dir', cls.out,
             '--countries', ','.join(cls.countries)],
            capture_output=True, text=True, cwd=ROOT)
        if result.returncode:
            raise AssertionError('the fixture build failed:\n%s\n%s'
                                 % (result.stdout, result.stderr))
        cls.log = result.stdout
        cls.built = snapshot(cls.out)
        with open(REFERENCE, encoding='utf-8') as handle:
            cls.reference = json.load(handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_every_country_matches_the_full_build_byte_for_byte(self):
        for code in self.countries:
            with self.subTest(country=code):
                expected = self.reference['countries'].get(code)
                self.assertIsNotNone(expected, '%s is not in the reference build' % code)
                actual = self.built['countries'].get(code)
                self.assertIsNotNone(actual, '%s was not built from the fixture' % code)
                extra = CONTESTED.get(code)
                if extra is not None:
                    self.assertEqual(expected['cities'] + extra, actual['cities'],
                                     '%s: the known cross-border delta changed' % code)
                    self.assertEqual(expected['regions'], actual['regions'])
                    continue
                for field in sorted(expected):
                    self.assertEqual(expected[field], actual.get(field),
                                     '%s: %s differs from the full build' % (code, field))

    def test_builds_exactly_the_countries_asked_for(self):
        self.assertEqual(sorted(self.countries), sorted(self.built['countries']))

    def test_the_fallback_tiers_still_run(self):
        # VA has no "<ISO2>-" division tier and must reach tier 2; without it
        # the country ships with no regions at all.
        self.assertIn('countries have no P300 tier', self.log)
        self.assertIn('tier 2', self.log)

    def test_the_attachment_fallback_still_fires(self):
        # NU is the case that set the floor at five stranded settlements.
        self.assertIn('attached almost nothing', self.log)
        self.assertIn('NU', self.log.split('attached almost nothing')[1].split('\n')[0])

    def test_romanised_names_ship_and_keep_their_original(self):
        """Tajikistan is in the fixture for this. A hash comparison would catch
        a change here, but not tell anyone what broke; this says it."""
        path = os.path.join(self.out, 'data', 'TJ.ndjson')
        romanised = 0
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                row = json.loads(line)
                if row.get('type') != 'settlement':
                    continue
                cyrillic = [n for names in (row.get('alt_names') or {}).values()
                            for n in names if any('Ѐ' <= c <= 'ӿ' for c in n)]
                if not cyrillic:
                    continue
                romanised += 1
                self.assertTrue(row['name'].isascii(),
                                '%s should have been romanised' % row['name'])
                self.assertTrue(row['slug'], 'a romanised name must still slug')
        self.assertGreater(romanised, 100,
                           'TJ should carry hundreds of romanised names; if this '
                           'is zero the transliteration silently stopped running')

    def test_the_build_is_deterministic(self):
        second = tempfile.mkdtemp(prefix='fixture-build-again-')
        try:
            subprocess.run(
                [sys.executable, os.path.join(ROOT, 'dump_build.py'),
                 '--scan-dir', FIXTURE, '--out-dir', second,
                 '--countries', ','.join(self.countries)],
                capture_output=True, text=True, cwd=ROOT, check=True)
            self.assertEqual(self.built, snapshot(second))
        finally:
            shutil.rmtree(second, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
