"""The per-entity and per-country fact blocks, and the small parsers under them.

Two shapes live here. `extra_fields` is the block every settlement and region
carries; `country_extra` is the block a country carries, which is where the
facts the upstream ODbL dataset had and this pipeline used to drop -- currency,
capital, continent, calling code, demonym -- are assembled.

Both are shared with the SPARQL extractor's output shape on purpose: the two
pipelines must produce the same canonical record or the comparison between them
means nothing.

Nothing here takes on a licence. The flag and the ccTLD are computed from the
ISO code, and the timezone comes from the IANA database, which is public
domain and already on the machine.
"""

import re

from build import remove_accents
from naming import alt_names_for, resolve_name

TZDB_PATH = '/usr/share/zoneinfo/zone1970.tab'

# ccTLDs that are not simply "." + the ISO 3166-1 alpha-2 code.
TLD_OVERRIDES = {'GB': '.uk'}


def single_timezone_by_country(path=TZDB_PATH):
    """ISO2 -> IANA zone, for countries that have exactly one.

    From the IANA time zone database, which is public domain and already on
    the machine -- so this closes most of the timezone gap without taking on a
    licence. 214 of its 247 countries have a single zone. Multi-zone countries
    are deliberately absent: picking one of several would be a guess, and the
    honest answer there needs boundary geometry, whose usable sources are
    OpenStreetMap-derived and therefore share-alike.
    """
    zones = {}
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) < 3:
                    continue
                for code in parts[0].split(','):
                    zones.setdefault(code.strip().upper(), set()).add(parts[2].strip())
    except FileNotFoundError:
        return {}
    return {code: next(iter(z)) for code, z in zones.items() if len(z) == 1}


def flag_emoji(iso2):
    """ISO 3166-1 alpha-2 -> its regional-indicator flag emoji.

    Computed, not sourced: the flag for a country *is* its two letters shifted
    into the regional indicator block.
    """
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return None
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in iso2.upper())


def derive_tld(iso2):
    """The ccTLD for a country code.

    Derived rather than read from P78: that property points at an item whose
    label carries the string, and those items are not places, so nothing keeps
    them and the QID cannot be resolved. For country-code TLDs the rule is
    mechanical, with a short exception list.
    """
    if not iso2 or len(iso2) != 2:
        return None
    return TLD_OVERRIDES.get(iso2.upper(), '.' + iso2.lower())


def parse_point(wkt):
    """'Point(<lng> <lat>)' -> (lat, lng) floats, or (None, None).

    Case varies between producers, so it is matched case-insensitively; the
    dump emits the OGC spelling.
    """
    if not wkt:
        return None, None
    match = re.match(r'^POINT\(([-\d.eE]+)\s+([-\d.eE]+)\)$', wkt, re.IGNORECASE)
    if not match:
        return None, None
    return float(match.group(2)), float(match.group(1))


