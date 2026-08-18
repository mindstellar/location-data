# The country-named region, and what actually lands in it

`LIMITATIONS.md` describes the fallback in one paragraph:

> Each country also has **one region named after the country itself**, which
> catches settlements whose containment reaches no division. It appears only if
> something lands in it, which is why counts here are typically ISO + 1.

That is accurate about the mechanism. This note is about its size, which is
larger than `build-stats.json` reports, and about one country where it holds
almost everything.

Measured against release `2026-08-15T0426Z`, over every `data/<CC>.ndjson`.

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

## Directions

Nothing here is prescriptive; the containment code will suggest better options
than a consumer can.

1. **Count the fallback where it happens.** A settlement that lost its division
   to the exclusion list is not `no_division`, and today nothing records it.
   A separate counter would make `build-stats.json` match the shipped files and
   would have made Lithuania visible immediately.
2. **Mark the region rather than naming it after the country.** A flag, or a
   type that is not `region`, lets a consumer tell a fallback from a division
   without inferring it from `id`, `iso_3166_2` and `place_type`.
3. **Lithuania specifically.** The counties were abolished in 2010 and Wikidata
   does not nest municipalities under them, so the rule cannot bridge the two
   levels — but shipping the municipalities as regions for this one country
   would place 23,193 settlements that currently have no parent.
4. **Point-in-polygon as a last resort.** Every row in the bucket has
   coordinates, so most could be placed geometrically rather than through the
   containment graph.

## Downstream note

`placedb.org`, which serves these releases, relabels the region as
`"name": "Division not recorded"` with `"unassigned": true`, strips the
country's population and centroid from it, sorts it last, and keeps it out of
autocomplete suggestions while its settlements stay searchable. That is
presentation only — it reassigns nothing — and it comes out when a release
changes the behaviour.
