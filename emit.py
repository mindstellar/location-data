"""Assembling one country's record, and writing it out.

Field names are this dataset's own -- `name`, `latitude`, `place_type` -- and
neutral by intent. A consumer whose schema differs maps them on the way in;
publishing a second copy in someone else's column conventions was tried and
removed, because it doubled the formats to keep consistent and put 518
synthesised places into a reference dataset.

`build_country` is where the earlier stages meet: the settlements grouped by
containment, the divisions selected for the country, the names resolved for
both, and the country block. It produces the canonical record -- neutral field
names, nothing synthesised, a region with no settlements simply carrying an
empty list -- and `write_canonical_ndjson` streams it.

The consumer-specific formats are not written here. They are generated from the
"""

import collections
import csv
import hashlib
import json
import math
import os
import re

from contracts import COUNTRY_NAME_OVERRIDES, coord, mean_coord, remove_accents, slugify

# Which upstream the ids on these rows belong to. A constant today, and that is
# exactly why it is worth writing now: the roadmap adds NGA GNS for the
# coordinates and Arabic names Wikidata lacks, and the moment two sources share
# a row's integer id space without a discriminator you get the defect this
# dataset already caused downstream -- an importer matching a Wikidata QID
# against an unrelated id from another source and overwriting the wrong place.
# Adding a field is safe; changing what an existing one means is a migration.
SOURCE = 'wikidata'
from classify import is_settlement
from countryblock import country_extra, extra_fields, official_language, parse_point
from naming import resolve_name, resolve_name_full, strip_qualifier

# Two settlements in one region carry the same name for two opposite reasons.
#
# Wikidata routinely holds the administrative unit and the built-up place at
# its seat as separate items: "comune of Italy" beside "municipality seat",
# "municipality of Spain" beside "human settlement", "commune of France"
# beside "human settlement". Both pass the settlement test, so both shipped,
# and the pair is one place described twice.
#
# Genuinely different places also share a name -- Germany has two towns called
# Aach, 80 km apart, and Russia two villages called Chekhrak, 25 km apart.
#
# Distance is what separates the two cases. Within this radius the rows are
# treated as one place and merged; beyond it both are kept, because deleting
# one would delete a real village. 2 km is wide enough to cover an item pair
# whose coordinates disagree by a town's width and far narrower than the gap
# between two settlements that merely share a name.
MERGE_KM = 2.0


def _distance_km(first, second):
    """Great-circle distance between two settlement rows."""
    lat1, lng1 = math.radians(float(first['latitude'])), math.radians(float(first['longitude']))
    lat2, lng2 = math.radians(float(second['latitude'])), math.radians(float(second['longitude']))
    half = (math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(half))


# Set by the row itself and never taken from a row it is merged with: an id
# identifies one upstream item, and the name, slug and position are the
# identity the merge is keeping.
_IDENTITY_FIELDS = frozenset(
    ('id', 'source', 'admin1_id', 'country_code', 'name', 'name_lang', 'slug',
     'latitude', 'longitude'))


def _absorb(base, other):
    """Fill base's empty fields from other, in place.

    The two items describe the same place, and upstream rarely fills both the
    same way -- the administrative item tends to carry area and population, the
    settlement item the GeoNames id and the postal code. Dropping one row
    without taking its fields would lose data the dataset already had.
    """
    for key, value in other.items():
        if key in _IDENTITY_FIELDS:
            continue
        if key == 'alt_names':
            names = base.get(key) or {}
            for lang, values in (value or {}).items():
                names[lang] = sorted(set(names.get(lang, [])) | set(values))
            base[key] = names
            continue
        if key == 'native_label':
            base[key] = sorted(set(base.get(key) or []) | set(value or []))
            continue
        current = base.get(key)
        if current is None or current == '' or current == [] or current == {}:
            base[key] = value


