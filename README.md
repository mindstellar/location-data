# location-data

A worldwide dataset of countries, administrative divisions and settlements,
built from Wikidata, plus the scripts that build it.

The dataset is not in this repository yet. What is here is the pipeline that
produces it, and the licensing reason it exists at all.

## Why

The obvious source for this data is
[dr5hn/countries-states-cities-database](https://github.com/dr5hn/countries-states-cities-database),
and it is a good dataset. It is also **ODbL**, which is share-alike: anything
that redistributes it owes attribution and must keep the derived database under
the same licence. That travels with the data forever, through every consumer.

Wikidata is **CC0**. A dataset built from it carries no attribution obligation
and no share-alike, so a consumer can take it, modify it, embed it in a product
and never think about the licence again. That is the entire point of this
repository, and every design decision below defers to it: no source that
carries an attribution requirement is mixed in, however convenient.

Where the data has gaps that Wikidata alone cannot fill, the fallbacks are
chosen for the same reason. Time zones come from the IANA time zone database,
which is public domain. Flag emoji and country-code TLDs are computed from the
ISO 3166-1 code rather than read from anywhere. Nothing here adds an obligation.

## How the data is built

There are two paths to the same canonical output.

### The dump pipeline (current)

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
scan, ~4 minutes to build.

### The SPARQL pipeline (superseded)

```bash
python build_wikidata.py --countries AL,IN,DE --keep-going
```

Kept because it is a useful cross-check on a handful of countries, and because
its query design documents the semantics the dump pipeline had to reproduce. It
cannot build the whole world: the endpoint cancels an expensive query at its
deadline and reports it as HTTP 429, so "too expensive" and "slow down" are
indistinguishable from the client side, and the largest countries never finish.

### Validation

```bash
python validate.py --baseline path/to/previous/json-list.json
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
  re-importing. Both pipelines import it from one place so they cannot drift.
- **`COUNTRY_NAME_OVERRIDES`** — each entry pins a spelling already shipped.
- **the upstream entity id** — the identity a re-import matches on, so a rename
  upstream becomes an in-place rename rather than a delete plus an insert.

## Licence

The scripts are **GPL-3.0** (`LICENSE`).

The data this pipeline produces is derived from Wikidata and is intended to be
**CC0-1.0**. It is not published here yet, and no licence file for it should be
added until the published data is genuinely Wikidata-derived — claiming CC0
over anything still carrying ODbL provenance would be false.

## Before you depend on it

`docs/LIMITATIONS.md` is the honest account: what the coverage actually is per
field, the three real gaps (settlements with no coordinates, Arabic-script
names, timezones outside single-zone countries), and the choices that are
decisions rather than defects — disputed territories, synthesised cities,
territories shipping as countries rather than as regions of their parent.

`docs/RELEASING.md` covers how a release is produced and published.
