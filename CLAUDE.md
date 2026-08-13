# CLAUDE.md

Guidance for working in this repository.

## What this is

A data pipeline, not an application. Python scripts turn Wikidata into a
worldwide dataset of countries, administrative divisions and settlements. There
is no library to import.

The generated data is not committed and never should be — it is published to
Cloudflare R2, see `docs/RELEASING.md`. `docs/LIMITATIONS.md` is the honest
account of what the dataset does and does not cover, with the numbers.

## The licence constraint drives everything

The dataset exists because the obvious alternative
(`dr5hn/countries-states-cities-database`) is ODbL, and ODbL's share-alike
travels with the data through every consumer forever. Wikidata is CC0.

**Never mix in a source that carries attribution or share-alike** without
raising it explicitly first. That includes GeoNames (CC-BY) and anything
OpenStreetMap-derived (ODbL) — both are tempting because they would close real
gaps, and both would forfeit the one thing this dataset has that competitors
cannot match. Public domain sources (IANA tzdb, NGA GNS, USGS GNIS) are safe.

Anything derived by computation from an ISO code — flag emoji, ccTLD — carries
no obligation at all and is preferred over looking it up.

## Commands

```bash
pip install -r requirements.txt

# dump pipeline (current): ~90 min scan, ~4 min build
zcat latest-truthy.nt.gz | python dump_scan.py --out-dir dump-scan
python dump_build.py --scan-dir dump-scan --out-dir dump-build
python dump_build.py --scan-dir dump-scan --countries FR,DE,JP   # iterate on a few
python dump_build.py --scan-dir dump-scan --bucket-dir /tmp/b    # keep the grouped
                                                                 # settlements to read

python validate.py <build-dir>                  # vs the published release (needs R2 creds)
python validate.py <build-dir> --baseline none   # first build, or offline
python validate.py <build-dir> --baseline path/to/previous/manifest.json

# checks, in ascending order of cost and confidence
python -m unittest discover -s tests            # ~1 s, no scan needed
python dump_build.py --scan-dir tests/fixtures/scan --out-dir /tmp/fx \
    --countries $(python -c "import json;print(','.join(json.load(open('tests/fixtures/scan/scan-stats.json'))['countries']))")
python tools/snapshot.py <build-dir> --compare tests/reference/build.json

# the monthly refresh and release, on a machine you own -- see docs/RELEASING.md
python tools/refresh.py --work-dir ~/development/wikidata-dump
python tools/refresh.py --skip dump,scan          # rebuild and publish only
python tools/publish.py status
```

`dump_scan.py` is the expensive half. Develop against a slice of the dump
rather than the whole thing — a couple of GB of N-Triples exercises every code
path except the country-level properties, which only appear on the low QIDs.

## How behaviour is pinned

Three layers, and a change to the pipeline should be justified against all of
them before it lands:

- **`tests/test_decisions.py`** — the six pure decision functions, on synthetic
  inputs. Every case is a defect that actually shipped.
- **`tests/test_fixture_build.py`** — a whole build over `tests/fixtures/scan`,
  seven countries in about a second. It asserts the result matches the *full*
  build's output for those countries byte for byte, not merely itself.
- **`tests/reference/build.json`** — the fingerprint of the whole world.
  Decisive, and about six and a half minutes away.

Regenerate the fixture with `tools/make_fixture.py` after a rescan. Adding a
country to it requires checking the result still matches the reference: a
country whose settlements are contested by a neighbour will not reproduce in
isolation. Kosovo does not, because Serbia claims its divisions.

## Where the code lives

`dump_scan.py` is stage 1. Stage 2 is split by decision rather than by step, so
a rule can be found by what it decides:

```
contracts.py    slugify, COUNTRY_NAME_OVERRIDES, coordinate precision -- published
                identity, frozen, and the reason it has its own module
classify.py     the subclass closures, the three kinds of exclusion, is_settlement
contain.py      P131 propagation, the four division tiers, CountryPlan
naming.py       resolve_name and what makes a label usable at all
countryblock.py the per-entity and per-country fact blocks
emit.py         assembling a country and writing all three output formats
dump_build.py   the index pass and the orchestration
```

Two things there are load-bearing rather than stylistic. `Exclusions` is
unpacked positionally inside `is_settlement` because that runs once per entity
across ~20M of them, so its field order is pinned by a test. `CountryPlan` owns
the step that makes a country's own record usable as a division record —
omitting it is what silently dropped twenty capitals, and both callers now have
to go through it.

