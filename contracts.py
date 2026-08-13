"""Published identity: the values consumers have already stored.

Everything here is a frozen contract. Adding is safe; changing an existing value
is a migration for every install that has imported this data, because these are
what a re-import matches rows on:

  * `slugify()` is in URLs and in the rows an install matches on. Change it and
    every slug in the dataset changes, every link breaks, and a re-import
    inserts duplicates instead of updating.
  * `COUNTRY_NAME_OVERRIDES` pins seven spellings already shipped. Editing one
    renames a country everywhere on the next import.
  * `coord()`'s precision matches the DECIMAL(10,6) columns these land in.
    Widening it would change every coordinate string and therefore the content
    hash of every file.

These lived in `build.py`, which was the builder for the ODbL dataset this
project replaced -- it downloaded dr5hn/countries-states-cities-database and
wrote src/. Nothing called it any more, but every module still imported its
identity functions from it, so the module the whole pipeline depended on was
the one thing in the repository whose purpose was to consume the dataset the
README says nothing here is derived from. The code was always ours and reading
a dataset never made it a derivative, but a claim that has to be explained is
weaker than one that does not, so the contracts moved here and the builder is
gone.
"""

import re
import unicodedata

# DECIMAL(10,6) in the Shopclass schema. Upstream ships 8 decimals; anything past
# the 6th is discarded on insert anyway, so round here and keep the diffs stable.
COORD_DP = 6

CSV_HEADER = [
    's_country_name', 's_country_slug', 's_country_code_iso2',
    's_region_name', 's_region_slug',
    's_city_name', 's_city_slug',
    'd_coord_lat', 'd_coord_long',
    'i_region_source_id', 'i_city_source_id',
]

# Names upstream spells differently from the form Shopclass has always shipped.
# Changing one of these renames a country on every install that re-imports, so
# they are additive only.
COUNTRY_NAME_OVERRIDES = {
    'CD': 'Congo Republic',
    'HR': 'Croatia',
    'CI': 'Ivory Coast',
    'HK': 'Hong Kong',
    'BS': 'Bahamas',
    'NL': 'Netherlands',
    'GM': 'Gambia',
}


def remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    txt_string = u"".join([c for c in nfkd_form if not unicodedata.combining(c)])
    return txt_string.encode('ascii', 'ignore').decode('ascii')


def slugify(input_str):
    """Lowercase ASCII, spaces to hyphens, punctuation dropped.

    Slugs are the identity an install matches on when it re-imports a country, and
    they are in published URLs. Do not change this function without a migration.
    """
    input_str = unicodedata.normalize('NFKD', input_str).encode(
        'ascii', 'ignore').decode('ascii')
    input_str = re.sub(r'[^\w\s-]', '', input_str).strip().lower()
    return re.sub(r'[-\s]+', '-', input_str)


def coord(value):
    """Upstream coordinate -> fixed-precision string, or None when absent."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return format(round(float(value), COORD_DP), '.%df' % COORD_DP)
    except (TypeError, ValueError):
        return None


def mean_coord(pairs):
    """Centroid of the points that have one. Returns (lat, lng) or (None, None).

    Used for the handful of regions upstream has no coordinate for: the mean of
    its cities is a better answer than leaving the region — and every listing that
    resolves to it — with no location at all.
    """
    lats = [float(a) for a, b in pairs if a is not None and b is not None]
    lngs = [float(b) for a, b in pairs if a is not None and b is not None]
    if not lats:
        return None, None
    return (format(round(sum(lats) / len(lats), COORD_DP), '.%df' % COORD_DP),
            format(round(sum(lngs) / len(lngs), COORD_DP), '.%df' % COORD_DP))


