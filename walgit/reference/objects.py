"""Exercise 1 -- the content-addressed object store.

NAMING, because the two collide constantly in this problem space: in this
codebase **CAS always means compare-and-swap** (Exercise 2, the atomic
conditional PUT on the WAL index). The store below is content-*addressed* and
is never abbreviated to CAS -- it is "the object store".

Every immutable byte-blob walgit produces (packfiles, mostly) lives here, keyed
by the hash of its own content. That single property buys a lot:

* writes are idempotent -- re-uploading the same pack is a no-op;
* there is no cache invalidation, because a key's content can never change;
* dedup is free, and so is integrity checking on read;
* and crucially: a *failed* upload can never corrupt anything, so we can upload
  first and only afterwards make the data reachable from the WAL index.

That last point is the whole durability story. `put` is safe to call
speculatively; only the compare-and-swap in Exercise 2 makes data visible.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Iterator

from botocore.exceptions import ClientError

from . import s3 as s3mod
from .config import Settings
from .errors import CorruptObject, ObjectNotFound

# A digest is the string "sha256:<64 hex chars>".
Digest = str

ALGO = "sha256"
CHUNK = 1024 * 1024
MULTIPART_THRESHOLD = 8 * 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    digest: Digest
    key: str
    size: int
    last_modified: float


class ContentStore:
    """Content-addressed blob storage on top of an S3 bucket prefix."""

    def __init__(self, settings: Settings, prefix: str):
        self.settings = settings
        self.bucket = settings.bucket
        self.prefix = prefix.rstrip("/")
        self.s3 = s3mod.client(settings)
        # Counters the tests use to prove dedup actually avoids uploads.
        self.stats: Counter = Counter()

    # ---------------------------------------------------------------- hashing

    @staticmethod
    def digest_bytes(data: bytes) -> Digest:
        """Return the digest of ``data`` as ``"sha256:<hex>"``.

        Exactly that shape: the literal algorithm name, a colon, then 64 lowercase
        hex characters. Every other method here parses that shape -- ``key_for``
        rejects anything else -- and it is what makes the store algorithm-agile:
        the digest says how to check itself.
        """
        return f"{ALGO}:{hashlib.sha256(data).hexdigest()}"

    @staticmethod
    def digest_file(path: Path) -> Digest:
        """Digest a file without loading it into memory.

        Read it in ``CHUNK``-sized blocks. The result must be byte-for-byte the
        same string ``digest_bytes`` would return for the same content: the
        multipart path in 1.9 hashes the file this way and then expects the small
        path's digests to interoperate.
        """
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                h.update(block)
        return f"{ALGO}:{h.hexdigest()}"

    def key_for(self, digest: Digest) -> str:
        """Map a digest to its S3 key.

        Two levels of hex fan-out, taken from the digest itself::

            <prefix>/sha256/ab/cd/abcd...ef

        The nesting is not cosmetic. S3 partitions its keyspace by key prefix, so
        a flat ``<prefix>/<hash>`` layout concentrates every request for the
        repository on one partition and caps your request rate there; spreading
        over 256x256 prefixes lets S3 split them as traffic grows. It also keeps
        a debugging ``aws s3 ls`` to a few hundred entries per level instead of
        one listing with a million siblings in it.

        Validate the input and raise ``ValueError`` on anything that is not this
        store's digest format -- wrong algorithm, wrong length, no colon. This
        function is the single place where a digest becomes a key, which makes it
        the only place that can stop a malformed one from turning into a
        plausible-looking key that would then quietly read or write the wrong
        object. Cheap check, and the alternative is a corruption bug with no
        stack trace.
        """
        algo, _, hexdigest = digest.partition(":")
        if algo != ALGO or len(hexdigest) != 64:
            raise ValueError(f"not a {ALGO} digest: {digest!r}")
        return f"{self.prefix}/{algo}/{hexdigest[:2]}/{hexdigest[2:4]}/{hexdigest}"

    # ---------------------------------------------------------------- reading

    def exists(self, digest: Digest) -> bool:
        """Return whether an object with this digest is already stored.

        A HEAD, not a GET -- we want the metadata, never the body; on a 2 GiB
        packfile the difference is the whole point.

        "Not there" is an answer, not a failure: a 404 must come back as ``False``
        (``s3.is_not_found`` recognizes it). Any *other* ``ClientError`` -- no
        credentials, no bucket, a 500 -- is a real problem and must propagate
        rather than be flattened into ``False``, or a transient outage would look
        to ``put_file`` like an empty store and to the GC like garbage.

        Count the round-trip in ``self.stats["head"]``; the tests read it.
        """
        self.stats["head"] += 1
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.key_for(digest))
            return True
        except ClientError as exc:
            if s3mod.is_not_found(exc):
                return False
            raise

    def missing(self, digests: Iterable[Digest], workers: int = 16) -> set[Digest]:
        """Return the subset of ``digests`` not already in the store.

        Issued concurrently: a push may reference hundreds of packs and a
        serial round-trip per digest would dominate push latency.
        """
        unique = list(dict.fromkeys(digests))
        if not unique:
            return set()
        with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as pool:
            present = list(pool.map(self.exists, unique))
        return {d for d, ok in zip(unique, present) if not ok}

    def get(self, digest: Digest, verify: bool = True) -> bytes:
        """Fetch an object's bytes.

        With ``verify=True`` (the default), re-hash what came back and raise
        ``CorruptObject`` if it does not match the digest that was asked for.
        That check is the entire payoff of content addressing: a bit-flip in S3,
        a truncated body, or the wrong object under the right key all surface
        here instead of inside `git index-pack` ten minutes later.
        ``verify=False`` returns the bytes exactly as stored, which is only for
        callers that have already verified (and for the test that tampers with a
        key on purpose).

        A missing object must raise ``ObjectNotFound``, not leak a raw
        ``ClientError``: callers up the stack branch on walgit's own errors.

        Count the round-trip in ``self.stats["get"]``.
        """
        self.stats["get"] += 1
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self.key_for(digest))
        except ClientError as exc:
            if s3mod.is_not_found(exc):
                raise ObjectNotFound(digest) from exc
            raise
        data = resp["Body"].read()
        if verify:
            actual = self.digest_bytes(data)
            if actual != digest:
                raise CorruptObject(f"asked for {digest}, bytes hash to {actual}")
        return data

    def download(self, digest: Digest, dest: Path) -> Path:
        """Stream an object to ``dest``, verifying the hash as bytes arrive.

        Packfiles are the one thing here that will not fit in memory, so read the
        body in chunks (``resp["Body"].iter_chunks(CHUNK)``) and feed the hash as
        you write.

        The ordering rule is the interesting part: write to a temporary sibling
        path, verify the digest, and only then rename onto ``dest``. Rename is
        atomic, so a concurrent reader -- or this node after a crash -- sees
        either a complete, verified file or no file at all, never a half-written
        pack that git would happily try to open. On a hash mismatch or a missing
        object, raise and leave nothing behind.

        Create ``dest``'s parent if needed, and return ``dest``.
        """
        self.stats["download"] += 1
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        h = hashlib.sha256()
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self.key_for(digest))
        except ClientError as exc:
            if s3mod.is_not_found(exc):
                raise ObjectNotFound(digest) from exc
            raise
        with tmp.open("wb") as fh:
            for block in resp["Body"].iter_chunks(CHUNK):
                h.update(block)
                fh.write(block)
        actual = f"{ALGO}:{h.hexdigest()}"
        if actual != digest:
            tmp.unlink(missing_ok=True)
            raise CorruptObject(f"asked for {digest}, bytes hash to {actual}")
        # Rename last: a reader either sees a complete, verified file or nothing.
        tmp.replace(dest)
        return dest

    # ---------------------------------------------------------------- writing

    def put(self, data: bytes) -> Digest:
        """Store ``data``, returning its digest. Idempotent.

        Uses ``If-None-Match: *`` (create-if-absent) so a duplicate upload is
        rejected by S3 with a 412 instead of rewriting bytes that are, by
        construction, already identical. For a content-addressed store this is
        purely an optimization -- overwriting would be harmless -- but it saves a
        round-trip's worth of bandwidth and makes dedup observable.
        """
        digest = self.digest_bytes(data)
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=self.key_for(digest), Body=data, IfNoneMatch="*"
            )
            self.stats["put"] += 1
        except ClientError as exc:
            if s3mod.is_precondition_failed(exc):
                self.stats["put_deduped"] += 1
            else:
                raise
        return digest

    def put_file(self, path: Path) -> Digest:
        """Store a file's contents, returning its digest.

        Large packfiles go up as a multipart upload; the conditional-write
        trick above does not apply to multipart, so we dedup with an explicit
        existence check instead.
        """
        path = Path(path)
        size = path.stat().st_size
        if size < MULTIPART_THRESHOLD:
            return self.put(path.read_bytes())

        digest = self.digest_file(path)
        if self.exists(digest):
            self.stats["put_deduped"] += 1
            return digest
        with path.open("rb") as fh:
            self.s3.upload_fileobj(fh, self.bucket, self.key_for(digest))
        self.stats["put_multipart"] += 1
        return digest

    def put_many(self, blobs: Iterable[bytes], workers: int = 16) -> list[Digest]:
        blobs = list(blobs)
        if not blobs:
            return []
        with ThreadPoolExecutor(max_workers=min(workers, len(blobs))) as pool:
            return list(pool.map(self.put, blobs))

    # ------------------------------------------------------- listing / delete

    def iter_all(self) -> Iterator[StoredObject]:
        """Enumerate every object in the store. Used by the GC sweep."""
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for item in page.get("Contents", []):
                hexdigest = item["Key"].rsplit("/", 1)[-1]
                if len(hexdigest) != 64:
                    continue
                yield StoredObject(
                    digest=f"{ALGO}:{hexdigest}",
                    key=item["Key"],
                    size=item["Size"],
                    last_modified=item["LastModified"].timestamp(),
                )

    def delete(self, digest: Digest) -> None:
        self.stats["delete"] += 1
        self.s3.delete_object(Bucket=self.bucket, Key=self.key_for(digest))


def open_file(path: Path) -> IO[bytes]:  # pragma: no cover - trivial helper
    return os.fdopen(os.open(path, os.O_RDONLY), "rb")
