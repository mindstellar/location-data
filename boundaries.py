"""Where a coordinate falls, for the settlements containment cannot place.

The last resort, and the only thing here that Wikidata does not decide. A
settlement whose P131 says nothing -- 863 of India's 966 unplaced rows carry no
containment statement at all, only a country and a point -- cannot be reached
by any walk of the containment graph in either direction. It can still be
placed, because it has coordinates and divisions have boundaries.

The boundaries are **Natural Earth** admin-1, which is public domain: no
attribution, no share-alike, nothing that attaches an obligation to a table of
city names. That is the same bar the IANA time zone database clears, and it is
the reason no other boundary set is used -- OpenStreetMap-derived data is ODbL
and GeoNames is CC-BY, and either would put a licence on the output.

Two things keep this narrow:

  * It answers with an **ISO 3166-2 code**, never with a place. The caller looks
    that code up among the divisions it already selected for the country and
    drops the answer if it is not one of them, so this can neither invent a
    division nor move a settlement into a country it does not belong to.
  * It runs **only where containment reached nothing**. A stated parent always
    wins over a polygon, because a boundary file is a rendering of an
    administrative fact rather than the fact itself.

No dependency. The shapefile and dBASE formats are both fixed-width binary and
the whole reader is a hundred lines; a GIS stack for two rectangles' worth of
parsing would be a much larger thing to take on. Ray casting likewise: the
point-in-polygon test is the textbook one, and the only subtlety is that a
division's outer rings and its holes are told apart by winding order, which is
what the shapefile spec guarantees.

Usage:
    boundaries = Admin1Boundaries('ne_10m_admin_1_states_provinces.zip')
    boundaries.code_at(29.189069, 73.209678)     # -> 'IN-RJ'
"""

import array
import struct
import zipfile

# Shapefile shape types. Only the polygon ones can bound a division; anything
# else in the file is skipped rather than guessed at.
POLYGON = 5
POLYGON_Z = 15
POLYGON_M = 25
POLYGON_TYPES = frozenset((POLYGON, POLYGON_Z, POLYGON_M))

# The index cell, in degrees. One degree buckets ~4,600 polygons into a few
# thousand cells and leaves a handful of candidates per lookup, which is the
# whole point: without it every lookup tests every polygon in the world.
CELL = 1.0


def _dbf_fields(data):
    """Field name -> (offset, length) over a dBASE III record, plus the record
    size. The header is fixed-width and the field descriptors are 32 bytes
    each, terminated by 0x0D."""
    header_len, record_len = struct.unpack('<HH', data[8:12])
    fields = {}
    offset = 1                      # byte 0 of a record is the deletion flag
    position = 32
    while position < header_len - 1 and data[position] != 0x0D:
        name = data[position:position + 11].split(b'\x00')[0].decode('ascii', 'replace')
        length = data[position + 16]
        fields[name.lower()] = (offset, length)
        offset += length
        position += 32
    return fields, record_len, header_len


def _dbf_column(data, name):
    """One column of a dBASE file, as a list of stripped strings.

    Reads the whole column rather than the whole table: Natural Earth's admin-1
    dBASE is 15 MB, almost all of it name translations into 30-odd languages,
    and this needs exactly one field out of 120.
    """
    fields, record_len, header_len = _dbf_fields(data)
    if name not in fields:
        raise ValueError('no %r column in the boundary file; its columns are %s'
                         % (name, ', '.join(sorted(fields))))
    offset, length = fields[name]
    count = (len(data) - header_len) // record_len
    out = []
    for i in range(count):
        start = header_len + i * record_len + offset
        # dBASE pads with spaces, Natural Earth pads with NULs, and both
        # appear in this one file.
        out.append(data[start:start + length].decode('utf-8', 'replace').strip(' \x00'))
    return out


def _rings(record):
    """One polygon record as (bbox, [(is_hole, coordinates), ...]).

    Returns (None, []) for a record that is not a polygon at all. Winding is
    decided here rather than at lookup time: it is a property of the ring, and
    a lookup that recomputed it would walk every coordinate twice.
    """
    shape_type, = struct.unpack('<i', record[:4])
    if shape_type not in POLYGON_TYPES:
        return None, []
    west, south, east, north = struct.unpack('<4d', record[4:36])
    num_parts, num_points = struct.unpack('<2i', record[36:44])
    starts = struct.unpack('<%di' % num_parts, record[44:44 + 4 * num_parts])
    points = array.array('d')
    points.frombytes(record[44 + 4 * num_parts:44 + 4 * num_parts + 16 * num_points])
    rings = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else num_points
        ring = points[start * 2:end * 2]
        rings.append((_is_hole(ring), ring))
    return (west, south, east, north), rings


def _inside(ring, lng, lat):
    """Ray casting, counting crossings of a horizontal ray to the east."""
    inside = False
    count = len(ring)
    j = count - 2
    for i in range(0, count, 2):
        yi, yj = ring[i + 1], ring[j + 1]
        if (yi > lat) != (yj > lat):
            xi, xj = ring[i], ring[j]
            if lng < xi + (lat - yi) * (xj - xi) / (yj - yi):
                inside = not inside
        j = i
    return inside


