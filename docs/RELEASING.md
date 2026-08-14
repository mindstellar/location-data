# Releasing

The dataset is published to Cloudflare R2. There is no CI: the refresh runs on
a machine you own, and this explains how.

## Why not GitHub

Three reasons, in order of weight.

The dump is 67 GB and the scan holds a machine for about ninety minutes. That
is the wrong shape for a hosted runner, and paying for it monthly to do work a
desktop does for free is hard to justify.

Git is the wrong store for the output. A release is 1.9 GB across ~1,020
files and almost all of it changes every month, because a refresh renames and
reparents real administrative divisions. Committing that is a repository that
grows by two gigabytes a month and can never be made smaller again.

And a refresh should be read before it ships. The diff is not noise — it is
countries gaining and losing subdivisions — and the person who understands
whether that is upstream reality or a bug in here is the person running it.

## What you need once

An R2 API token. Cloudflare dashboard → R2 → **Manage API tokens** → *Create
API token*, with **Object Read & Write**, scoped to the `location-data` bucket
alone. It gives you three values:

```bash
cp .env.example .env && chmod 600 .env      # then fill in the two key values
```

`.env` is gitignored and read automatically, so nothing needs exporting.
Anything already in the environment wins over the file, so a one-off
`R2_BUCKET=scratch python tools/publish.py …` behaves as expected.

Exported variables work equally well if you would rather keep credentials out
of the working tree entirely:

```bash
export R2_ACCOUNT_ID=...          # also visible in any R2 endpoint URL
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
```

Worth making **two** tokens. Object Read & Write for publishing, and a separate
Object Read Only for anything that only consumes releases — an import, a
Worker, a restore. The read-only one ends up in more places and cannot
overwrite a release if it leaks.

Do not put R2 credentials in `~/.aws/credentials` alongside AWS ones. R2 only
works because the endpoint points at `<account>.r2.cloudflarestorage.com`; an
S3 client with no endpoint talks to AWS instead, and on a machine with working
AWS credentials that mistake *succeeds* against the wrong provider rather than
failing. Keeping the two apart makes it impossible.

## The whole refresh

```bash
python tools/refresh.py --work-dir ~/development/wikidata-dump
```

Five stages, each idempotent, so an interrupted run is resumed by running it
again:

| stage | what it does | roughly |
|---|---|---|
| `cache` | pulls the query cache from R2 if it is not on disk | 2 min |
| `dump` | fetches the truthy dump if it is not on disk | 50 min |
| `scan` | stage 1, the streaming pass over 8.2 billion triples | 90 min |
| `build` | stage 2, assembling every country | 5 min |
| `publish` | validates, then uploads a release and backs the cache up | 5 min |

`--skip dump,scan` to rebuild from a scan you already have. `--dry-run` does
everything except write to R2. `--rescan` forces stage 1 to redo itself.

Publishing is gated on `validate.py`, which compares the new build against the
currently published release — not against a git ref, and not against nothing.
Without credentials it fails rather than skipping, so the gate cannot quietly
disappear. On the very first publish there is nothing to compare against and it
says so.

**A month in which nothing changed produces no release.** The build is byte
deterministic, so an unchanged Wikidata gives an identical `s_version`, and
`publish.py` refuses to mint a release that already exists. That is the same
property the abandoned GitHub workflow depended on, and it is worth keeping.

## Two buckets, and why

`location-data` holds releases and is published at **https://geo.mindstellar.com**.
`location-data-cache` holds the query-cache backup and has no domain, ever.

They are separate because a custom domain publishes an entire bucket. With the
cache in the same one, a 173 MB tarball of SPARQL responses would sit on a
public URL next to the data, get crawled, and serve no one. Separate buckets
make that impossible rather than merely discouraged. Scope publishing tokens to
both; a token for only one will fail with `AccessDenied` on the other, which is
how it should behave.

## What lands in the bucket

```
releases/<version>/manifest.json      the catalog a consumer reads first
releases/<version>/LICENSE            CC0, beside the data it applies to
releases/<version>/data/<CC>.ndjson   canonical records, streamable
releases/<version>/json/<CC>.json     the same record, nested
releases/<version>/csv/<CC>.csv       the same record, flat
releases/latest.json                  which version is current
```

and in `location-data-cache`, which is not public:

```
cache/<version>.tar.gz                the query cache, one object
cache/latest.json
```

## The edge

`geo.mindstellar.com` is an R2 custom domain, not the managed `r2.dev` one.
That matters: `r2.dev` is rate-limited, documented as non-production, and does
not get the CDN cache, so every request would reach R2.

Two cache rules on the zone, and **their order is load-bearing**. Every
matching rule in a cache ruleset applies in sequence and the last one wins, so
the broad rule comes first and the specific override second:

1. `/releases/*` — 30 days at the edge, 1 day in the browser. Versioned files
   never change.
