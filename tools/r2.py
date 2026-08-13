"""Talking to the R2 bucket, which is where releases and the query cache live.

R2 speaks S3, so this is boto3 pointed at an account-specific endpoint. Two
things differ from S3 in ways that matter here:

  * egress is free, which is why releases are uploaded uncompressed. The
    manifest already carries a sha256 of each file's exact bytes, and a
    consumer that fetches one country should be able to verify it without
    decompressing first.
  * there are no regions. `region_name` has to be "auto".

Credentials come from three variables:

    R2_ACCOUNT_ID          the account the bucket belongs to
    R2_ACCESS_KEY_ID       from an R2 API token
    R2_SECRET_ACCESS_KEY

set in the environment, or in a `.env` in the repository root, which is read
automatically and is gitignored. See `.env.example`. Anything already exported
wins over the file, so a one-off `R2_BUCKET=... python tools/publish.py` works
as expected.

Create the token at Cloudflare dashboard -> R2 -> Manage API tokens, with
Object Read & Write on this bucket alone.

The endpoint is built from R2_ACCOUNT_ID and never defaults to anything. That
matters: an S3 client with no endpoint talks to AWS, and a machine with working
AWS credentials would then succeed against the wrong provider rather than fail.
Here a wrong account id is a DNS error.
"""

import concurrent.futures
import hashlib
import os
import sys
import threading


def _load_dotenv():
    """Read .env from the repository root into the environment.

    Deliberately does not overwrite anything already set, so an explicit export
    or a one-off VAR=... prefix beats the file. No dependency: the format is
    KEY=VALUE, # comments, and optional surrounding quotes.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('\'"')
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

BUCKET = os.environ.get('R2_BUCKET', 'location-data')

# What each format is, so a browser fetching a country renders rather than
# downloads it. R2 stores whatever it is told and serves it back verbatim.
CONTENT_TYPES = {
    '.json': 'application/json',
    '.ndjson': 'application/x-ndjson',
    '.csv': 'text/csv',
    '.gz': 'application/gzip',
    '.txt': 'text/plain',
    '.md': 'text/markdown',
}


def _require(name):
    value = os.environ.get(name)
    if not value:
        sys.exit('%s is not set. See docs/RELEASING.md -- an R2 API token with '
                 'Object Read & Write is needed to publish.' % name)
    return value


_local = threading.local()


def client():
    """One boto3 client per thread. Botocore clients are documented as
    thread-safe for calls, but sharing one across a pool serialises on its
    internal connection pool, and this uploads a thousand objects."""
    existing = getattr(_local, 'client', None)
    if existing is not None:
        return existing
    import boto3
    from botocore.config import Config
    account = _require('R2_ACCOUNT_ID')
    _local.client = boto3.client(
        's3',
        endpoint_url='https://%s.r2.cloudflarestorage.com' % account,
        aws_access_key_id=_require('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=_require('R2_SECRET_ACCESS_KEY'),
        # R2 has no regions, but the SDK insists on one.
        region_name='auto',
        config=Config(retries={'max_attempts': 5, 'mode': 'standard'},
                      max_pool_connections=32),
    )
    return _local.client


def content_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1], 'application/octet-stream')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def put(local_path, key, bucket=BUCKET):
    client().upload_file(local_path, bucket, key,
                         ExtraArgs={'ContentType': content_type(local_path)})
    return key


def put_bytes(data, key, bucket=BUCKET, content='application/json'):
    client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content)
    return key


def get(key, local_path, bucket=BUCKET):
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    client().download_file(bucket, key, local_path)
    return local_path


def exists(key, bucket=BUCKET):
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if error.response['Error']['Code'] in ('404', 'NoSuchKey', '403'):
            return False
        raise


def read_json(key, bucket=BUCKET):
    import json
    from botocore.exceptions import ClientError
    try:
        body = client().get_object(Bucket=bucket, Key=key)['Body'].read()
    except ClientError as error:
        if error.response['Error']['Code'] in ('404', 'NoSuchKey'):
            return None
        raise
    return json.loads(body)


def put_many(pairs, bucket=BUCKET, workers=16, label='uploading'):
    """[(local_path, key), ...] -> number uploaded, with progress.

    A release is about a thousand small objects; done one at a time the round
    trips dominate and it takes minutes rather than seconds.
    """
    done = 0
    total = len(pairs)
    lock = threading.Lock()

    def one(pair):
        nonlocal done
        put(pair[0], pair[1], bucket)
        with lock:
            done += 1
            if done % 100 == 0 or done == total:
                print('  %s %d/%d' % (label, done, total), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, pairs):
            pass
    return total
