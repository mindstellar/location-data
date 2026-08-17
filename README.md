# location-data

A worldwide dataset of countries, administrative divisions and settlements,
built from Wikidata and published **CC0** — plus the pipeline that builds it.

**255 countries · 4,328 administrative divisions · 1,674,947 settlements**

Every settlement has coordinates. No region contains the same name twice. Two
runs over the same Wikidata state produce byte-identical output.

## Start here

```bash
# what is current
curl -s https://geo.mindstellar.com/releases/latest.json

# the catalog: every country, its files, their sha256
curl -s --compressed https://geo.mindstellar.com/releases/<version>/manifest.json

# one country
curl -s --compressed https://geo.mindstellar.com/releases/<version>/json/MT.json
```

`releases/latest.json` is the only URL worth hardcoding. It names the current
version and its manifest; everything else hangs off that. Use `--compressed` —
the edge compresses on the fly, and Mexico is 76 MB if you don't.

## What a row looks like

```json
{
  "type": "settlement",
  "id": 39520,
  "source": "wikidata",
  "admin1_id": 39520,
  "country_code": "MT",
  "name": "Mosta",
  "slug": "mosta",
  "latitude": "35.900000",
  "longitude": "14.433333",
  "population": 20241,
  "place_type": "Q515",
  "geonames_id": 8299734,
  "timezone_id": "Europe/Malta",
  "elevation": null,
  "alt_names": {},
  "admin2_id": "Q20199334"
}
```

`id` is the Wikidata QID, and it is the identity to match on when you
re-import: an upstream rename becomes a rename rather than a delete plus an
insert. Coordinates are 6-decimal strings, sized for `DECIMAL(10,6)`. Countries
carry a fuller block — capital, currency and symbol, calling code, continent,
demonym, ccTLD, flag emoji, ISO alpha-3 and numeric.

Field-by-field fill rates are in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

## Three formats, same records

| | |
|---|---|
| `data/<CC>.ndjson` | one JSON object per line — country, then each region followed by its settlements. Streamable in constant memory, which matters: the largest country is 76 MB. |
| `json/<CC>.json` | the same record as one nested document |
| `csv/<CC>.csv` | one row per settlement, flat |

Every file's sha256 and byte count is in `manifest.json`, so a fetch can be
verified. Files are named by ISO code, so a country renamed upstream cannot
move one.

The data is published to object storage rather than kept in git — see
[`docs/RELEASING.md`](docs/RELEASING.md). What is in this repository is the
pipeline that produces it.

## Why it exists

