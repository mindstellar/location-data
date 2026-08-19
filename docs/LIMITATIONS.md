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
| administrative divisions | 4,328 |
| settlements | 1,674,947 |
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
counts. Its `s_version` is the content fingerprint, the same field name the
release pointer uses for it; `version` carries the same value for consumers
that already read it. Fetch that first; it is the only URL worth hardcoding, and files are
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
| timezone_id | 46.4% |
| geonames_id | 44.5% |
| population | 41.8% |
| alt_names | 37.5% |
| postal_code | 31.9% |
| elevation | 29.1% |
| area | 16.6% |
| osm_relation_id | 13.3% |
| native_label | 6.2% |
| sitelinks | 0% |

Per country, the block is 92–100% filled for capital, continent, currency with
code and symbol, calling code, ISO alpha-3 and numeric, demonym, ccTLD, flag,
population, area and official language. Timezone is 83.9%, for the reason
below.

## What a region is

**The first-level ISO 3166-2 subdivision** — a division with no other
ISO-coded division above it. India's states, Germany's Länder, Japan's
prefectures, Spain's autonomous communities, Czechia's kraje, Bangladesh's
divisions, France's régions.

That matters because ISO 3166-2 is a flat list per country and for about a
fifth of countries it describes two levels at once. Taking the finer one gave
109 regions for Czechia and 74 for Bangladesh — their districts rather than
their regions and divisions — and made "region" mean something different in
every country. The no-ISO-parent rule is the only definition that is
consistent across all of them.

Cross-checked against Natural Earth's admin-1, which is public domain: the two
agree on India, Germany, Japan, Brazil, the United States and most others.
Where they differ, Natural Earth is applying per-country judgement rather than
a rule — it takes départements for France but autonomous communities for
Spain.

Each country also has **one region named after the country itself**, which
catches settlements whose containment reaches no division. It appears only if
something lands in it, which is why counts here are typically ISO + 1. In the
2026-08-15 release it holds 57,250 settlements across 249 countries, and for
Lithuania 99% of them — [`UNPLACED-SETTLEMENTS.md`](UNPLACED-SETTLEMENTS.md)
measures it per country.

Three things about it have since changed in the pipeline, and land in the next
release.

Containment now also reads **P150**, the parent's statement that it contains a
division, which places 27,476 of those settlements — Lithuania alone accounts
for 24,117 of them.

What still reaches no division is placed by its **coordinate**, against Natural
Earth's public-domain admin-1 boundaries. This is the one thing in the dataset
Wikidata does not decide, and it is fenced in accordingly: it answers with an
ISO 3166-2 code, the code must name a division already shipped for that
country, it cannot cross a border, and it runs only after both directions of
containment have failed. Where the code names a division at another level or
from an older ISO edition — Natural Earth gives France its départements and
this dataset ships régions — what it means here is learned from the settlements
already placed inside it, and only where they are at least 90% agreed. Over the
whole of the last release that places 45,666 settlements, on top of what P150
reaches. It is optional: a build without
`--boundaries` behaves as every released build has.

And `build-stats.json` now carries `country_region` per country, counted on the
rows that ship rather than on the containment failures alone, so the number can
be checked against the published file. `no_division` is unchanged and still
means what it always did: containment reached nothing.

A short hand-maintained list corrects the cases the rule cannot see, because
ISO itself carries entries that are not first-level divisions and nothing
distinguishes them from ones that are. Each is in the pipeline with its reason:

- **ISO groupings**, which overlap divisions rather than dividing anything:
  `GB-EAW` England and Wales, `GB-GBN` Great Britain, and Indonesia's seven
  geographical units (Java, Sumatra, Kalimantan and the rest, which sit beside
  its 38 provinces).
- **Abolished division types** that carry no end date upstream: every
  *prefecture of Greece*, all 51 replaced by the 13 regions in 2011; Morocco's
  `MA-MMD` and `MA-MMN`, superseded by the 2015 regions.
- **Lithuania's 60 municipalities**, which ISO lists beside the 10 counties.
  The county administrations were abolished in 2010 and Wikidata does not
  record the municipalities as being inside them, so the rule cannot separate
  the levels.
- **Six ISO codes claimed by two items each** — Sevastopol and "administrative
  and municipal division of Ukraine" both hold `UA-40`, Thessaly appears twice.
  One code now yields one division, lowest QID winning so a rebuild cannot
  flip which.

What remains, and is upstream rather than fixable here: **Vietnam** ships 35
against 63 provinces, because Wikidata files its subdivision classes beneath
"former subdivisions of Vietnam"; **Kenya** ships 43 against 47 counties.

## One name, one place

