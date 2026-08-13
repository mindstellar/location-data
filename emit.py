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
from naming import resolve_name, resolve_name_full

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


def _qualify(rows, admin2_name, stats):
    """Name the division below the region, so two places that really do share
    a name in one region can be told apart.

    "Aach" and "Aach" in Baden-Wurttemberg are indistinguishable in a list;
    "Aach" and "Aach (Konstanz)" are not. The qualifier is the P131 parent --
    the district or municipality -- which is how Wikidata disambiguates these
    itself.

    The largest of the group keeps the bare name. Qualifying every row instead
    reads well until it reaches a place anyone has heard of: Colombia has a
    village of 61,549 called Bogota in the same region as the capital, and
    Lithuania a hamlet called Vilnius, and qualifying all of them renamed
    "Bogota" to "Bogota (Cundinamarca Department)" and "Vilnius" to "Vilnius
    (Vilnius City Municipality)". Both then failed the check that a country
    contains its own capital, which is exactly what that check is for. A bare
    name is the answer to "which place does this name usually mean"; the
    qualifiers answer "where is the other one".

    Population decides, lowest QID breaking ties, so a rebuild cannot flip
    which row keeps the plain name.

    A row is qualified only when its parent has a name that no other row in the
    group shares, so the qualifier always distinguishes. Where it cannot, the
    bare name is left and counted: a qualifier that does not qualify is noise,
    and a wrong one is worse than none.
    """
    primary = min(rows, key=lambda row: (-(row.get('population') or -1), row['id']))
    labels = []
    for row in rows:
        reference = row.get('admin2_id')
        label = admin2_name(int(reference[1:])) if reference else None
        labels.append(label if label and label != row['name'] else None)
    counts = collections.Counter(label for label in labels if label)
    for row, label in zip(rows, labels):
        if row is primary:
            continue
        if label is None or counts[label] > 1:
            stats['ambiguous_names'] += 1
            continue
        row['name'] = '%s (%s)' % (row['name'], label)
        row['slug'] = slugify(row['name'])


def resolve_collisions(settlements, admin2_name, stats):
    """One name, one place, inside a region.

    Same name and effectively the same position means one place upstream
    described twice, and those are merged. Same name and a real distance apart
    means two places, and those are kept and qualified.
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
            _qualify(merged, admin2_name, stats)
        resolved.extend(merged)
    return resolved


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
                                                   admin2_name, stats)

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