## Two stages, and why it cannot be one

Nothing in a streaming pass can decide whether an entity is a settlement. That
needs the transitive `P279` subclass closure, and attaching a settlement to its
region needs the transitive `P131` containment graph — both scattered across
~982 GB. So stage 1 harvests without judging and stage 2 judges without
touching the dump.

Neither graph is randomly accessed. The dump is *nearly* QID-ordered but not
exactly (adjacent transpositions occur), so any binary search over the edge
arrays is quietly wrong, and a child→parents dict over 15M edges costs more
memory than the machine has. Both closures rescan the flat `int32` array until
it stops changing, converging in the depth of the hierarchy rather than its
size.

## What a region is

The **root-most** ISO 3166-2 division: one with no other ISO-coded division
above it. `keep_root_most` in `contain.py`. ISO 3166-2 is flat, and for about a
fifth of countries it lists two levels at once, so the alternative -- keeping
the leaf-most -- gave Czechia 109 regions instead of 14 and Bangladesh 74
instead of 8.

Two things this depends on, both of which were wrong once:

- **The ancestor map must close upward, not stop at divisions.** A chain from
  one division to its parent division often passes through something that is
  not itself ISO-coded. Building the parent map over divisions alone left
  Lithuania's municipalities all looking top-level, Czechia with 31 regions
  and Britain with 137.
- **Nothing is held back as a coarse fallback.** That existed to catch a
  capital whose P131 pointed at a coarser division than the selected leaf.
  Selecting the root-most removes the cause, and keeping the finer divisions
  as coarse seeds would actively hurt: a settlement inside a departement
  reaches the departement and its region at the same depth, so the tie would
  break on QID and scatter settlements between two levels.

## One name, one place inside a region

Two settlements in one region can share a name for two opposite reasons, and
`resolve_collisions` in `emit.py` separates them by distance.

Wikidata routinely holds the administrative unit and the built-up place at its
seat as **separate items** -- "comune of Italy" beside "municipality seat",
"municipality of Colombia" beside "human settlement". Both pass the settlement
test, so both used to ship: 248,712 rows shared a name with another row in the
same region. Within 2 km they are merged, lowest QID surviving and absorbing
the other's fields, because upstream fills the pair differently -- the
administrative item tends to carry area and population, the settlement item the
GeoNames id.

Beyond 2 km they are different places. Germany has two towns called Aach 80 km
apart, and Colombia and Guatemala were each about a third duplicates. Those are
kept, and their names qualified with the P131 parent: `Aach (Konstanz)`.

- **The largest of a group keeps the bare name.** Qualifying every row renamed
  the capital of Colombia, because a village of 61,549 called Bogota sits in
  the same region -- and the same for Vilnius. Both then failed the
  contains-its-own-capital check, which is exactly what that check is for.
- **A qualifier that does not qualify is not used.** Where two rows share a
  parent, or the parent is named after the place, the bare name is left and
  counted in `ambiguous_names`.

## Traps that have already been hit

Each of these cost real time and will look like a fresh idea to anyone who
hasn't seen them:

- **Containment must pick the nearest division, not the lowest QID.** `US-PR`
  is a valid ISO 3166-2 code, so a Puerto Rican settlement reaches both its own
  municipality and Puerto-Rico-as-a-US-state; choosing by QID gave every one of
  them to the United States and left Puerto Rico with 99 regions and no cities.
- **A country item can carry an ISO 3166-2 code of its own**, and 32 of them
  do. Most are dependent territories coded under a parent — Aruba is `NL-AW`,
  Guadeloupe `FR-971` — but the United Kingdom itself is `GB-UKM`, which made
  Q145 an ordinary GB division sitting one hop above every British territory.
  Gibraltar's containment resolved to Britain and its real settlements shipped
  under `GB`. Fixed by `is_country_item`: a country is never a division of
  another country. Do not undo it to give France back its overseas régions —
  they ship as their own countries, and having them in both places puts the
  same place under two ids.
- **Group settlements by containment, not by the shard they were stored in.**
  Shards are keyed on `P17`, and a dependent territory's settlements name the
  parent state — Guadeloupe's communes say France.
