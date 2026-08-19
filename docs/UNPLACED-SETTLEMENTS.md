# The country-named region, and what actually lands in it

`LIMITATIONS.md` describes the fallback in one paragraph:

> Each country also has **one region named after the country itself**, which
> catches settlements whose containment reaches no division. It appears only if
> something lands in it, which is why counts here are typically ISO + 1.

That is accurate about the mechanism. This note is about its size, which is
larger than `build-stats.json` reports, and about one country where it holds
almost everything.

Measured against release `2026-08-15T0426Z`, over every `data/<CC>.ndjson`.
What the pipeline has since been changed to do about it is at the
[end](#what-the-pipeline-does-about-it-now); everything before that describes
the release as it shipped. **A build over the same 11 August dump now leaves
5,477 settlements in a country-named region rather than 57,250**, and the
figures in the later sections are measured on that build rather than projected.

## How big it is

**57,250 settlements sit in a country-named region, across 249 of 255
countries** — 3.4% of the dataset.

`build-stats.json` totals **52,734** for `no_division` over the same release.
The two numbers describe different things, and the gap is the point of this
note.

## Why the two disagree

`no_division` counts settlements whose containment chain reached no division at
all. But a settlement also lands in the country-named region when its division
*was* resolved and then removed — the hand-maintained exclusions in
`LIMITATIONS.md`: ISO groupings, abolished division types, Lithuania's 60
municipalities. Containment succeeded, so the counter does not fire; the
settlement still has nowhere to go.

**154 countries hold more in the bucket than `no_division` reports.**

| cc | in the country region | `no_division` | gap | share of the country |
|---|---|---|---|---|
| LT | 23,193 | 43 | 23,150 | 99% |
| GR | 708 | 215 | 493 | 5% |
| KR | 456 | 51 | 405 | 13% |
| SI | 388 | 53 | 335 | 6% |
| BI | 481 | 148 | 333 | 81% |
| NP | 400 | 76 | 324 | 19% |
| VI | 216 | 0 | 216 | 94% |
| CI | 295 | 93 | 202 | 21% |
| FO | 149 | 0 | 149 | 100% |
| SG | 85 | 8 | 77 | 100% |

Faroe Islands and Singapore report `no_division: 0` while every settlement they
ship is in the bucket.

## Lithuania

This is the documented municipality exclusion, priced:

```
23193  —        Lithuania      <- the country-named region
  114  LT-VL    Vilnius County
   27  LT-KU    Kaunas County
   22  LT-SA    Siauliai County
   19  LT-PN    Panevezys County
   12  LT-KL    Klaipeda County
    6  LT-UT    Utena County
    5  LT-MR    Marijampole County
    5  LT-TA    Taurage County
    1  LT-TE    Telsiai County
    0  LT-AL    Alytus County
```

The counties were kept and the municipalities dropped, which is the consistent
choice under the no-ISO-parent rule. But Wikidata files Lithuanian settlements
under municipalities, so 99% of them lost their only parent. `build-stats.json`
records `no_division: 43` for Lithuania, three orders of magnitude below what
the shipped file shows.

A consumer building a division picker gets ten Lithuanian counties holding 211
settlements between them, and no signal that 23,193 more exist one level up.

## What a consumer sees

The fallback region is a normal `region` record carrying the country's own
identity:

```json
{"type":"region","id":668,"name":"India","slug":"india","iso_3166_2":null,
 "place_type":"Q6256","latitude":"22.800000","longitude":"83.000000",
 "population":1326093247}
```

Its `id` equals the country record's `id`, `iso_3166_2` is null, and
`place_type` is a country class (`Q3624078` sovereign state, `Q6256` country)
rather than any division type. Those three are reliable ways to detect it.

The rows inside are ordinary settlements with correct coordinates — they are
filed one level too high, not damaged. Plotting India's 966 against state
bounding boxes scatters them nationwide: roughly 125 in Maharashtra, 59 in
Rajasthan, 57 in Uttar Pradesh, 31 in West Bengal, and about 490 in the
southern states.

## Reproducing

```bash
python3 - <<'PY'
import json, collections
cid = None; regions = {}; counts = collections.Counter()
for line in open('data/LT.ndjson', encoding='utf-8'):
    r = json.loads(line); t = r.get('type')
    if t == 'country': cid = r['id']
    elif t == 'region': regions[r['id']] = r
    elif t == 'settlement': counts[r.get('admin1_id')] += 1
print('country id', cid)
print('settlements attached to it:', counts[cid])
print('also published as a region:', cid in regions)
PY
```

## What the pipeline does about it now

Three changes, all in the repository and none of them in the release measured
above: containment now reads P150, coordinates place what containment cannot,
and the stats count the region itself.

**Containment reads P150 as well.** Wikidata records containment from both
ends: P131 is the child naming its parent, P150 is the parent listing its
children. Where the first is missing the second often is not, and Lithuania is
the extreme case — all 60 municipalities point P131 straight at Lithuania,
because the county edge carries a 2010 end date and is not truthy, while the
ten counties still list their municipalities under P150.

The walk runs twice. P131 alone first, as before; then again with P150 read
backwards alongside it, seeded from the divisions only. A settlement takes the
second answer only where the first got it no further than its own country, and
only for a division of the country it already belongs to — so a statement made
by a parent can never outrank one made by the child, and can never move a place
across a border.

Measured against the same release, by walking the affected settlements'
containment out to Wikidata:

| cc | in the country region | placed by P150 | |
|---|---|---|---|
| LT | 23,193 | 22,070 | 95% |
| GR | 708 | 553 | 78% |
| NP | 400 | 52 | 13% |
| KR | 456 | 8 | 2% |
| SI, BI, CI, IN, FO, SG | 2,364 | 0 | — |

On the real build, 27,476 settlements in total. Ten Lithuanian counties go from
211 settlements between them to 23,456, and Lithuania's country-named region
falls from 23,193 to seven.

Spot-checked against geography rather than trusted: every one of the 22,070
Lithuanian rows lands within 120 km of the centroid of the county it is
assigned to, and 82% land in the county whose centroid is nearest of the ten.
The Greek outliers are the Sporades — Alonnisos, Peristera, Piperi — which are
genuinely Thessaly, 180 km from its mainland centroid.

**`build-stats.json` counts the region itself.** A new `country_region` per
country, counted on the rows that ship, plus `in_country_region` over the whole
build, and `placed_by_contains` and `placed_by_boundary` for how the rest were
rescued. `no_division` is untouched and still counts what it always counted, a
failure of containment; the gap between the two is the exclusion list doing its
job, and is now visible instead of implied.

## And then by coordinate

What the containment graph cannot reach, geometry can. India is the shape of
it: 863 of its 966 have no P131 statement in either direction — not a wrong
one, none — and carry only a country, a class and a point. `74 GB`, a village
in Sri Ganganagar district, is one of them, at 29.189069/73.209678 with nothing
else recorded. A post office a few hundred metres away, `78 Gb Branch Post
Office`, does state its district; the village does not, and a post office is
not a settlement, so nothing in the dataset connects them.

So the last resort tests the settlement's own coordinate against **Natural
Earth's** admin-1 boundaries, which are public domain. `74 GB` falls inside
`IN-RJ` and ships under Rajasthan.

Three things keep it narrow, and are why it can be allowed at all:

- It answers with an **ISO 3166-2 code**, never a place, and the code must name
  a division this build already selected for that country. Natural Earth
  carries superseded ISO editions — Nepal's zones rather than its seven
  provinces, Ivory Coast's nineteen regions rather than its fourteen districts
  — and those answers are dropped rather than approximated.
- It cannot cross a border. The code has to belong to the country the
  settlement is already filed under.
- It runs **last**, after P131 and after P150. A stated parent always beats a
  polygon, because a boundary file is a rendering of an administrative fact
  rather than the fact itself.

Measured over the same release, on the fallback bucket of the ten worst
countries — this is the code matching by string equality, before the mapping
below:

| cc | in the country region | placed by boundary | |
|---|---|---|---|
| LT | 23,193 | 22,900 | 99% |
| GR | 708 | 684 | 97% |
| SI | 388 | 378 | 97% |
| SG | 85 | 81 | 95% |
| IN | 966 | 870 | 90% |
| KR | 456 | 44 | 10% |
| BI, CI, NP, FO | 1,325 | 1 | — |
| **total** | **27,121** | **24,958** | **92%** |

The four at the bottom are the stale-ISO cases above, plus the Faroes, which
ship no ISO-coded divisions at all for a code to match.

Where P150 and the polygons both have an opinion — Lithuania, mostly — they
agree on 21,090 and differ on 979, all of them within a few kilometres of a
county line, and P150 wins every one of those because it runs first. In one
case Natural Earth put a Lithuanian village in `LV-DGV`, across the Latvian
border, and the same-country rule refused it.

## Reading a code that means something else

Matching codes as strings leaves a large piece behind, and France is all of it:
4,220 unplaced settlements, every one of them inside a Natural Earth polygon,
none of those polygons named by a code this dataset ships. Natural Earth gives
France its *departements* — `FR-69`, `FR-75`, `FR-13` — and the root-most rule
here selects the 2016 *regions*, `FR-ARA`, `FR-IDF`, `FR-PAC`. Poland is the
same country in two ISO editions: lettered voivodeships upstream, numbered ones
here. Italy is provinces against regions, Britain is districts against England,
Scotland, Wales and Northern Ireland.

None of those can be matched by string equality and all of them are exact
nestings, so the mapping is read off the data instead of written down. Every
settlement whose division containment already established votes for what the
polygon it sits in means. A departement's settlements are unanimous about their
region — that is what a nesting produces — and two divisions that genuinely
disagree about the same ground are not, which is what a purity floor of 90%
over at least five settlements refuses.

Nothing is hand-maintained, and nothing needs revisiting when ISO or Natural
Earth moves: the vote simply comes out differently.

Measured over the whole release, on all 57,196 settlements in a country-named
region across 254 countries:

| | |
|---|---|
| placed by a division stating it contains them (P150) | **27,476** |
| placed by a code that matches one we ship | **38,675** |
| placed by reading what a code means here | **6,991** |
| dropped as one place recorded twice (P460) | **111** |
| **still in the country-named region** | **5,477** |

That is the whole release, measured: 57,250 down to 5,477, across 209 countries
rather than 249. France ends at 85, India at 63, Greece at 17, Poland at 2,
Lithuania at 7.

France alone accounts for 4,135 of the second row, at 99.6% purity; Britain 479
at 99.9%, Italy 263 at 99.4%, Spain 189 at 99.8%, Poland 308 at 96.2%.

## Duplicates, which this creates

A settlement moving out of the bucket meets its new region's names for the
first time, and some of them are its own. India has 91 such rows: 44 within the
2 km radius that means one place recorded twice, and 47 further off. The issue
named one — `Agra` sits in the bucket 3.8 km from the `Agra` already under
`IN-UP`.

Nothing new was needed for this. `resolve_collisions` already runs per region
after grouping, merges what is one place, qualifies what is two, and drops what
nothing can tell apart; it simply now sees these rows. The effect is that the
city count *falls* slightly where a bucket row turns out to be a duplicate of a
properly placed one — on the seven-country test fixture, 376 rows placed and 42
of them absorbed into rows that were already there.

## What is left

**5,477 settlements, 0.33% of the dataset**, in four situations:

| | |
|---|---|
| the polygon means nothing here | 2,852 |
| no polygon covers the point | 1,883 |
| the polygon is another country's | 856 |
| the country places nothing at all | 362 |

Worst remaining: BI 473, KR 403, MA 335, MG 313, NP 308, LT 293, CI 217,
VI 216, RU 177, PS 173, FO 149. France comes out at 85 from 4,220, India at 64
from 966, Lithuania at 293 from 23,193 — and Lithuania's are border cases that
P150 places before the boundaries are consulted at all.

Almost none of these are places with no division. They are places whose
division exists and which no evidence available here reaches:

- **The country reorganised and no public-domain boundary has caught up.**
  Burundi, South Korea, Nepal, Ivory Coast, Morocco, Palestine, North
  Macedonia. Every one of them ships real ISO-coded divisions — Korea 14,
  Macedonia 69 — and every one of these settlements is inside one. Natural
  Earth still draws the pre-reform divisions, which do not nest into the
  current ones, so there is nothing for the vote to learn. It resolves itself
  when Natural Earth updates.
- **The point is in no polygon at all** — small islands Natural Earth does not
  draw at 1:10m, and coastlines generalised inland of where the settlement
  sits. Kiribati settles what this means: all 105 of its unplaced settlements
  fall inside the longitude band of one of the three island groups the dataset
  already ships, which lie a thousand kilometres and more apart. 85 are in the
  Gilberts, 13 in the Line Islands, 5 in the Phoenix Islands. Nothing about
  them is unknown; they are atolls too small to be drawn.
- **The point is inside another country's polygon**, near a border, where a
  generalised boundary crosses the wrong side of a village.
- **The country places nothing at all**, which is the only category with a
  claim to being genuinely division-free, and mostly is not — see below.

## The 21 countries that ship one region

A country whose only region is named after it holds no fallback: that region
*is* the country. Twenty-one are in this state, holding 326 settlements between
them, and **237 of those settlements have a containing entity recorded
upstream**. They are not division-free; the division tiers did not find their
divisions.

The Faroe Islands are the whole story in one country. 149 settlements, 147 of
them filed under one of **33 municipalities**, which roll up cleanly into the
**six sýslur** — Eysturoy 47, Streymoy 38, Norðoyar 19, Suðuroyar 11, Vága 8,
Sandoyar 6. Wikidata records the entire hierarchy. The dataset ships one region
called "Faroe Islands".

The reason was narrow, and it is fixed. Tier 2 selects the country's
administrative P131 children, and the sýslur state no P131 at all — not a wrong
parent, none — so nothing was found and the country became its own region. What
the settlements themselves point at was never asked.

**Tier 4 now asks it.** For a country no other tier resolves, the pipeline
takes the entities that country's settlements say contain them, and what
contains those, and applies the same root-most rule used everywhere else. The
Faroes come out as their sýslur rather than their 33 municipalities, for the
same reason France comes out as régions rather than départements. On the real
build that is one region holding all 149 settlements before, and five sýslur
holding 120 after, with 29 left over.

It has to be asked in the right place, and was not at first. Tier 4 originally
ran only where tier 2 found nothing, and the countries that need it are the
ones where tier 2 finds something useless: a handful of administrative items do
point P131 at the Faroe Islands, so tier 2 answered, and 0 of 155 settlements
attached to what it chose. A full build selected tier 4 for no country at all.
It is now tried where a tier is seen to have attached nothing, and taken only
if it places more than the tier it replaces.

Two things keep it from running wild, both learned the hard way on the Cook
Islands and the Vatican. Asking what the *country* claims rather than what its
*settlements* claim gave the Cook Islands 26 regions — its own villages, and an
electorate — and gave the Vatican a charity. And a municipality is a settlement
by this pipeline's rule, so the chain is walked *through* it rather than
stopping at it; stopping made Avarua, a town, a region holding the suburb next
to it.

One dependency worth naming: a division reachable only this way still has to
have survived the scan, which keeps an entity for its coordinate, its
containment, its subclass or its ISO code. A division with none of those —
no coordinate, no parent, no code — is not in the scan to be found.

What is left after that is genuinely division-free, and it is a much smaller
set: Sint Maarten's 55, the Vatican's
2, and a scattering of dependencies that really have no subdivision — Gibraltar,
Christmas Island, the Cocos Islands, Pitcairn. On the order of a hundred rows,
not six thousand.

What would close most of the rest is a vote of the *nearest already-placed
settlements* rather than of a polygon: on held-out data it answers for 10,962
of the 12,638 that were left before the code mapping ran, and reproduces the
known division 96% of the time. It is not used. 96% over six thousand rows is
two hundred and forty settlements confidently filed in the wrong division, and
this dataset's position is that a well-formed wrong answer is worse than a
visibly absent one — which is the argument the issue that started this made in
the first place.

## Downstream note

`placedb.org`, which serves these releases, relabels the region as
`"name": "Division not recorded"` with `"unassigned": true`, strips the
country's population and centroid from it, sorts it last, and keeps it out of
autocomplete suggestions while its settlements stay searchable. That is
presentation only — it reassigns nothing — and it comes out when a release
changes the behaviour.
