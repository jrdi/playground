"""Shared fixtures. Every test runs against a real MinIO and real git."""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from walgit import s3 as s3mod
from walgit.objects import ContentStore
from walgit.config import Settings
from walgit.server import make_server
from walgit.wal import RefUpdate, WalEntry, WalLog

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
}


def run_git(*args: str, cwd: Path, check: bool = True, env_extra: dict | None = None):
    env = {**GIT_ENV, **(env_extra or {})}
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, env=env
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode})\n"
            f"stdout: {proc.stdout.decode(errors='replace')}\n"
            f"stderr: {proc.stderr.decode(errors='replace')}"
        )
    return proc


# --------------------------------------------------------------------- MinIO


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    base = Settings.from_env()
    bucket = f"walgit-test-{uuid.uuid4().hex[:12]}"
    cfg = Settings(
        endpoint_url=base.endpoint_url,
        access_key=base.access_key,
        secret_key=base.secret_key,
        region=base.region,
        bucket=bucket,
        cache_dir=tmp_path_factory.mktemp("cache"),
    )
    try:
        s3mod.ensure_bucket(cfg)
    except (EndpointConnectionError, ClientError) as exc:
        pytest.exit(
            f"cannot reach MinIO at {cfg.endpoint_url} ({exc}).\n"
            "Start it with:  make up",
            returncode=1,
        )
    yield cfg
    _empty_bucket(cfg)


def _empty_bucket(cfg: Settings) -> None:
    client = s3mod.client(cfg)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.bucket):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=cfg.bucket, Delete={"Objects": keys})
    client.delete_bucket(Bucket=cfg.bucket)


@pytest.fixture
def repo_id() -> str:
    return f"r{uuid.uuid4().hex[:10]}"


@pytest.fixture
def store(settings, repo_id) -> ContentStore:
    return ContentStore(settings, prefix=f"tests/{repo_id}/objects")


@pytest.fixture
def wal(settings, repo_id) -> WalLog:
    return WalLog(settings, repo_id)


# ----------------------------------------------------------------- git client


class Client:
    """A local working repo that can push straight into the WAL.

    Bypasses the HTTP server so the WAL and compaction exercises are testable
    on their own: it builds the same packfile receive-pack would have staged,
    and appends the same WAL entry the hook would have written.
    """

    def __init__(self, path: Path, wal: WalLog):
        self.path = path
        self.wal = wal
        self.path.mkdir(parents=True, exist_ok=True)
        run_git("init", "--quiet", "-b", "main", ".", cwd=self.path)
        self.pushed: dict[str, str] = {}

    def commit(self, message: str, content: str | None = None) -> str:
        (self.path / "file.txt").write_text(content or message)
        run_git("add", "file.txt", cwd=self.path)
        run_git("commit", "--quiet", "-m", message, cwd=self.path)
        return self.head()

    def head(self, ref: str = "HEAD") -> str:
        return run_git("rev-parse", ref, cwd=self.path).stdout.decode().strip()

    def make_pack(self, new_oid: str, old_oid: str | None) -> bytes:
        args = ["rev-list", "--objects", new_oid]
        if old_oid:
            args += ["--not", old_oid]
        listing = run_git(*args, cwd=self.path).stdout.decode()
        names = "".join(line.split(" ")[0] + "\n" for line in listing.splitlines() if line)
        proc = subprocess.run(
            ["git", "pack-objects", "--stdout", "-q"],
            cwd=str(self.path),
            input=names.encode(),
            capture_output=True,
            env=GIT_ENV,
        )
        assert proc.returncode == 0, proc.stderr.decode()
        return proc.stdout

    def push(self, ref: str = "refs/heads/main", author: str = "test"):
        new_oid = self.head()
        old_oid = self.pushed.get(ref)
        pack = self.make_pack(new_oid, old_oid)
        digest = self.wal.objects.put(pack)
        entry = WalEntry.new_push(
            ref_updates=[RefUpdate(ref=ref, old=old_oid, new=new_oid)],
            pack=digest,
            pack_size=len(pack),
            author=author,
        )
        pointer, index, _ = self.wal.append(entry)
        self.pushed[ref] = new_oid
        return pointer, index


@pytest.fixture
def client(tmp_path, wal) -> Client:
    return Client(tmp_path / "work", wal)


# ---------------------------------------------------------------- HTTP server


@pytest.fixture
def server(settings):
    """A walgit smart-HTTP server on an ephemeral port."""
    httpd = make_server(settings, host="127.0.0.1", port=0, quiet=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield type("ServerHandle", (), {"url": url, "httpd": httpd, "settings": settings})
    httpd.shutdown()
    httpd.server_close()
