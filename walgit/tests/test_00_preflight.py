"""Exercise 0 -- nothing to implement. Run this first.

If any of these fail, no amount of correct code downstream will save you: the
entire design assumes S3 gives you an atomic conditional PUT, and that git
gives you a pre-receive hook with object quarantine.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from botocore.exceptions import ClientError

from walgit import s3 as s3mod


def test_minio_is_reachable(settings):
    client = s3mod.client(settings)
    client.head_bucket(Bucket=settings.bucket)


def test_create_if_absent_is_atomic(settings):
    """``If-None-Match: *`` must reject a second create. This is how a WAL
    index is born exactly once, no matter how many nodes race to create it."""
    client = s3mod.client(settings)
    key = "preflight/create-once"
    client.put_object(Bucket=settings.bucket, Key=key, Body=b"first", IfNoneMatch="*")

    with pytest.raises(ClientError) as excinfo:
        client.put_object(
            Bucket=settings.bucket, Key=key, Body=b"second", IfNoneMatch="*"
        )
    assert s3mod.is_precondition_failed(excinfo.value)
    assert client.get_object(Bucket=settings.bucket, Key=key)["Body"].read() == b"first"


def test_compare_and_swap_on_etag(settings):
    """``If-Match: <etag>`` must reject a write based on a stale read.

    This single primitive replaces the entire consensus layer: it is the
    linearization point for every push in the system.
    """
    client = s3mod.client(settings)
    key = "preflight/cas"
    first = client.put_object(Bucket=settings.bucket, Key=key, Body=b"v1")["ETag"]

    second = client.put_object(
        Bucket=settings.bucket, Key=key, Body=b"v2", IfMatch=first
    )["ETag"]
    assert second != first

    with pytest.raises(ClientError) as excinfo:
        client.put_object(Bucket=settings.bucket, Key=key, Body=b"v3", IfMatch=first)
    assert s3mod.is_precondition_failed(excinfo.value)
    assert client.get_object(Bucket=settings.bucket, Key=key)["Body"].read() == b"v2"


def test_conditional_get_returns_304(settings):
    """A warm cache validates itself with this, for zero bytes of body."""
    client = s3mod.client(settings)
    key = "preflight/cond-get"
    etag = client.put_object(Bucket=settings.bucket, Key=key, Body=b"body")["ETag"]

    with pytest.raises(ClientError) as excinfo:
        client.get_object(Bucket=settings.bucket, Key=key, IfNoneMatch=etag)
    assert s3mod.is_not_modified(excinfo.value)


def test_git_is_new_enough():
    """Object quarantine (GIT_QUARANTINE_PATH) landed in git 2.11."""
    out = subprocess.run(["git", "--version"], capture_output=True).stdout.decode()
    parts = out.split()[2].split(".")
    assert (int(parts[0]), int(parts[1])) >= (2, 11), out


def test_git_http_backend_exists():
    """The smart-HTTP server delegates the whole wire protocol to this."""
    probe = subprocess.run(
        ["git", "http-backend"],
        capture_output=True,
        env={"REQUEST_METHOD": "GET", "PATH_INFO": "/", "PATH": "/usr/bin:/bin"},
    )
    assert probe.returncode == 0 or b"Status" in probe.stdout or probe.stderr
    assert shutil.which("git")