def _is_hole(ring):
    """Shapefile winding: an outer ring is clockwise, a hole anticlockwise.

    Measured by the signed area, which is twice the shoelace sum. Enclaves are
    the reason this is read at all -- without it every hole would answer for
    the division it was cut out of.
    """
    total = 0.0
    count = len(ring)
    j = count - 2
    for i in range(0, count, 2):
        total += (ring[j] - ring[i]) * (ring[j + 1] + ring[i + 1])
        j = i
    return total > 0


# What it takes to believe a learned code translation. Support first: fewer
# than this many placed settlements inside one polygon is not evidence, it is a
# coincidence. Purity second: the settlements inside a polygon must nearly all
# agree on which division they are in, which is what a nesting looks like --
# and what a genuine boundary disagreement does not.
MIN_SUPPORT = 5
MIN_PURITY = 0.90


def learn_code_map(samples, min_support=MIN_SUPPORT, min_purity=MIN_PURITY):
    """Natural Earth's ISO code -> this build's division, learned from the
    settlements already placed.

    Natural Earth carries whichever ISO 3166-2 edition it was last updated
    against, and the root-most rule here picks a level of its own, so the two
    vocabularies often describe the same country at different levels or in
    different editions. France is the clearest: Natural Earth gives
    departements (FR-69, FR-75, FR-13) and this dataset ships the 2016 regions
    (FR-ARA, FR-IDF, FR-PAC). Poland is the same country in two ISO editions,
    lettered voivodeships against numbered ones. Neither can be matched by
    string equality, and both are exact nestings.

    So the mapping is read off the data rather than written down: every
    settlement whose division containment already established is a vote for
    "this polygon means that division". A departement's settlements are
    unanimous about their region, which is what a nesting produces; two
    genuinely different divisions of the same ground are not, which is what the
    purity floor refuses. Nothing is hand-maintained and nothing needs updating
    when ISO or Natural Earth moves -- the vote simply comes out differently.

    `samples` is an iterable of (code, division). Returns code -> division.
    Ties break on the lowest division id, the same rule the country and
    division indexes use, so a rebuild cannot flip an answer.
    """
    votes = {}
    for code, division in samples:
        counts = votes.setdefault(code, {})
        counts[division] = counts.get(division, 0) + 1
    learned = {}
    for code, counts in votes.items():
        total = sum(counts.values())
        if total < min_support:
            continue
        division, best = min(((d, n) for d, n in counts.items()),
                             key=lambda pair: (-pair[1], pair[0]))
        if best / total >= min_purity:
            learned[code] = division
    return learned


class Admin1Boundaries:
    """Natural Earth admin-1 polygons, indexed for point lookup by ISO code.

    Divisions with no ISO 3166-2 code are dropped on the way in: an answer this
    class cannot express as a code is an answer its caller cannot check.
    """

    def __init__(self, path, code_field='iso_3166_2'):
        self.codes = []
        self.boxes = []
        self.rings = []
        self.cells = {}
        self._load(path, code_field)

    def _load(self, path, code_field):
        with zipfile.ZipFile(path) as archive:
            names = {name.rsplit('.', 1)[-1].lower(): name for name in archive.namelist()
                     if '.' in name}
            for extension in ('shp', 'dbf'):
                if extension not in names:
                    raise ValueError('%s holds no .%s' % (path, extension))
            codes = _dbf_column(archive.read(names['dbf']), code_field)
            shp = archive.read(names['shp'])

        position = 100                      # past the file header
        index = 0
        while position < len(shp):
            length, = struct.unpack('>i', shp[position + 4:position + 8])
            record = shp[position + 8:position + 8 + length * 2]
            position += 8 + length * 2
            code = codes[index] if index < len(codes) else ''
            index += 1
            # "-99" is Natural Earth's own null, and a trailing "~" marks a
            # code it invented where ISO has none -- "FO-X00~". Neither can be
            # matched against a division this dataset ships, so neither is kept.
            if not code or code.startswith('-') or '-' not in code or '~' in code:
                continue
            box, rings = _rings(record)
            if not rings:
                continue
            slot = len(self.codes)
            self.codes.append(code)
            self.boxes.append(box)
            self.rings.append(rings)
            west, south, east, north = box
            for cell_x in range(int(west // CELL), int(east // CELL) + 1):
                for cell_y in range(int(south // CELL), int(north // CELL) + 1):
                    self.cells.setdefault((cell_x, cell_y), []).append(slot)

    def __len__(self):
        return len(self.codes)

    def countries(self):
        """The ISO 3166-1 codes the boundaries can answer for, as a set."""
        return {code.split('-')[0] for code in self.codes}

    def code_at(self, lat, lng):
        """The ISO 3166-2 code of the division containing this point, or None.

        Outer rings and holes are counted against each other rather than the
        first hit winning, because one record can hold both and in either
        order: a point in a hole is outside, and a point in an enclave drawn
        inside that hole -- an outer ring again -- is back inside.
        """
        for slot in self.cells.get((int(lng // CELL), int(lat // CELL)), ()):
            west, south, east, north = self.boxes[slot]
            if not (west <= lng <= east and south <= lat <= north):
                continue
            depth = 0
            for is_hole, ring in self.rings[slot]:
                if _inside(ring, lng, lat):
                    depth += -1 if is_hole else 1
            if depth > 0:
                return self.codes[slot]
        return None
