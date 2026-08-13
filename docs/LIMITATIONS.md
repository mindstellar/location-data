# What this dataset does not do

Written for someone deciding whether to depend on it. Everything here is
measured against the current build, not estimated, and every number can be
reproduced by running the pipeline.

The short version: coverage is very good for countries, divisions and any
settlement Wikidata gives a coordinate and a Latin-script name. It is poor for
Arabic-script names, for China below the county level, and for timezones
outside single-zone countries. Those three have specific causes, given below.

## Scale

| | |
|---|---:|
| countries | 255 |
| administrative divisions | 6,036 |
| settlements | 1,745,358 |
| with coordinates | 100% |

Every row is a place Wikidata records. Nothing is synthesised to round the
numbers out or to keep a region from looking empty.

## Formats

Each release carries the same records three ways, under
`releases/<version>/`:

| | |
|---|---|
| `data/<CC>.ndjson` | one JSON object per line — country, then each region followed by its settlements. Streamable in constant memory, which matters: the largest country is 76 MB. |
| `json/<CC>.json` | the same record as one nested document |
| `csv/<CC>.csv` | one row per settlement, flat |

`manifest.json` lists every country with its file paths, sha256 of each, and
counts. Fetch that first; it is the only URL worth hardcoding, and files are
named by ISO code so a country being renamed upstream cannot move one.

Field names are this dataset's own and deliberately neutral. An earlier release
also published a copy in a particular consumer's column conventions; it was
removed, because two formats to keep consistent is a cost paid forever and
mapping field names on the way in is a few lines paid once.

## Field coverage

Per settlement:

| field | fill |
|---|---:|
| name, slug, latitude, longitude, place_type, country_code, source | 100% |
| admin2_id | 98.3% |
| timezone_id | 46.6% |
| geonames_id | 45.1% |
| population | 41.7% |
| alt_names | 36.8% |
| postal_code | 31.4% |
| elevation | 29.1% |
| area | 16.4% |
| osm_relation_id | 13.2% |
| native_label | 6.2% |
| sitelinks | 0% |

Per country, the block is 92–100% filled for capital, continent, currency with
code and symbol, calling code, ISO alpha-3 and numeric, demonym, ccTLD, flag,
population, area and official language. Timezone is 83.9%, for the reason
below.

## The three real gaps

### Settlements with no coordinates are dropped — 883,194 of them

A row without a position cannot be mapped, distance-sorted or deduplicated, so
it is not shipped. About two thirds of these are China's administrative
villages: Wikidata has the village, its name and its containment, but not where
it is. India, Russia, Uganda and Myanmar account for most of the rest.

This is the largest single gap in the dataset and it is upstream. Closing it
needs a gazetteer with coordinates — NGA GNS is public domain and would fit —
and the expensive part is conflation, not licensing.

### Names in Arabic script are refused — 163,759 settlements have no usable name

A name has to survive slugification to have an identity, so a settlement whose
only labels are in a non-Latin script is transliterated. That is done only for
scripts where the output was checked against real data and found to be a name:
**Cyrillic, Greek, Georgian, Armenian, and Chinese via pinyin.**

Everything else is refused rather than guessed, because a wrong name still
slugs, still ships, and nobody notices:

- **Abjads** — Arabic, Hebrew — omit short vowels, so a table lookup returns a
  consonant skeleton. Casablanca comes out `ldr lbyd'`.
- **Abugidas** — Burmese, Bengali, Devanagari — drop inherent vowels unevenly.
  Burmese gives `Kyiunlpmriu` for a town called Kyainglat.
- **Japanese and Korean** — the tables reach for Mandarin readings of kanji and
  hanja and return `MangSangHaeSuYogJang` for a Korean beach.

Yemen and Morocco are the worst affected. This needs a source with real BGN
romanisations, which is again NGA GNS.

Where a name *was* transliterated, the original script is kept in `alt_names`
under its own language code, so nothing is lost.

### Timezone is 46.6%

From the IANA time zone database, which is public domain, and which resolves
only countries that have exactly one zone — 214 of 247. Multi-zone countries
(United States, Russia, Canada, Australia, Brazil) need boundary geometry, and
the usable boundary sets are OpenStreetMap-derived and therefore share-alike.
Taking one would forfeit the CC0 guarantee, which is the whole point of the
project, so the field is left empty rather than filled from a source that
carries obligations.

`sitelinks` is 0% for a narrower reason: the Wikidata truthy dump does not
carry it. It is a separate enrichment pass that has not been built.

## Choices you may disagree with

These are decisions, not defects, and they are all reversible in the pipeline.

**Disputed territories follow ISO, not `P131`.** Crimea ships under `UA`;
Kosovo ships as its own `XK`. About 1,624 settlements are filed under a country
other than the one Wikidata's containment graph places them in, and nearly all
are Crimea, Kosovo and the Essequibo. Where Wikidata is politically ambiguous,
the more standard answer was taken.

**Six codes are not ISO 3166-1.** `AC`, `CP`, `CQ`, `DG` and `TA` are ISO
*exceptionally reserved* codes for real inhabited places — Ascension, Clipperton,
Sark, Diego Garcia, Tristan da Cunha. `XK` is the user-assigned code for Kosovo
that the EU and most software use. Dissolved states carrying legacy ISO codes —
East Germany, Yugoslavia, the Netherlands Antilles — are excluded.

**Territories are countries, not regions of their parent.** Guadeloupe, Puerto
Rico, Hong Kong, Aruba and Åland ship as their own countries and do *not* also
appear in the region list of France, the United States, China, the Netherlands
or Finland. Listing them in both places would put one place under two ids.

**Country-level regions.** A settlement whose containment reaches no division
is filed under a region named for the country itself rather than dropped. This
is why some countries have a region with the same name as the country.

**Archaeology is excluded.** Ghost towns, abandoned villages, hillforts,
Neolithic settlements and ancient cities are not current places and are left
out — about 30,000 rows. A modern city that is *also* an ancient one (Rome,
Athens, Damascus, Istanbul) is kept, because the rule requires every one of its
classes to be historical.

**Suburbs are cities; neighbourhoods are not.** Where Wikidata uses "suburb" it
is overwhelmingly an addressing unit — gazetted localities of Victoria and
Tasmania — and an Australian address names its suburb. Neighbourhoods are
informal, overlapping and have no postal identity.

**Every row names its source.** `source` is `wikidata` on every row today. It
exists because ids from a second source would otherwise share an integer space
with Wikidata QIDs and nothing would tell them apart — which is exactly the
defect an earlier release caused in a consumer that matched on the id alone.

## Smaller things

- **Vietnam is undercounted.** Wikidata files its commune-level towns, district
  towns, provincial cities and urban districts beneath "former subdivisions of
  Vietnam". Excluding what upstream marks as former is correct, so this should
  recover on its own as Wikidata reclassifies.
- **1,618 names are longer than 60 characters** (0.09%), the longest being a
  249-character German housing-estate address. Truncate if your schema needs
  to.
- **Names are accent-folded.** `name` is ASCII; the unfolded form is not
  currently kept.
- **Machine-transliterated names are not flagged as such.** They can be
  identified by the presence of an `alt_names` entry in a non-Latin script for
  the same language as `name_lang`.

## How to check any of this yourself

The build is byte-deterministic: the same Wikidata state produces the same
output and the same `s_version`. Every count above comes from
`build-stats.json`, which ships with each release and records per country how
many settlements were seen, how many reached no division, and how many were
dropped for having no usable name or no coordinates.
