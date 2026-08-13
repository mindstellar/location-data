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

import csv
import hashlib
import json
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


def build_country(iso2, country_qid, shard_files, admin1_selected, admin1_records,
                  assign, settlement_classes, lang_codes, country_records, stats,
                  mode='admin1', refs=None, single_zone=None, exclude_classes=None,
                  coarse=None):
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
                    if admin1_qid is None and country_qid in coarse:
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
