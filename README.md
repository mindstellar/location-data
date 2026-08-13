# location-data

A worldwide dataset of countries, administrative divisions and settlements,
built from Wikidata, plus the scripts that build it.

**255 countries, 6,036 administrative divisions, 1,745,358 settlements**, every
one with coordinates.

```bash
curl -s https://geo.mindstellar.com/releases/latest.json
curl -s --compressed https://geo.mindstellar.com/releases/<version>/manifest.json
curl -s --compressed https://geo.mindstellar.com/releases/<version>/json/MT.json
```

Three formats per country — `data/<CC>.ndjson` to stream, `json/<CC>.json`
nested, `csv/<CC>.csv` flat — with every file's sha256 in `manifest.json`, so a
fetch can be verified. Use `--compressed`: the edge compresses on the fly and
Mexico is 76 MB otherwise.

The data is published to object storage rather than kept in git — see
`docs/RELEASING.md`. What is in this repository is the pipeline that produces
it.

## Why

The dataset is built from [Wikidata](https://www.wikidata.org/), which is
**CC0**, and it is published CC0. A consumer can take it, modify it, embed it
in a product and never think about the licence again.

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

Every design decision below defers to the same constraint: no source carrying
an attribution or share-alike requirement is mixed in, however convenient. That
rules out GeoNames (CC-BY) and anything OpenStreetMap-derived (ODbL), both of
which would close real gaps documented in `docs/LIMITATIONS.md`.

Where the data has gaps that Wikidata alone cannot fill, the fallbacks are
chosen for the same reason. Time zones come from the IANA time zone database,
which is public domain. Flag emoji and country-code TLDs are computed from the
ISO 3166-1 code rather than read from anywhere. Nothing here adds an obligation.

## How the data is built

### How it runs

```bash
zcat latest-truthy.nt.gz | python dump_scan.py --out-dir dump-scan
python dump_build.py --scan-dir dump-scan --out-dir dump-build
```

`dump_scan.py` makes one streaming pass over the Wikidata truthy dump (~982 GB
of N-Triples, ~8.2 billion statements) and reduces it to the facts this build
needs: roughly 1.7 GB of entity records plus the two graphs it cannot do
without. `dump_build.py` then does all the judging with no network access at
all, in about four minutes.

The split is forced, not stylistic. Nothing in a streaming pass can decide
whether an entity is a settlement: that needs the transitive `P279` subclass
closure, and attaching a settlement to its region needs the transitive `P131`
containment graph. Both are scattered across the whole dump, so classification
cannot happen until the pass is over.

Budget on a 4-core machine: ~50 minutes to download the dump, ~90 minutes to
scan, ~5 minutes to build. `tools/refresh.py` runs all of it, resumably.

### Validation

```bash
python validate.py <build-dir>
```

Fails on loss, never on growth. The check that matters is per-country: the
global sums pass comfortably while individual countries are gutted, which is
exactly how a source swap can ship an eightfold overall gain alongside a
country that lost 98% of its cities.

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

## Determinism

Two runs over the same Wikidata state produce byte-identical output: sorted
collections, fixed coordinate precision, stable key order, and a version
derived from content hashes rather than the clock. This is load-bearing for any
scheduled refresh — a timestamp or an unordered set anywhere in the output
turns every run into a spurious diff.

## Frozen contracts

Three things are published identity that consumers store. Adding is safe;
changing an existing value is a migration for everyone:

- **`slugify()`** — in URLs and in the rows a consumer matches on when
  re-importing. It lives in `contracts.py`, alone, so that changing it is a
  deliberate act rather than an edit to a file that does other things.
- **`COUNTRY_NAME_OVERRIDES`** — each entry pins a spelling already shipped.
- **the upstream entity id** — the identity a re-import matches on, so a rename
  upstream becomes an in-place rename rather than a delete plus an insert.

## Licence

The scripts are **GPL-3.0** (`LICENSE`).

The data is **CC0-1.0**, and a licence file stating so ships inside every
release alongside the files it applies to. The manifest records it too, in
`s_license`.

The claim is meant literally, so it is worth saying what backs it. The data
comes from the Wikidata truthy dump, which is CC0. Time zones come from the
IANA time zone database, which is public domain. Flag emoji and country-code
TLDs are computed from the ISO 3166-1 code rather than read from any source.
Names in non-Latin scripts are machine transliterated, which is a mechanical
transformation of CC0 input and creates no new rights. No other source
contributes a single field.

## Before you depend on it

`docs/LIMITATIONS.md` is the honest account: what the coverage actually is per
field, the three real gaps (settlements with no coordinates, Arabic-script
names, timezones outside single-zone countries), and the choices that are
decisions rather than defects — disputed territories, synthesised cities,
territories shipping as countries rather than as regions of their parent.

`docs/RELEASING.md` covers how a release is produced and published.
