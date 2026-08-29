"""Thin S3 helpers. Provided for you -- no exercises live in this file.

The only interesting thing here is turning botocore's stringly-typed errors
into predicates you can branch on. Three of them carry the whole design:

    is_not_found          -- the object has never existed
    is_precondition_failed -- 412: your If-Match / If-None-Match was refused,
                              i.e. you lost a compare-and-swap race
    is_not_modified       -- 304: your cached ETag is still current, the
                              object did not change, no body was transferred
"""

from __future__ import annotations

import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import Settings

_clients: dict[tuple, object] = {}
_lock = threading.Lock()


def client(settings: Settings):
    """Return a (cached, thread-safe) boto3 S3 client for these settings."""
    key = (settings.endpoint_url, settings.access_key, settings.region)
    with _lock:
        if key not in _clients:
            _clients[key] = boto3.client(
                "s3",
                endpoint_url=settings.endpoint_url,
                aws_access_key_id=settings.access_key,
                aws_secret_access_key=settings.secret_key,
                region_name=settings.region,
                config=Config(
                    retries={"max_attempts": 3, "mode": "standard"},
                    max_pool_connections=64,
                    signature_version="s3v4",
                ),
            )
        return _clients[key]


def ensure_bucket(settings: Settings) -> None:
    c = client(settings)
    try:
        c.head_bucket(Bucket=settings.bucket)
    except ClientError as exc:
        if http_status(exc) in (404, 403, 301):
            c.create_bucket(Bucket=settings.bucket)
        else:
            raise


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def http_status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def is_not_found(exc: ClientError) -> bool:
    return http_status(exc) == 404 or error_code(exc) in ("404", "NoSuchKey", "NotFound")


def is_precondition_failed(exc: ClientError) -> bool:
    """412 -- an If-Match / If-None-Match precondition was refused."""
    return http_status(exc) == 412 or error_code(exc) == "PreconditionFailed"


def is_not_modified(exc: ClientError) -> bool:
    """304 -- conditional GET says your cached copy is still current."""
    return http_status(exc) == 304 or error_code(exc) == "304"


def normalize_etag(etag: str | None) -> str | None:
    """S3 hands back ETags wrapped in literal quotes. Keep them; strip nothing.

    They must be echoed back verbatim in If-Match, so this only guards against
    a caller that stripped them somewhere along the way.
    """
    if etag is None:
        return None
    if not etag.startswith('"'):
        return f'"{etag}"'
    return etag
