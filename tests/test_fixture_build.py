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

import array
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

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

    def test_no_region_contains_the_same_name_twice(self):
        """The guarantee the README leads with, asserted on the built files
        rather than on the function that enforces it. It matters more now that
        settlements move between regions after they are grouped: a row rescued
        into a real division meets that division's names for the first time,
        and resolve_collisions is what has to notice."""
        for code in self.countries:
            with self.subTest(country=code):
                region = None
                names = set()
                path = os.path.join(self.out, 'data', '%s.ndjson' % code)
                with open(path, encoding='utf-8') as handle:
                    for line in handle:
                        row = json.loads(line)
                        if row.get('type') == 'region':
                            region, names = row['name'], set()
                        elif row.get('type') == 'settlement':
                            self.assertNotIn(row['name'], names,
                                             '%s: %s appears twice in %s'
                                             % (code, row['name'], region))
                            names.add(row['name'])

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


def one_polygon_shapefile(path, code, west, south, east, north):
    """A Natural Earth-shaped zip holding one rectangular division.

    Written by hand rather than shipped as a fixture: the real boundary file is
    15 MB, none of which this needs, and a shapefile is a header and a list of
    doubles. The ring runs anticlockwise on the page and clockwise in shapefile
    terms -- north up the west edge, east along the top -- which is what marks
    it an outer ring rather than a hole.
    """
    ring = [(west, south), (west, north), (east, north), (east, south), (west, south)]
    content = struct.pack('<i', 5) + struct.pack('<4d', west, south, east, north)
    content += struct.pack('<2i', 1, len(ring)) + struct.pack('<i', 0)
    for x, y in ring:
        content += struct.pack('<2d', x, y)
    record = struct.pack('>2i', 1, len(content) // 2) + content
    header = struct.pack('>i', 9994) + b'\x00' * 20
    header += struct.pack('>i', (100 + len(record)) // 2) + struct.pack('<2i', 1000, 5)
    header += struct.pack('<4d', west, south, east, north) + struct.pack('<4d', 0, 0, 0, 0)

    field, width = b'iso_3166_2', 10
    dbf = struct.pack('<4B', 3, 26, 1, 1) + struct.pack('<i', 1)
    dbf += struct.pack('<2H', 32 + 32 + 1, 1 + width) + b'\x00' * 20
    dbf += field.ljust(11, b'\x00') + b'C' + b'\x00' * 4 + bytes([width, 0]) + b'\x00' * 14
    dbf += b'\x0d' + b' ' + code.encode('ascii').ljust(width, b'\x00')

    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ne_10m_admin_1_states_provinces.shp', header + record)
        archive.writestr('ne_10m_admin_1_states_provinces.dbf', dbf)
    return path


def build_fixture(p150_edges=(), boundaries=None, countries='AL'):
    """A build of the fixture with the optional graphs dropped beside it.

    With neither, this is the compatibility case: the scan every published
    release was built from, which has to keep producing what it always did.

    Returns the build's log, its per-country stats, the first ISO-coded
    division of each country, and every case of one region holding two
    settlements of the same name -- which must always be none.
    """
    scan = tempfile.mkdtemp(prefix='fixture-extra-')
    out = tempfile.mkdtemp(prefix='fixture-extra-build-')
    try:
        shutil.copytree(FIXTURE, scan, dirs_exist_ok=True)
        if p150_edges:
            with open(os.path.join(scan, 'graph-p150.i32'), 'wb') as handle:
                array.array('i', p150_edges).tofile(handle)
        command = [sys.executable, os.path.join(ROOT, 'dump_build.py'),
                   '--scan-dir', scan, '--out-dir', out, '--countries', countries]
        if boundaries:
            command += ['--boundaries', boundaries]
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        if result.returncode:
            raise AssertionError('%s\n%s' % (result.stdout, result.stderr))
        with open(os.path.join(out, 'build-stats.json'), encoding='utf-8') as handle:
            stats = json.load(handle)
        divisions = {}
        duplicates = []
        for code in countries.split(','):
            region, names = None, set()
            with open(os.path.join(out, 'data', '%s.ndjson' % code), encoding='utf-8') as handle:
                for line in handle:
                    row = json.loads(line)
                    if row.get('type') == 'region':
                        region, names = row['name'], set()
                        if row.get('iso_3166_2'):
                            divisions.setdefault(code, row['id'])
                    elif row.get('type') == 'settlement':
                        if row['name'] in names:
                            duplicates.append((code, region, row['name']))
                        names.add(row['name'])
        return (result.stdout, {e['code']: e for e in stats['countries']},
                divisions, duplicates)
    finally:
        shutil.rmtree(scan, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def tiny_scan(directory, duplicates=()):
    """A scan for one invented country whose divisions state nothing upward.

    The Faroe Islands in miniature, and the reason tier 4 exists: settlements
    sit in municipalities, municipalities sit in districts, districts state no
    P131 at all, and the only thing tying any of it to the country is P17 --
    which is what the scan shards on, so it is all in one file. A municipality
    is a settlement by this pipeline's rule, so the chain has to be walked
    through it to reach the district above.
    """
    entities = os.path.join(directory, 'entities')
    os.makedirs(entities, exist_ok=True)
    country, district_class, muni_class = 900001, 900002, 900003
    village_class, admin_root, settlement_root = 900004, 56061, 486972
    districts = [910001, 910002]
    p131, p279 = array.array('i'), array.array('i')
    for child, parent in ((muni_class, settlement_root), (village_class, settlement_root),
                          (district_class, admin_root)):
        p279 += array.array('i', [child, parent])

    rows = [{'id': country, 'iso_3166_1': ['ZZ'], 'capital': ['Q910001'],
             'instance_of': ['Q6256'], 'labels': {'en': 'Testland'}}]
    for index, district in enumerate(districts):
        rows.append({'id': district, 'country': 'Q%d' % country,
                     'instance_of': ['Q%d' % district_class],
                     'coord': 'Point(%d.0 50.0)' % (10 + index),
                     'labels': {'en': 'District %d' % (index + 1)}})
        municipality = district + 100
        rows.append({'id': municipality, 'country': 'Q%d' % country,
                     'instance_of': ['Q%d' % muni_class],
                     'located_in': ['Q%d' % district],
                     'coord': 'Point(%d.0 50.0)' % (10 + index),
                     'labels': {'en': 'Municipality %d' % (index + 1)}})
        p131 += array.array('i', [municipality, district])
        for village in range(3):
            qid = municipality * 10 + village
            rows.append({'id': qid, 'country': 'Q%d' % country,
                         'instance_of': ['Q%d' % village_class],
                         'located_in': ['Q%d' % municipality],
                         'coord': 'Point(%d.%d 50.0)' % (10 + index, village + 1),
                         'labels': {'en': 'Village %d%d' % (index + 1, village + 1)}})
            p131 += array.array('i', [qid, municipality])

    # Rows upstream says are the same thing as one already in the file. Each is
    # a settlement with a country, a class, a coordinate and P460 -- and no
    # containment whatsoever, which is the shape of every real one: "Warszawa"
    # beside Warsaw, "Cochin" beside Kochi.
    for index, (qid, target, located) in enumerate(duplicates):
        row = {'id': qid, 'country': 'Q%d' % country,
               'instance_of': ['Q%d' % village_class],
               'same_as': ['Q%d' % target],
               'coord': 'Point(10.9 50.0)',
               'labels': {'en': 'Duplicate %d' % (index + 1)}}
        if located:
            row['located_in'] = ['Q%d' % located]
            p131 += array.array('i', [qid, located])
        rows.append(row)

    with open(os.path.join(entities, '%d.jsonl' % country), 'w', encoding='utf-8') as out:
        for row in rows:
            if row['id'] != country:
                out.write(json.dumps(row, sort_keys=True) + '\n')
    with open(os.path.join(entities, '0.jsonl'), 'w', encoding='utf-8') as out:
        out.write(json.dumps(rows[0], sort_keys=True) + '\n')
    for filename, edges in (('graph-p131.i32', p131), ('graph-p279.i32', p279)):
        with open(os.path.join(directory, filename), 'wb') as out:
            edges.tofile(out)
    return directory


class DivisionsTheCountryNeverClaims(unittest.TestCase):
    """Tier 4: the divisions a country's settlements point at, when nothing
    points at the country."""

    def build(self):
        scan = tempfile.mkdtemp(prefix='tiny-scan-')
        out = tempfile.mkdtemp(prefix='tiny-build-')
        try:
            tiny_scan(scan)
            result = subprocess.run(
                [sys.executable, os.path.join(ROOT, 'dump_build.py'),
                 '--scan-dir', scan, '--out-dir', out, '--countries', 'ZZ'],
                capture_output=True, text=True, cwd=ROOT)
            if result.returncode:
                raise AssertionError('%s\n%s' % (result.stdout, result.stderr))
            regions = []
            with open(os.path.join(out, 'data', 'ZZ.ndjson'), encoding='utf-8') as handle:
                for line in handle:
                    row = json.loads(line)
                    if row.get('type') == 'region':
                        regions.append([row['name'], row['id'], 0])
                    elif row.get('type') == 'settlement':
                        regions[-1][2] += 1
            return result.stdout, regions
        finally:
            shutil.rmtree(scan, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)

    def test_the_districts_become_the_regions(self):
        log, regions = self.build()
        self.assertIn('tier 4', log)
        self.assertEqual([name for name, _, _ in regions], ['District 1', 'District 2'])
        # Three villages and the municipality itself, which ships as a city in
        # its own district -- a municipality is a settlement here, which is the
        # same reason it is not eligible to be the district.
        self.assertEqual([count for _, _, count in regions], [4, 4])

    def test_the_country_is_not_one_of_them(self):
        """Tier 5 is what this replaces: one region named after the country,
        holding everything."""
        _log, regions = self.build()
        self.assertNotIn('Testland', [name for name, _, _ in regions])

    def test_a_municipality_is_walked_through_and_not_selected(self):
        """It is a settlement by this pipeline's own rule, and selecting it
        would put a place inside a place."""
        _log, regions = self.build()
        self.assertEqual([], [name for name, _, _ in regions if name.startswith('Municipality')])


class SaidToBeTheSameAs(unittest.TestCase):
    """P460: one row per place, decided by which side has a division."""

    VILLAGE = 9101010          # Village 11, placed in District 1

    def build(self, duplicates):
        scan = tempfile.mkdtemp(prefix='tiny-dup-scan-')
        out = tempfile.mkdtemp(prefix='tiny-dup-build-')
        try:
            tiny_scan(scan, duplicates)
            result = subprocess.run(
                [sys.executable, os.path.join(ROOT, 'dump_build.py'),
                 '--scan-dir', scan, '--out-dir', out, '--countries', 'ZZ'],
                capture_output=True, text=True, cwd=ROOT)
            if result.returncode:
                raise AssertionError('%s\n%s' % (result.stdout, result.stderr))
            names, regions = [], []
            with open(os.path.join(out, 'data', 'ZZ.ndjson'), encoding='utf-8') as handle:
                for line in handle:
                    row = json.loads(line)
                    if row.get('type') == 'region':
                        regions.append(row['name'])
                    elif row.get('type') == 'settlement':
                        names.append(row['name'])
            with open(os.path.join(out, 'build-stats.json'), encoding='utf-8') as handle:
                stats = json.load(handle)
            return result.stdout, names, regions, stats['countries'][0]
        finally:
            shutil.rmtree(scan, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)

    def test_the_row_with_no_division_is_the_one_dropped(self):
        log, names, regions, stats = self.build([(9200001, self.VILLAGE, None)])
        self.assertNotIn('Duplicate 1', names)
        self.assertEqual(stats['said_to_be_a_duplicate'], 1)
        self.assertIn('said to be the same as one that does', log)
        self.assertNotIn('Testland', regions,
                         'the duplicate should not have opened a country-named region')

    def test_a_duplicate_that_has_a_division_of_its_own_is_kept(self):
        """P460 alone decides nothing. Both rows reach a division, so which one
        upstream meant is not this pipeline's to guess -- Hoya in Lower Saxony
        is said to be the same as La Hoya in Salamanca, 1,778 km away."""
        _log, names, _regions, stats = self.build([(9200001, self.VILLAGE, 9101010 // 10)])
        self.assertIn('Duplicate 1', names)
        self.assertEqual(stats['said_to_be_a_duplicate'], 0)

    def test_a_duplicate_of_something_equally_unplaced_is_kept(self):
        """Neither side has evidence, so there is no basis for choosing."""
        _log, names, _regions, stats = self.build(
            [(9200001, 9200002, None), (9200002, 9200001, None)])
        self.assertIn('Duplicate 1', names)
        self.assertIn('Duplicate 2', names)
        self.assertEqual(stats['said_to_be_a_duplicate'], 0)

    def test_nothing_is_dropped_when_nothing_says_so(self):
        _log, names, _regions, stats = self.build([])
        self.assertEqual(stats['said_to_be_a_duplicate'], 0)
        self.assertEqual(len(names), 8)


class ContainsRescue(unittest.TestCase):
    """The P150 rescue, end to end, over a fixture whose scan predates the
    property -- as every scan that produced a published release does.

    Albania supplies the shape: 308 of its settlements ship in the region named
    after the country, 59 of them reaching no further than Q236845. One
    synthesised edge saying a selected county contains Q236845 moves exactly
    those 59, and the same edge naming a Maltese division moves none of them.
    """

    PARENT = 236845
    AL_IN_COUNTRY_REGION = 308

    def test_a_scan_with_no_p150_graph_builds_as_it_always_did(self):
        _log, stats, _divisions, _duplicates = build_fixture()
        self.assertEqual(stats['AL']['country_region'], self.AL_IN_COUNTRY_REGION,
                         'the fixture moved; every number in this class reads off it')

    def test_a_division_claiming_a_parent_empties_that_much_of_the_bucket(self):
        _log, _stats, divisions, _duplicates = build_fixture()
        log, stats, _, duplicates = build_fixture([self.PARENT, divisions['AL']])
        self.assertEqual(stats['AL']['country_region'], 250)
        self.assertIn('reached a division only because a parent listed them', log)
        # 59 rows meet a division's existing names for the first time here, and
        # two of them share one. The rule that keeps a region's names unique
        # has to run over the region they arrive in, not the bucket they left.
        self.assertEqual(duplicates, [],
                         'a rescued settlement duplicated a name already in its '
                         'new region')

    def test_an_edge_from_another_country_is_refused(self):
        _log, _stats, divisions, _duplicates = build_fixture(countries='AL,MT')
        log, stats, _, _ = build_fixture([self.PARENT, divisions['MT']], countries='AL,MT')
        self.assertEqual(stats['AL']['country_region'], self.AL_IN_COUNTRY_REGION,
                         'a stated parent must not move a place across a border')
        self.assertNotIn('reached a division only because a parent listed them', log)


class BoundaryPlacement(unittest.TestCase):
    """Natural Earth as the last resort, end to end.

    The rectangle below is not Albania's real geography and is not meant to be
    -- it is one division's code stretched over 109 of the 308 settlements in
    Albania's country-named region, which is enough to prove that a coordinate
    reaches the boundary lookup, that its answer is checked against the
    divisions actually shipped, and that a row arriving in a new region still
    meets the names already there.
    """

    AL_IN_COUNTRY_REGION = 308
    BOX = (19.5, 41.0, 20.5, 42.0)          # west, south, east, north
    # Placed during grouping, before anything is dropped for want of a usable
    # name or a coordinate, which is why three of these never reach a file and
    # the bucket falls by 109 rather than 112.
    IN_BOX = 112
    STILL_IN_BUCKET = 199

    def zip_for(self, code, box=None):
        path = os.path.join(tempfile.mkdtemp(prefix='fixture-ne-'), 'ne.zip')
        return one_polygon_shapefile(path, code, *(box or self.BOX))

    def test_a_coordinate_inside_a_division_places_the_settlement(self):
        _log, _stats, divisions, _duplicates = build_fixture()
        log, stats, _, duplicates = build_fixture(boundaries=self.zip_for('AL-01'))
        self.assertEqual(stats['AL']['placed_by_boundary'], self.IN_BOX)
        self.assertEqual(stats['AL']['country_region'], self.STILL_IN_BUCKET)
        self.assertIn('placed by their coordinates', log)
        self.assertEqual(duplicates, [], 'a placed settlement duplicated a name '
                                         'already in its new region')
        # Moving a row between regions must not create or lose one. It can
        # still change a merge -- two rows that were duplicates in the bucket
        # can end up in different regions -- so this is the count to watch.
        self.assertEqual(stats['AL']['cities'], 3335)
        self.assertTrue(divisions)

    def test_a_code_this_build_does_not_ship_places_nothing_directly(self):
        """Natural Earth carries superseded ISO editions -- Nepal's zones, Ivory
        Coast's old regions -- and a code that names no division here cannot be
        matched to one. This rectangle also spans most of Albania, so the
        learned mapping refuses it too and the bucket is untouched."""
        log, stats, _, _ = build_fixture(boundaries=self.zip_for('AL-99'))
        self.assertEqual(stats['AL']['placed_by_boundary'], 0)
        self.assertEqual(stats['AL']['placed_by_code_map'], 0)
        self.assertEqual(stats['AL']['country_region'], self.AL_IN_COUNTRY_REGION)
        self.assertNotIn('placed by their coordinates', log)

    def test_a_code_this_build_does_not_ship_is_learned_from_what_is_inside_it(self):
        """The other half of the same case. This rectangle sits inside
        Gjirokaster County: 164 settlements the build already placed fall in it
        and 92% of them are in that one county, which is what a nesting looks
        like -- so 'AL-99' is read as meaning Gjirokaster, and the 16 unplaced
        settlements inside it follow."""
        log, stats, _, duplicates = build_fixture(
            boundaries=self.zip_for('AL-99', (19.9, 40.0, 20.4, 40.4)))
        self.assertEqual(stats['AL']['placed_by_code_map'], 16)
        self.assertEqual(stats['AL']['country_region'],
                         self.AL_IN_COUNTRY_REGION - 16)
        self.assertIn('against the divisions this build ships', log)
        self.assertEqual(duplicates, [])

    def test_a_polygon_that_straddles_two_divisions_is_refused(self):
        """The purity floor. This rectangle holds 278 placed settlements and
        only 62% of them agree on a division, which is a boundary disagreement
        rather than a nesting, and nothing is learned from it."""
        log, stats, _, _ = build_fixture(
            boundaries=self.zip_for('AL-99', (19.6, 41.0, 20.0, 41.4)))
        self.assertEqual(stats['AL']['placed_by_code_map'], 0)
        self.assertEqual(stats['AL']['country_region'], self.AL_IN_COUNTRY_REGION)
        self.assertNotIn('against the divisions this build ships', log)

    def test_another_country_s_division_cannot_claim_it(self):
        log, stats, _, _ = build_fixture(boundaries=self.zip_for('MT-01'),
                                         countries='AL,MT')
        self.assertEqual(stats['AL']['placed_by_boundary'], 0)
        self.assertEqual(stats['AL']['country_region'], self.AL_IN_COUNTRY_REGION)
        self.assertNotIn('placed by their coordinates', log)

    def test_a_build_with_no_boundaries_places_nothing_by_coordinate(self):
        _log, stats, _divisions, _duplicates = build_fixture()
        self.assertEqual(stats['AL']['placed_by_boundary'], 0)

    def test_placing_by_coordinate_stays_deterministic(self):
        """The whole release is fingerprinted from its content, so a lookup
        that answered differently between two runs over the same input would
        republish the world."""
        boundaries = self.zip_for('AL-01')
        first = build_fixture(boundaries=boundaries)[1]['AL']
        second = build_fixture(boundaries=boundaries)[1]['AL']
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