def parse_int(value):
    if value is None or value == '':
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_float(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def qid_ref(value):
    """A reference to another Wikidata item, kept as 'Q1234' so it is never
    confused with this dataset's own numeric ids."""
    if not value:
        return None
    return value if str(value).startswith('Q') else None


def extra_fields(record, native_lang, timezone_id=None, romanised_from=None):
    """The shared population/type/.../alt-name block, in the same shape and
    under the same names the SPARQL extractor produces, so both pipelines
    write the same canonical record.

    'place_type' rather than 'type': the ndjson line-kind discriminator is
    also called 'type', and a record merged with dict.update() would otherwise
    silently lose one of them.

    `romanised_from` is (language, original text) when the name had to be
    transliterated. It goes into alt_names under its own language, so a record
    whose name reads "Xiacunzhen" still carries 下村镇 and a consumer that
    wants the local script has it in the same row.
    """
    instance_of = record.get('instance_of') or []
    located_in = record.get('located_in') or []
    alt_names = alt_names_for(record, native_lang)
    if romanised_from:
        lang, original = romanised_from
        existing = alt_names.get(lang) or []
        if original not in existing:
            # Sorted, like every other collection in the output: an unsorted
            # list here would change between runs and break the fingerprint.
            alt_names[lang] = sorted(existing + [original])
    return {
        'population': parse_int(record.get('population')),
        'place_type': qid_ref(instance_of[0]) if instance_of else None,
        'geonames_id': parse_int(record.get('geonames_id')),
        'timezone': qid_ref(record.get('timezone')),
        'postal_code': record.get('postal_code') or None,
        'elevation': parse_float(record.get('elevation')),
        'native_label': sorted(record.get('native_label') or []),
        'alt_names': alt_names,
        'area': parse_float(record.get('area')),
        'osm_relation_id': parse_int(record.get('osm_relation_id')),
        'capital_of': qid_ref(record.get('capital_of')),
        # Absent from the truthy dump, which carries no wikibase:sitelinks.
        # Left as None for a later enrichment pass rather than silently
        # dropped from the schema.
        'sitelinks': None,
        'admin2_id': qid_ref(located_in[0]) if located_in else None,
        # From the IANA database via the country, when that country has a
        # single zone. P421 fills only ~5% of settlements on its own.
        'timezone_id': timezone_id,
    }


def _first_qid(record, field):
    values = record.get(field) or []
    for value in values:
        if isinstance(value, str) and value.startswith('Q'):
            return int(value[1:])
    return None


def country_extra(iso2, record, refs, native_lang, single_zone):
    """The country-level block: the facts the upstream ODbL dataset carried
    and this pipeline previously dropped on the floor.

    Everything here comes from the same CC0 source as the rest, except the
    flag and the ccTLD, which are computed from the ISO code, and the
    timezone, which comes from the public-domain IANA database. No
    attribution-bearing source is involved, so the result stays CC0.
    """
    if record is None:
        record = {}

    currency_qid = _first_qid(record, 'currency')
    currency = refs.get(currency_qid) or {}
    capital_qid = _first_qid(record, 'capital')
    capital = refs.get(capital_qid) or {}
    continent_qid = _first_qid(record, 'continent')
    continent = refs.get(continent_qid) or {}

    demonyms = record.get('demonym') or {}
    demonym = demonyms.get('en') or (demonyms.get(native_lang) if native_lang else None)

    capital_name, _lang = resolve_name(capital, native_lang) if capital else (None, None)
    continent_name, _lang = resolve_name(continent, native_lang) if continent else (None, None)
    currency_name, _lang = resolve_name(currency, native_lang) if currency else (None, None)

    return {
        'iso_3166_1_alpha3': record.get('iso_3166_1_alpha3'),
        'iso_3166_1_numeric': record.get('iso_3166_1_numeric'),
        'calling_code': sorted(record.get('calling_code') or []),
        'currency_id': 'Q%d' % currency_qid if currency_qid else None,
        'currency_code': currency.get('currency_code'),
        'currency_name': remove_accents(currency_name) if currency_name else None,
        'currency_symbol': currency.get('unit_symbol'),
        'capital_id': 'Q%d' % capital_qid if capital_qid else None,
        'capital_name': remove_accents(capital_name) if capital_name else None,
        'continent_id': 'Q%d' % continent_qid if continent_qid else None,
        'continent_name': remove_accents(continent_name) if continent_name else None,
        'demonym': remove_accents(demonym) if demonym else None,
        'tld': derive_tld(iso2),
        'flag_emoji': flag_emoji(iso2),
        'timezone_id': single_zone.get(iso2.upper()),
        'official_language': native_lang,
        'population': parse_int(record.get('population')),
        'area': parse_float(record.get('area')),
    }


def official_language(country_record, lang_codes):
    """The country's primary official language as a Wikimedia language code.

    Several official languages resolve to several codes; the lexicographically
    lowest wins, the same determinism convention used everywhere else here.
    """
    if not country_record:
        return None
    codes = []
    for lang_qid in country_record.get('official_language') or []:
        if not lang_qid.startswith('Q'):
            continue
        code = lang_codes.get(int(lang_qid[1:]))
        if code:
            codes.append(code)
    return min(codes) if codes else None