A region used to be able to contain the same name twice, 248,712 times over.
Two different things caused it and they are treated differently.

Wikidata often holds the **administrative unit and the built-up place at its
seat as separate items** -- "comune of Italy" beside "municipality seat",
"municipality of Colombia" beside "human settlement". Where two rows in one
region share a name and sit within 2 km of each other they are now one row,
keeping the lower id and absorbing the other's fields, because upstream fills
the pair differently. That merged 43,285 rows. Colombia and Guatemala were each
about a third duplicates this way; most countries were a few percent.

Beyond 2 km they are genuinely different places -- Germany has two towns called
Aach 80 km apart, Russia two villages called Chekhrak 25 km apart -- and
deleting one would delete a real place. Those are kept, and **their names are
qualified with the division below the region**: `Aach (Konstanz)`. 223,594
names (13.1%) carry such a qualifier.

The largest of a group keeps the plain name, because a village of 61,549 called
Bogota shares a region with the capital, and qualifying both would rename
Bogota. So a plain name answers "which place does this name usually mean" and a
qualifier answers "where is the other one".

**No region contains the same name twice.** That is a guarantee, not a
tendency, and it is why 1,892 rows are not shipped.

A name that identifies two places identifies neither. A consumer picking from a
list cannot see that the choice was ambiguous, so the wrong one is taken
silently and looks right. That makes an unidentifiable row worse than a missing
one, and three tiers decide which it is:

| | |
|---|---|
| the parent division | `Aach (Konstanz)` -- used where the parent has a name of its own that no other row in the group shares |
| a compass sector within the parent | `Bankati (north Gorakhpur)` -- for rows sharing one parent, where no ancestor can separate them. 4,875 names |
| nothing, so the row is dropped | 3,228 rows, no parent at all or nothing that distinguishes them |

A sector is only used where it narrows something. Two parents are refused: one
that *is* the region, because "east Alabama" inside region Alabama tells a
consumer nothing they have not already chosen -- five settlements called
Hopewell were separated only that way -- and one named after the settlement,
where the sector alone says as much and `Evergem (south Evergem)` becomes
`Evergem (south)`. 308 names take the short form.

**Upstream's own disambiguation is redone rather than kept.** Wikidata
brackets some of its labels — `Dushi (Baghlan Province)`, `Floq (Klos)` — by a
different rule and in a different format from this one, and two systems
produced the collision each was meant to prevent: this built `Floq (Klos)`
while upstream shipped `Floq, Klos`, and the two slugged alike. The bracket
now comes off 17,345 labels and the tiers above put back whatever is actually
needed. 2,077 pairs turned out to be the same place twice, within 2 km, which
the differing brackets had been hiding. The parent's own bracket comes off
before it is used to qualify, since a qualifier is a reference to the parent
rather than a rename of it: `Lindow (Mark)` and `Werder (Havel)` qualify as
`Lindow` and `Werder`, which distinguishes exactly as well inside one region
and does not put brackets inside brackets.

Square brackets are the same thing and were missed at first -- Mexico's
statistical office tags rows `[Nuevo Centro de Poblacion]`, German labels
disambiguate as `Baumgarten [Sonnenberg]`, 4,507 rows, most of them too short
for any length check to have found. A Russian administrative formation is
labelled by what kind of unit it is with the name inside guillemets --
`Gorodskoe poselenie <<Gorod Zavitinsk>>` is Zavitinsk -- which is another
1,830. Where nothing derived from the data can
tell two rows apart, the original bracket is put back rather than the row
dropped — 768 of them.

Exactly one row in each group keeps the plain name, since one of them is what
the name usually means -- the largest, unless another cannot be qualified at
all, in which case that one takes it and the rest are qualified around it.

Two villages of one name in one district is ordinary and merging them would be
wrong: of the pairs sharing a parent, 2,775 are more than 25 km apart. Russia,
India and Mexico account for half the dropped rows.

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

**The local name outranks a foreign rendering of it.** Only English and `mul`
come first, because a real English exonym is what an international consumer is
looking for; everything else loses to romanising the country's own label.
Romanisation used to be the last resort, which shipped Bulgarian villages under
Polish names (`Arda (obwod Chaskowo)`), Belarusian ones under Lithuanian and
Cebuano, and Chuvash ones under French -- 12,204 rows. Names still coming from
an unrelated language are 3.0%, and are almost all cases where that language
carries the clean form while the local label is parenthesised: `Cabildo`
against `Cabildo (ciudad)`.

### Timezone is 46.4%

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
is why some countries have a region with the same name as the country. Its `id`
is the country's own, its `iso_3166_2` is null, and its `place_type` is a
country class — any of the three identifies it.