def _merge_colocated(rows):
    """Single-link clustering of same-named rows by distance.

    Single-link rather than pairwise: three items for one place can be strung
    out so that the first and last are further apart than the threshold while
    each is close to the middle one. Sorted by id first, so the surviving row
    is the lowest QID -- the same tiebreak the country and division indexes
    use, which is what keeps a rebuild from flipping which id a place has.
    """
    rows = sorted(rows, key=lambda row: row['id'])
    parent = list(range(len(rows)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if _distance_km(rows[i], rows[j]) <= MERGE_KM:
                left, right = find(i), find(j)
                if left != right:
                    parent[left] = right

    clusters = collections.OrderedDict()
    for index, row in enumerate(rows):
        clusters.setdefault(find(index), []).append(row)
    merged = []
    for cluster in clusters.values():
        base = cluster[0]
        for other in cluster[1:]:
            _absorb(base, other)
        merged.append(base)
    return merged


# Compass sectors, for the groups whose rows share one parent so no ancestor
# can separate them. "north Gorakhpur" is true by construction: both rows are
# in Gorakhpur, so the northern one is the northern one, and no coordinate for
# the parent is needed to say it.
_SECTORS = ('north', 'northeast', 'east', 'southeast',
            'south', 'southwest', 'west', 'northwest')


def _sector(row, lat0, lng0):
    """Which eighth of the compass `row` sits in, seen from (lat0, lng0)."""
    lat1, lat2 = math.radians(lat0), math.radians(float(row['latitude']))
    delta = math.radians(float(row['longitude']) - lng0)
    y = math.sin(delta) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    degrees = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _SECTORS[int((degrees + 22.5) // 45) % 8]


# Where the stripped-off upstream qualifier rides between build_country and
# resolve_collisions. Private, and popped before the row can reach a writer:
# write_country_json serialises the whole record, so anything left on it ships.
_UPSTREAM = '_upstream_qualifier'


def _apply(row, qualifier):
    row['name'] = '%s (%s)' % (row['name'], qualifier)
    row['slug'] = slugify(row['name'])


def _qualify(rows, admin2_name, stats, region_qid):
    """Give every row in a group something that says which place it is, and
    drop the ones that cannot be given anything.

    A name that identifies two places identifies neither. It is worse than a
    missing row, because a consumer picking from a list has no way to know the
    choice was ambiguous and will silently take the wrong one. So the rule is
    not "qualify where possible" but "qualify or do not ship".

    Three tiers, in order:

    1. **The parent division.** "Aach" and "Aach (Konstanz)". Usable when the
       parent has a name, that name is not the settlement's own, and no other
       row in the group has the same parent name.

    2. **A compass sector within the parent**, for rows sharing one parent, so
       no ancestor can separate them: "Bankati (north Gorakhpur)". Two villages
       of one name in one district is ordinary -- of the pairs under a shared
       parent, 2,775 are more than 25 km apart -- and merging them would delete
       a real place. Two parents are refused here: one that *is* the region,
       because a sector of the region narrows nothing already chosen, and one
       named after the settlement, where the sector alone says as much.

    3. **Nothing, so the row is dropped.** What is left has no parent at all,
       or two parents with the same name and the same sector.

    Exactly one row keeps the plain name, since one of them is what the name
    usually means. Which one is chosen carefully: the largest, unless a row
    cannot be qualified anyway, in which case that one takes the plain name and
    the rest are qualified around it. Always giving it to the largest left both
    rows bare whenever the other one was the unqualifiable one, which is how
    2,987 groups stayed ambiguous -- a settlement named after its own
    municipality gets no qualifier from it, and Bulgaria has a great many.

    Returns the rows that survive.
    """
    labels = []
    for row in rows:
        reference = row.get('admin2_id')
        label = admin2_name(int(reference[1:])) if reference else None
        labels.append(label)
    shared = collections.Counter(label for label in labels if label)

    # Tier 1 is available to a row whose parent names it distinctly.
    tier1 = [label is not None and label != row['name'] and shared[label] == 1
             for row, label in zip(rows, labels)]

    stranded = [i for i, ok in enumerate(tier1) if not ok]
    if len(stranded) <= 1:
        # One row cannot be qualified, so it is the one that keeps the plain
        # name; if every row can be, the largest keeps it.
        primary = (stranded[0] if stranded else
                   rows.index(min(rows, key=lambda r: (-(r.get('population') or -1), r['id']))))
        for index, row in enumerate(rows):
            if index != primary:
                _apply(row, labels[index])
        return rows

    # Tier 2. Sectors are taken from the centre of the rows that need them, so
    # they describe the spread of exactly those places.
    lat0 = sum(float(rows[i]['latitude']) for i in stranded) / len(stranded)
    lng0 = sum(float(rows[i]['longitude']) for i in stranded) / len(stranded)
    sectors = {}
    for i in stranded:
        if not labels[i]:
            continue
        # A parent that *is* the region gives a sector of the region, which
        # narrows nothing a consumer has not already chosen -- five settlements
        # called Hopewell separated only into east, north, southeast and west
        # Alabama. Those are dropped rather than labelled.
        if rows[i].get('admin2_id') == 'Q%d' % region_qid:
            continue
        # A parent named after the settlement repeats it: "Evergem (south
        # Evergem)", "Drvar (southwest Drvar Municipality)". The sector alone
        # carries everything the pair of them did.
        if labels[i].lower().startswith(rows[i]['name'].lower()):
            sectors[i] = _sector(rows[i], lat0, lng0)
        else:
            sectors[i] = '%s %s' % (_sector(rows[i], lat0, lng0), labels[i])
    counts = collections.Counter(sectors.values())
    resolved = [i for i in stranded if counts.get(sectors.get(i)) == 1]

    # Last resort: whatever upstream had in its brackets before this stripped
    # them off. It is used only where nothing derived from the data works, and
    # it is exactly what the row shipped with before, so a place that cannot be
    # told apart any other way keeps its old name instead of being dropped.
    for i in stranded:
        if i in resolved or not rows[i].get(_UPSTREAM):
            continue
        candidate = rows[i][_UPSTREAM]
        if candidate != rows[i]['name'] and candidate not in sectors.values():
            sectors[i] = candidate
            resolved.append(i)
            stats['upstream_qualifier_kept'] += 1

    # Exactly one row keeps the plain name. Preferably one that could not be
    # qualified anyway; if the sectors covered every stranded row, the largest
    # of them gives its sector up, so a group never comes out with all of its
    # rows qualified and none carrying the name plainly.
    hopeless = [i for i in stranded if i not in resolved]
    keep = min(hopeless or stranded,
               key=lambda i: (-(rows[i].get('population') or -1), rows[i]['id']))

    for index, row in enumerate(rows):
        if index == keep:
            continue
        if tier1[index]:
            _apply(row, labels[index])
        elif index in resolved:
            _apply(row, sectors[index])

    dropped = [i for i in hopeless if i != keep]
    stats['ambiguous_names'] += len(dropped)
    return [row for index, row in enumerate(rows) if index not in set(dropped)]


def resolve_collisions(settlements, admin2_name, stats, region_qid):
    """One name, one place, inside a region.

    Same name and effectively the same position means one place upstream
    described twice, and those are merged. Same name and a real distance apart
    means two places, and those are kept and qualified.

    Nothing here returns two rows a consumer cannot tell apart. That is the
    whole point: a name identifying two places identifies neither, and a list
    offering the same string twice makes a wrong choice look like a right one.
    """
    groups = collections.OrderedDict()
    for settlement in settlements:
        groups.setdefault(settlement['slug'], []).append(settlement)

    resolved = []
    for rows in groups.values():
        if len(rows) == 1:
            resolved.extend(rows)
            continue
        merged = _merge_colocated(rows)
        stats['merged_duplicates'] += len(rows) - len(merged)
        if len(merged) > 1:
            merged = _qualify(merged, admin2_name, stats, region_qid)
        resolved.extend(merged)

    # A qualifier can land on a name that was already in the region, because
    # Wikidata disambiguates some of its own labels and does it differently:
    # "Floq (Klos)" is what this builds and "Floq, Klos" is what upstream
    # shipped, and both slug to floq-klos. Those never met above, since they
    # started in different groups. This is the sweep that makes the guarantee
    # hold rather than nearly hold.
    final = collections.OrderedDict()
    for settlement in resolved:
        final.setdefault(settlement['slug'], []).append(settlement)
    unique = []
    for rows in final.values():
        if len(rows) == 1:
            unique.extend(rows)
        else:
            stats['ambiguous_names'] += len(rows) - 1
            unique.append(min(rows, key=lambda r: (-(r.get('population') or -1), r['id'])))
    # Popped here and nowhere else. write_country_json serialises the whole
    # record, so a private key left on a row ships in the published data.
    for settlement in unique:
        settlement.pop(_UPSTREAM, None)
    return unique


def build_country(iso2, country_qid, shard_files, admin1_selected, admin1_records,
                  assign, settlement_classes, lang_codes, country_records, stats,
                  mode='admin1', refs=None, single_zone=None, exclude_classes=None,
                  coarse=None, admin2_records=None):
    """One country's canonical record, in the shape build_canonical_country()
    produces: neutral field names, nothing synthesised, and a region with no
    settlements is simply a region with an empty list.
    """
    refs = refs or {}
    single_zone = single_zone or {}
    timezone_id = single_zone.get(iso2.upper())
    country_record = country_records.get(country_qid)
    native_lang = official_language(country_record, lang_codes)
    stats['native_lang'] = native_lang or ''

    country_name = None
    if country_record:
        country_name, _lang = resolve_name(country_record, native_lang)
    country_name = COUNTRY_NAME_OVERRIDES.get(iso2, country_name or iso2)

    coarse = coarse or {}
    by_region = {qid: [] for qid in admin1_selected}
    # A coarse division becomes a region only if something actually attached
    # to it, so a country whose leaf tier works keeps exactly the regions it
    # had before.
    used_coarse = {}
    # Tier 4 has no admin-1 to contain anything, so containment is not
    # consulted: every settlement the country's own P17 claims lands in the
    # single synthesised region. Its id is the country's own QID, which is
    # unique across every Wikidata entity type and so cannot collide with a
    # real division id.
    single_region = next(iter(admin1_selected)) if mode == 'country' else None
    seen = 0

    written = set()
    for shard_path in shard_files:
        if not os.path.exists(shard_path):
            continue
        with open(shard_path, encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                if not is_settlement(record, settlement_classes, exclude_classes):
                    continue
                if record['id'] in written:
                    continue
                written.add(record['id'])
                seen += 1
                if single_region is not None:
                    admin1_qid = single_region
                else:
                    admin1_qid = assign.get(record['id'])
                    # Nothing contains it, but its country does claim it, so
                    # the country-level region is a better answer than
                    # dropping it. Same reasoning as the coarse fallback below,
                    # one step further out: a place that is findable under its
                    # country beats a place that is not in the dataset.
                    if (admin1_qid not in by_region and admin1_qid not in coarse
                            and country_qid in coarse):
                        # Either nothing contains it, or what does belongs to
                        # another country. Its own country-level region is the
                        # better answer than dropping it.
                        admin1_qid = country_qid
                    if admin1_qid is not None and admin1_qid in coarse and admin1_qid not in by_region:
                        by_region[admin1_qid] = []
                        used_coarse[admin1_qid] = coarse[admin1_qid]
                    if admin1_qid is None or admin1_qid not in by_region:
                        stats['orphan'] += 1
                        continue
                name, name_lang, romanised_from = resolve_name_full(record, native_lang)
                if not name or re.match(r'^Q\d+$', name):
                    stats['no_label'] += 1
                    continue
                lat, lng = parse_point(record.get('coord'))
                lat_s, lng_s = coord(lat), coord(lng)
                if lat_s is None or lng_s is None:
                    stats['no_coord'] += 1
                    continue
                clean = remove_accents(name).strip()
                if not clean or not slugify(clean) or not any(c.isalpha() for c in clean):
                    stats['no_label'] += 1
                    continue
                # Upstream's own disambiguation comes off here and
                # resolve_collisions puts back whatever is actually needed, in
                # this dataset's one format. What was in the brackets is kept
                # on the row as a last resort for a place nothing else can
                # tell apart, and never reaches a writer -- resolve_collisions
                # pops it.
                clean, upstream_qualifier = strip_qualifier(clean)
                if upstream_qualifier:
                    stats['stripped_qualifiers'] += 1
                settlement = {
                    'id': record['id'],
                    'source': SOURCE,
                    'admin1_id': admin1_qid,
                    'country_code': iso2,
                    'name': clean,
                    'name_lang': name_lang,
                    'slug': slugify(clean),
                    'latitude': lat_s,
                    'longitude': lng_s,
                }
                settlement.update(extra_fields(record, native_lang, timezone_id,
                                               (name_lang, romanised_from) if romanised_from else None))
                settlement[_UPSTREAM] = upstream_qualifier
                by_region[admin1_qid].append(settlement)

    # Resolved lazily and only for a region that actually has a collision:
    # 148,000 divisions carry an admin-2 name and a few thousand are ever
    # needed to tell two rows apart.
    admin2_records = admin2_records or {}
    admin2_cache = {}

    def admin2_name(qid):
        if qid not in admin2_cache:
            record = admin2_records.get(qid)
            name, _lang = resolve_name(record, native_lang) if record else (None, None)
            admin2_cache[qid] = (remove_accents(name).strip() or None) if name else None
        return admin2_cache[qid]

    for admin1_qid in list(by_region):
        by_region[admin1_qid] = resolve_collisions(by_region[admin1_qid],
                                                   admin2_name, stats, admin1_qid)

    regions = []
    emit = dict(admin1_selected)
    emit.update(used_coarse)
    stats['coarse_regions'] = len(used_coarse)
    for admin1_qid, iso_code in sorted(emit.items()):
        record = admin1_records.get(admin1_qid) or {}
        name, name_lang, romanised_from = resolve_name_full(record, native_lang)
        if mode == 'country':
            name, name_lang = country_name, None
        if not name:
            stats['region_no_label'] += 1
            continue
        name = remove_accents(name) or name
        # id as a tiebreaker: two settlements can genuinely share a name, so
        # name alone is not a stable sort key.
        settlements = sorted(by_region[admin1_qid], key=lambda s: (s['name'], s['id']))
        lat, lng = parse_point(record.get('coord'))
        lat_s, lng_s = coord(lat), coord(lng)
        if lat_s is None or lng_s is None:
            lat_s, lng_s = mean_coord([(s['latitude'], s['longitude']) for s in settlements])
        if not settlements:
            stats['empty_regions'] += 1

        region = {
            'id': admin1_qid,
            'source': SOURCE,
            'country_code': iso2,
            'name': name,
            'slug': slugify(name),
            'name_lang': name_lang,
            'iso_3166_2': iso_code,
            'latitude': lat_s,
            'longitude': lng_s,
        }
        region.update(extra_fields(record, native_lang, timezone_id,
                                   (name_lang, romanised_from) if romanised_from else None))
        region['settlements'] = settlements
        regions.append(region)

    country = {
        'id': country_qid,
        'source': SOURCE,
        'code': iso2,
        'name': country_name,
        'slug': slugify(country_name),
    }
    country.update(country_extra(iso2, country_record, refs, native_lang, single_zone))
    country['regions'] = sorted(regions, key=lambda r: (r['name'], r['id']))
    return country


def write_canonical_ndjson(country, data_dir):
    """One country line, then each region line immediately followed by its
    settlement lines. Same streaming shape the SPARQL pipeline writes."""
    filename = '%s.ndjson' % country['code']
    extra_keys = ('population', 'place_type', 'geonames_id', 'timezone', 'timezone_id',
                  'postal_code', 'elevation', 'native_label', 'alt_names', 'area',
                  'osm_relation_id', 'capital_of', 'sitelinks', 'admin2_id')

    country_line = {
        'type': 'country', 'id': country['id'], 'source': country['source'],
        'code': country['code'],
        'name': country['name'], 'slug': country['slug'],
    }
    country_line.update({k: v for k, v in country.items()
                         if k not in ('id', 'source', 'code', 'name', 'slug', 'regions')})
    lines = [json.dumps(country_line, separators=(',', ':'), ensure_ascii=False)]

    for region in country['regions']:
        line = {
            'type': 'region', 'id': region['id'], 'source': region['source'],
            'country_code': region['country_code'],
            'name': region['name'], 'slug': region['slug'], 'name_lang': region['name_lang'],
            'iso_3166_2': region['iso_3166_2'],
            'latitude': region['latitude'], 'longitude': region['longitude'],
        }
        line.update({k: region[k] for k in extra_keys})
        lines.append(json.dumps(line, separators=(',', ':'), ensure_ascii=False))
        for settlement in region['settlements']:
            row = {'type': 'settlement'}
            row.update(settlement)
            lines.append(json.dumps(row, separators=(',', ':'), ensure_ascii=False))

    payload = '\n'.join(lines) + '\n'
    with open(os.path.join(data_dir, filename), 'w', encoding='utf-8') as out:
        out.write(payload)
    encoded = payload.encode('utf-8')
    return filename, hashlib.sha256(encoded).hexdigest(), len(encoded)


# --- the neutral distribution ------------------------------------------------
#
# The same canonical record as data/<CC>.ndjson, in the two shapes a consumer
# who is not streaming will reach for. Named by ISO code alone: json/MT.json
# rather than json/MT-Malta.json, because a country name is a mutable thing --
# COUNTRY_NAME_OVERRIDES exists precisely because upstream spellings move -- and
# a renamed country must not silently change its URL.

NEUTRAL_CSV_HEADER = [
    'country_code', 'country_name',
    'region_id', 'region_name', 'region_slug',
    'id', 'name', 'slug', 'latitude', 'longitude',
    'population', 'place_type', 'timezone_id', 'source',
]


def write_country_json(country, json_dir):
    """One country as a nested document. Returns (filename, sha256, bytes).

    Compact, not pretty-printed. Nobody opens a 91 MB file in an editor, and
    indenting Mexico costs 60 MB -- 40% of the file -- to no one's benefit.
    Sorted keys so two runs over the same data produce the same bytes.
    """
    filename = '%s.json' % country['code']
    payload = json.dumps(country, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'))
    with open(os.path.join(json_dir, filename), 'w', encoding='utf-8') as out:
        out.write(payload)
    encoded = payload.encode('utf-8')
    return filename, hashlib.sha256(encoded).hexdigest(), len(encoded)


def write_country_csv(country, csv_dir):
    """One row per settlement, flat. Returns (filename, sha256, bytes)."""
    filename = '%s.csv' % country['code']
    path = os.path.join(csv_dir, filename)
    with open(path, 'w', encoding='utf-8', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(NEUTRAL_CSV_HEADER)
        for region in country['regions']:
            for row in region['settlements']:
                writer.writerow([
                    country['code'], country['name'],
                    region['id'], region['name'], region['slug'],
                    row['id'], row['name'], row['slug'],
                    row['latitude'], row['longitude'],
                    row['population'] if row['population'] is not None else '',
                    row['place_type'] or '',
                    row['timezone_id'] or '',
                    row['source'],
                ])
    with open(path, 'rb') as handle:
        encoded = handle.read()
    return filename, hashlib.sha256(encoded).hexdigest(), len(encoded)
