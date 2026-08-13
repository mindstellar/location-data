"""Assembling one country's record, and writing it out.

`build_country` is where the earlier stages meet: the settlements grouped by
containment, the divisions selected for the country, the names resolved for
both, and the country block. It produces the canonical record -- neutral field
names, nothing synthesised, a region with no settlements simply carrying an
empty list -- and `write_canonical_ndjson` streams it.

The consumer-specific formats are not written here. They are generated from the
canonical record by adapter_shopclass, so the two shapes cannot drift.
"""

import hashlib
import json
import os
import re

from build import COUNTRY_NAME_OVERRIDES, coord, mean_coord, remove_accents, slugify
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
        'type': 'country', 'id': country['id'], 'code': country['code'],
        'name': country['name'], 'slug': country['slug'],
    }
    country_line.update({k: v for k, v in country.items()
                         if k not in ('id', 'code', 'name', 'slug', 'regions')})
    lines = [json.dumps(country_line, separators=(',', ':'), ensure_ascii=False)]

    for region in country['regions']:
        line = {
            'type': 'region', 'id': region['id'], 'country_code': region['country_code'],
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