**One row per place.** A settlement that reaches no division and that upstream
marks with P460 — *said to be the same as* — as being the same place as one
that does is dropped, and the row with the division is kept. That removed 111
rows from a build over the 11 August dump: a second "Warszawa" beside Warsaw, a
second "Łódź", "Cochin" beside Kochi, an item labelled "do not use" two hundred
metres from Stuttgart, and the ancient names of living cities — Ledra beside
Nicosia, Naissus beside Niš, Arbela beside Erbil. None of the 111 carries a
population and almost none carries any containment at all.

Distance is not consulted. P460 links confusable places as readily as identical
ones — Hoya in Lower Saxony to La Hoya in Salamanca, 1,778 km apart, or Loving
County to Mentone, the county and its seat — and in every such pair both sides
have a division, so asking which side is placed refuses them without measuring
anything. Where both are placed, or neither is, nothing is dropped. It runs
before the boundary lookup, because 92 of those rows fall inside a polygon and
would otherwise be filed beside the row they duplicate under a different name,
where nothing merges them.

**Archaeology is excluded.** Ghost towns, abandoned villages, hillforts,
Neolithic settlements and ancient cities are not current places and are left
out — about 55,000 rows, 20,914 of them German *Bodendenkmäler*: buried
monuments from the state heritage registers, named by their register id
(`Cultural heritage D-1-6933-0003 in Titting`). Those needed a categorical
exclusion rather than the former-entity one, because Wikidata tags them as
human settlements too — the site of a settlement two thousand years gone — and
they carry no end date, the monument designation being current.

Four more classes are excluded the same way and for the same reason — each is
tagged as a human settlement upstream, so nothing softer keeps it out:
**concentration camp** (the Alderney camps shipped as Guernsey settlements),
**prisoner-of-war camp** (Stalags and Oflags), **internment camp**, **labor
camp**, **corrective labor colony**, **clandestine centre of detention**,
**urban ensemble**, **urban layout** and
**group of houses** — a housing estate, which is part of a town and not one:
Polish *osiedla*, Berlin and Vienna *Wohnanlagen*, RNZAF married quarters.
A *Wohnanlage* is named by the streets it covers, so its label is a street
list, which is where most of the remaining absurd names came from.

**A refugee camp is not in that list and will not be.** Somewhere people are
confined is not somewhere people live; somewhere people have lived for decades
is. Kutupalong holds 598,195, Katumba 120,000, Ifo 84,181, Zaatari 79,000, and
someone living in one has to be able to say so. None of the exclusions above
reaches the refugee-camp class, and a test holds that line, because the two
categories are one subclass edge apart.
*Archaeological site* deliberately is not: its subclass closure is 585 classes,
79 of them settlement classes, so a categorical exclusion there would reach a
modern city that is also an ancient one.

A modern city that is *also* an ancient one (Rome,
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
- **A label over 100 characters is refused as a description**, which is the
  same judgement the parenthesis rule makes for the bot glosses that carry one.
  Names are short — the median is 11 characters and the 99th percentile 49 — so
  this cannot reach a real one. It removed street lists (`Wohnsiedlung
  Gontardweg 52; 53; 54; …`, 249 characters), Portuguese heritage ensembles,
  Polish urban-layout register entries, and one Honduran label consisting of a
  village name followed by a message to the author's friends. Names over 100
  characters went from 108 to 1.
- **An address is cut back to its name.** Wikidata's English labels for
  Russian villages are routinely the whole containment chain — `Pavlovskaya,
  Vozhegodsky Selsoviet, Vozhegodsky District, Vologda Oblast`. The name is the
  head; the rest is where it is, which `admin1_id` and `admin2_id` already
  record. 2,715 rows, cut only where there are at least two commas and an
  administrative word after the first, so `Washington, D.C.` and
  `Frankfurt, Oder` keep theirs.
- **Names are accent-folded.** `name` is ASCII; the unfolded form is not
  currently kept. Letters Unicode will not decompose -- Polish `ł`, Turkish
  dotless `ı`, Norwegian `ø`, Serbian `đ`, German `ß` -- are transliterated
  rather than dropped. They used to be deleted mid-word, so Chelmno shipped as
  `Chemno` and Ilica as `Ilca`.
- **Machine-transliterated names are not flagged as such.** They can be
  identified by the presence of an `alt_names` entry in a non-Latin script for
  the same language as `name_lang`.

## How to check any of this yourself

The build is byte-deterministic: the same Wikidata state produces the same
output and the same `s_version`. Every count above comes from
`build-stats.json`, which ships with each release and records per country how
many settlements were seen, how many reached no division, and how many were
dropped for having no usable name or no coordinates.