The dataset is built from [Wikidata](https://www.wikidata.org/), which is CC0,
and it is published CC0. You can take it, modify it, embed it in a product and
never think about the licence again.

That is the whole reason the project exists, because the alternative does not
allow it.
[dr5hn/countries-states-cities-database](https://github.com/dr5hn/countries-states-cities-database)
is the dataset most people reach for and it is a good one, but it is **ODbL**,
which is share-alike: anything redistributing it owes attribution and must keep
the derived database under the same licence, forever, through every consumer.
For anything that ships location data inside a product, that is a permanent
obligation attached to a table of city names.

So it was rejected as a source rather than used as one. **Nothing here is
derived from it** — not the data, which comes from the Wikidata dump, and not
the code. Its existence is the motivation, not the provenance.

Every decision below defers to the same constraint: no source carrying an
attribution or share-alike requirement is mixed in, however convenient. That
rules out GeoNames (CC-BY) and anything OpenStreetMap-derived (ODbL), both of
which would close real gaps documented in
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

Where Wikidata alone cannot fill a gap, the fallbacks are chosen the same way.
Time zones come from the IANA time zone database, which is public domain. Flag
emoji and ccTLDs are computed from the ISO 3166-1 code rather than read from
anywhere. Nothing here adds an obligation.

## What counts as a city

The dataset is a city tier. That sounds obvious and is the single most
consequential set of decisions here, because Wikidata's ontology does not draw
the line where you would expect.

- **Municipalities are a second class root.** Wikidata files them under the
  administrative branch, not under "human settlement", so classifying on
  settlement alone misses every commune of France while happily keeping its
  abbeys and hamlets.
- **Former divisions are excluded**, including ones marked dissolved only by
  their class rather than by an end date. Over half of Japan's candidates were
  abolished municipalities.
- **Neighbourhoods are excluded, suburbs are not.** The two words describe
  different things. Where Wikidata uses "suburb" it is overwhelmingly an
  addressing unit — "gazetted locality of Victoria", "suburb/locality of
  Tasmania" — and an Australian address names its suburb, so a picker without
  them cannot express most Australian locations. Neighbourhoods are informal,
  overlapping and have no postal identity.
- **Class exclusions lose to positive evidence.** "College town" is a subclass
  of "academic enclave", which is a subclass of "neighborhood", so excluding
  neighbourhoods transitively deletes Bern and Basel. Anything that is also a
  city, town, village or municipality survives.
- **Somewhere people are confined is not somewhere people live.** Prison camps,
  internment camps and buried archaeological monuments are excluded even though
  Wikidata tags them as human settlements. Refugee camps are kept, and
  deliberately: Kutupalong holds 598,195 people.

## What is guaranteed

- **Every settlement has coordinates.** A row without a position cannot be
  mapped, distance-sorted or deduplicated, so it is not shipped.
- **No region contains the same name twice.** A name identifying two places
  identifies neither, and a consumer picking from a list cannot see that the
  choice was ambiguous. Duplicates are merged where they are the same place,
  qualified where they are not — `Aach (Konstanz)` — and dropped where nothing
  can tell them apart.
- **The build is byte-deterministic.** Sorted collections, fixed coordinate
  precision, stable key order, and a version derived from content hashes rather
  than the clock. A scheduled refresh depends on this: a timestamp or an
  unordered set anywhere in the output turns every run into a spurious diff.
- **Three things are frozen**, because consumers store them. `slugify()` lives
  alone in `contracts.py` so that changing it is a deliberate act;
  `COUNTRY_NAME_OVERRIDES` pins spellings already shipped; the upstream entity
  id is the identity a re-import matches on. Adding is safe. Changing an
  existing value is a migration for everyone.

## How it is built

```bash
pip install -r requirements.txt

zcat latest-truthy.nt.gz | python dump_scan.py --out-dir dump-scan
python dump_build.py --scan-dir dump-scan --out-dir dump-build
python validate.py dump-build
```

`dump_scan.py` makes one streaming pass over the Wikidata truthy dump (~982 GB
of N-Triples, ~8.2 billion statements) and reduces it to what this build needs:
about 1.8 GB of entity records plus the two graphs it cannot do without.
`dump_build.py` then does all the judging with no network access at all.

The split is forced, not stylistic. Nothing in a streaming pass can decide
whether an entity is a settlement: that needs the transitive `P279` subclass
closure, and attaching a settlement to its region needs the transitive `P131`
containment graph. Both are scattered across the whole dump, so classification
cannot happen until the pass is over.

Budget on a 4-core machine: **~50 minutes** to download the dump, **~90
minutes** to scan, **~7 minutes** to build. `tools/refresh.py` runs all of it,
resumably.

`validate.py` fails on loss, never on growth. The check that matters is
per-country: the global sums pass comfortably while individual countries are
gutted, which is exactly how a source swap can ship an eightfold overall gain
alongside a country that lost 98% of its cities.

## Licence

The scripts are **GPL-3.0** (`LICENSE`).

The data is **CC0-1.0**. A licence file stating so ships inside every release
alongside the files it applies to, and the manifest records it in `license`.

The claim is meant literally, so it is worth saying what backs it. The data
comes from the Wikidata truthy dump, which is CC0. Time zones come from the
IANA time zone database, which is public domain. Flag emoji and ccTLDs are
computed from the ISO 3166-1 code rather than read from any source. Names in
non-Latin scripts are machine transliterated, which is a mechanical
transformation of CC0 input and creates no new rights. No other source
contributes a single field.

## Before you depend on it

[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) is the honest account: coverage
per field, the three real gaps (settlements with no coordinates, Arabic-script
names, timezones outside single-zone countries), and the choices that are
decisions rather than defects — disputed territories, country-level regions,
territories shipping as countries rather than as regions of their parent.

[`docs/RELEASING.md`](docs/RELEASING.md) covers how a release is produced and
published. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers what the pipeline
decides, where each decision lives, and the traps that have already been hit.
