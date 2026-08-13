"""Purging the CDN in front of the bucket, after a release is uploaded.

R2 is the origin; geo.mindstellar.com is a Cloudflare custom domain with cache
rules on top, and those rules hold release files for 30 days. That TTL is the
point -- a release path is immutable, so a consumer should never re-fetch one
-- but it means the edge does not notice a write. Two cases follow:

  * a normal release writes new paths, which were never cached, and moves
    releases/latest.json, which is held for 60 seconds. Purging the pointer
    makes a release visible immediately instead of within a minute.

  * a --force republish overwrites paths the edge is already holding for 30
    days. Nothing expires them. This has already happened once: the release
    was replaced in the bucket and the edge kept serving the old bytes, and
    the sha256 in the new manifest did not match what anyone actually
    received. That is the case this exists for, and it needs the whole host
    purged rather than one URL.

Every purge method -- single file, prefix, hostname, tag, everything -- became
available on all Cloudflare plans in April 2025, so purging by hostname works
on the Free plan this zone is on. Hostname rather than purge_everything on
purpose: geo.mindstellar.com is one host inside the mindstellar.com zone, and
purge_everything would throw away the cache of every other site in it.

Two variables, in .env beside the R2 ones or in the environment:

    CF_API_TOKEN    a token with Zone.Cache Purge on this zone alone
    CF_ZONE_ID      the zone the custom domain belongs to

Create the token at Cloudflare dashboard -> Manage Account -> API Tokens, with
the "Purge Cache" template scoped to mindstellar.com. It is a different token
from the R2 one and cannot be substituted for it.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import r2  # noqa: E402  -- imported for its .env loader

API = 'https://api.cloudflare.com/client/v4'

# The host the bucket is published under. Not derived from the bucket name:
# the mapping between a bucket and a custom domain lives in Cloudflare, not
# here, and guessing it wrong would purge nothing and report success.
PUBLIC_HOST = os.environ.get('PUBLIC_HOST', 'geo.mindstellar.com')


def configured():
    return bool(os.environ.get('CF_API_TOKEN') and os.environ.get('CF_ZONE_ID'))


def _post(body):
    request = urllib.request.Request(
        '%s/zones/%s/purge_cache' % (API, os.environ['CF_ZONE_ID']),
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer %s' % os.environ['CF_API_TOKEN'],
                 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', 'replace')[:400]
        raise RuntimeError('purge failed: HTTP %d %s' % (error.code, detail))
    if not payload.get('success'):
        raise RuntimeError('purge failed: %s' % json.dumps(payload.get('errors')))
    return payload


def purge_urls(paths):
    """Single-file purge of specific keys, given as bucket-relative paths."""
    urls = ['https://%s/%s' % (PUBLIC_HOST, path.lstrip('/')) for path in paths]
    _post({'files': urls})
    return urls


def purge_host():
    """Everything the edge holds for the published host, and nothing belonging
    to any other host in the zone."""
    _post({'hosts': [PUBLIC_HOST]})
    return PUBLIC_HOST