2. `/releases/latest.json` — 60 seconds. This is how a new release is noticed;
   with the rules the other way round it inherited the 30-day TTL and a
   release would have been invisible for a month.

Two things about the edge that are not obvious and both cost time to find.

**A cache rule change does not purge what is already cached.** `releases/latest.json`
was first cached while it still inherited the 30-day rule, so after the rules
were fixed the edge kept serving a three-hour-old pointer with `age: 10459` and
`cache-control: max-age=60` side by side. New entries honour the new rule;
existing ones do not.

`tools/publish.py` purges as part of every release when it can. It is best
effort rather than a precondition -- the release is in R2 by the time the purge
runs, and a bucket does not have to have a CDN in front of it -- so without
credentials it says so and carries on. Two variables turn it on, in `.env`
beside the R2 ones:

```
CF_API_TOKEN    a token with Zone.Cache Purge, scoped to this zone alone
CF_ZONE_ID      the zone geo.mindstellar.com belongs to
```

Create the token under **Manage Account -> API Tokens** with the "Purge Cache"
template. It is not the R2 token and cannot be substituted for it.

What gets purged depends on what was written. An ordinary publish writes a new
version prefix the edge has never seen, so only the pointer and the new
manifest are cleared -- without that the pointer's own 60-second TTL would
carry it, a minute later. A `--force` republish overwrites paths the edge is
holding for 30 days and nothing expires them, so the whole host is purged:

```
POST /zones/<zone>/purge_cache   {"hosts": ["geo.mindstellar.com"]}
```

By hostname rather than `purge_everything`, which would throw away the cache of
every other site in the `mindstellar.com` zone. Every purge method became
available on all Cloudflare plans in April 2025, so this works on the Free plan
this zone is on. `--no-purge` skips it even when the credentials are present.

When nothing purged, `releases/latest.json` clears itself within 60 seconds and
the release becomes visible on its own. A `--force` republish does not: those
paths are held for 30 days and need purging by hand.

After changing a cache rule, purge by hand -- publish only purges what it
wrote, and a rule change affects everything already cached.

**The zone's security settings blocked a legitimate client.** Browser Integrity
Check returned 403 to `Python-urllib/3.11` — not the WAF, not Bot Fight Mode,
which were both off or irrelevant. `python-requests`, `curl`, `wget`, Go and
Java all passed, so it was invisible until something fetched with the Python
standard library. A custom firewall rule now skips `bic`, `uaBlock`,
`securityLevel`, `waf`, `hot` and `zoneLockdown` for `geo.mindstellar.com`
only; every other hostname on the zone keeps them. A host serving immutable
public files with no auth and no writes has nothing for those products to
protect, and they can only produce false positives like that one.

The edge also compresses on the fly, which is what makes storing the files
uncompressed the right call rather than a compromise. Measured on the worst
case, `json/MX-Mexico.json`: **76.5 MB uncompressed, 5.7 MB over the wire with
Brotli, 13.5x**. The client decompresses transparently, so the sha256 in the
manifest still verifies against what it receives — the thing pre-compressed
objects would have broken.

Uncompressed, deliberately. The manifest carries a sha256 of each file's exact
bytes, and a consumer that fetches one country should be able to verify what it
received without decompressing first. 1.9 GB of R2 storage is about three cents
a month and egress is free, so the only real cost is the consumer's.

That cost is not nothing, and it is worth knowing before it surprises anyone:
the largest single file is `json/MX-Mexico.json` at **76.5 MB**, followed by
Russia at 58.4 MB. An install fetching Mexico downloads all of it. Gzip would
take that to roughly a tenth, and the reason it is not done is that
pre-compressed objects served with `Content-Encoding: gzip` are a footgun —
`curl` without `--compressed` saves the compressed bytes under a `.json` name,
and the sha256 in the manifest then matches nothing the user can see. If
bandwidth ever matters more than that, the clean fix is a custom domain letting
Cloudflare compress on the fly, which changes nothing about what is stored.

The cache is the exception and goes up as a single tarball: 3,413 files and
3.2 GB uncompressed, which would otherwise be 3,413 round trips to restore, and
it is hours of querying a slow, rate-limiting endpoint rather than anything
that can be regenerated cheaply.

## Publishing by hand

```bash
python tools/publish.py status                       # what is currently out
python tools/publish.py release <build-dir> --dry-run
python tools/publish.py release <build-dir>
python tools/publish.py cache ~/development/wikidata-cache-backup
```

`--version` defaults to today's date. `--force` overwrites an existing version,
and is the only way to replace a release rather than add one.

## Access

The bucket is **private**. Turning on public access is a separate, deliberate
act — either the `r2.dev` managed domain or a custom domain — and it is the
moment the data becomes downloadable by anyone, so it is not something a
publish script should do as a side effect.

Until then, restoring or consuming a release needs the same credentials as
publishing.
