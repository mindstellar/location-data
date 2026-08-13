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
export R2_ACCOUNT_ID=...          # also visible in any R2 endpoint URL
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
```

Put them somewhere your shell reads and **not in this repository**. Nothing
here writes them to disk, and `tools/r2.py` reads them from the environment
only.

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

## What lands in the bucket

```
releases/<version>/json-list.json     the manifest a consumer reads first
releases/<version>/LICENSE            CC0, beside the data it applies to
releases/<version>/data/<CC>.ndjson   canonical records, 21 fields
releases/<version>/json/…             per-country, the shape installs fetch
releases/<version>/csv/…
releases/<version>/ndjson/…
releases/latest.json                  which version is current
cache/<version>.tar.gz                the query cache, one object
cache/latest.json
```

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