- **Municipalities are not under `Q486972`.** Wikidata files them in the
  administrative branch, so the settlement root alone misses every commune of
  France while keeping its abbeys.
- **Class exclusions must lose to positive evidence.** "College town" →
  "academic enclave" → "neighborhood", so a transitive neighbourhood exclusion
  deletes Bern and Basel.
- **Block traversal through "administrative territorial entity of a defunct
  state".** Wikidata files China's divisions beneath it, so an unguarded
  "former entity" closure marks all 21,494 towns of China as former.
- **Names must survive `slugify()`, not merely accent folding.** A Cyrillic
  name with a parenthesised qualifier folds to `" ( )"` — non-empty, but slugs
  to nothing, and an empty slug is an empty identity.
- **Transliterate only from the local language.** Wikidata carries bot-written
  Chechen and Serbian Cyrillic labels for Mexican places — transliterations of
  names that were Latin to begin with. Romanising those reverses the
  transliteration: `Avikola la Morena (Ermosiyo)` for *Avícola la Morena
  (Hermosillo)*, across 5,623 rows.
- **Transliterate only from a script on the allowlist**, and add to it only
  after looking at real output. Abjads have no vowels to map, so Arabic yields
  `lghbh` for *Al-Ghaba*; abugidas drop them unevenly, so Burmese yields
  `Kyiunlpmriu`; and the tables reach for Mandarin readings of kanji and hanja,
  yielding `MangSangHaeSuYogJang` for a Korean beach. A wrong name still slugs,
  still ships, and nobody notices — which is why the list is an allowlist.
- **Blank nodes are meaningful.** Wikidata writes "dissolved, date unknown" as
  an unknown-value snak, which RDF renders as a blank node. Dropping it because
  the object is unparseable resurrects every division abolished on an unknown
  date.
- **Shard files are opened for append**, so rerunning a scan into a directory
  that already has output silently doubles it.
- **A scan predating a property extraction produces a build that looks fine.**
  The country-level properties -- capital, currency, continent, calling code,
  demonym, ISO alpha-3 -- were added to `dump_scan.py` after an early scan was
  taken, and building from that scan emitted 255 countries whose country block
  was entirely null. Counts, coordinates and per-country regressions all
  passed, and `capital_presence` skips a country that names no capital, so with
  every capital gone it reported nothing at all. `capital_naming` in
  `validate.py` is the floor that now catches it. Check that a scan directory
  is the one a release was built from before building from it.

## Determinism is load-bearing

Two runs over the same Wikidata state must produce byte-identical output:
sorted collections, fixed coordinate precision, stable key order, and a version
derived from content hashes rather than the clock. Any scheduled refresh
depends on this — a timestamp, a set iteration, or an unsorted collection
anywhere in the output turns every run into a spurious diff.

## Frozen contracts

Published identity that consumers have already stored. Adding is safe; changing
an existing value is a migration for everyone:

- **`slugify()`** — in URLs and in the rows a consumer matches on when
  re-importing. It lives in `contracts.py` with the rest of the frozen values.
  Do not reimplement it.
- **`COUNTRY_NAME_OVERRIDES`** — each entry pins a spelling already shipped.
  Additive only.
- **the upstream entity id** — the identity a re-import matches on, so an
  upstream rename becomes an in-place rename rather than a delete plus insert.

## Validation thresholds

`validate.py` fails on loss, never on growth. The country block is checked
separately from everything else, because it is read from one place for every
country at once and so fails all-or-nothing rather than country by country.
It compares against **the published release** by default, and without R2 credentials it exits non-zero
rather than skipping — a gate that disables itself when it cannot reach its
baseline is worse than no gate, and that is not hypothetical: the default used
to be a git ref in a repository that no longer existed, so both regression
gates were dead and every run printed one line about skipping and passed. Use
`--baseline none` for a first build or offline work.

The per-country gate is the one that matters: the global sums pass comfortably while individual countries are
gutted, which is exactly how a source swap ships an eightfold overall gain
alongside a country that lost 98% of its cities. A drop that is investigated
and genuinely upstream sparsity goes in `validate_allowlist.txt` with the
evidence, not a loosened threshold.

## Do not commit

The query cache (`.wikidata_cache/`), dump snapshots, and generated build
output. The cache is hours of querying and worth keeping locally — there is a
copy outside the repository precisely so `git clean -fdx` cannot destroy it.
