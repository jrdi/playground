"""Exercise 1 -- the content-addressed object store (walgit/objects.py)."""

from __future__ import annotations

import hashlib
import os

import pytest

from walgit.errors import CorruptObject, ObjectNotFound
from walgit.objects import ContentStore


def test_digest_is_prefixed_sha256(store):
    assert ContentStore.digest_bytes(b"hello") == (
        "sha256:" + hashlib.sha256(b"hello").hexdigest()
    )
    assert ContentStore.digest_bytes(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_digest_file_matches_digest_bytes(store, tmp_path):
    blob = os.urandom(3_000_000)
    path = tmp_path / "blob.bin"
    path.write_bytes(blob)
    assert ContentStore.digest_file(path) == ContentStore.digest_bytes(blob)


def test_key_layout_fans_out(store):
    digest = ContentStore.digest_bytes(b"hello")
    hexdigest = digest.split(":")[1]
    key = store.key_for(digest)
    assert key.endswith(f"sha256/{hexdigest[:2]}/{hexdigest[2:4]}/{hexdigest}")
    assert key.startswith(store.prefix + "/")


def test_key_rejects_nonsense(store):
    with pytest.raises(ValueError):
        store.key_for("md5:abc")
    with pytest.raises(ValueError):
        store.key_for("sha256:tooshort")


def test_put_get_roundtrip(store):
    digest = store.put(b"packfile bytes")
    assert store.get(digest) == b"packfile bytes"
    assert store.exists(digest)


def test_put_is_idempotent_and_dedups(store):
    """Re-uploading identical content must not re-upload the bytes."""
    first = store.put(b"same content")
    second = store.put(b"same content")
    assert first == second
    assert store.stats["put"] == 1
    assert store.stats["put_deduped"] == 1


def test_get_missing_raises(store):
    with pytest.raises(ObjectNotFound):
        store.get(ContentStore.digest_bytes(b"never stored"))


def test_exists_is_false_for_missing(store):
    assert not store.exists(ContentStore.digest_bytes(b"absent"))


def test_get_detects_corruption(store):
    """Content addressing is only worth anything if reads are verified."""
    digest = store.put(b"honest bytes")
    store.s3.put_object(
        Bucket=store.bucket, Key=store.key_for(digest), Body=b"tampered bytes"
    )
    with pytest.raises(CorruptObject):
        store.get(digest)
    assert store.get(digest, verify=False) == b"tampered bytes"


def test_missing_reports_only_absent_digests(store):
    present = [store.put(f"blob-{i}".encode()) for i in range(5)]
    absent = [ContentStore.digest_bytes(f"nope-{i}".encode()) for i in range(3)]
    assert store.missing(present + absent) == set(absent)
    assert store.missing([]) == set()


def test_download_streams_and_verifies(store, tmp_path):
    blob = os.urandom(200_000)
    digest = store.put(blob)
    dest = tmp_path / "out" / "pack.pack"
    store.download(digest, dest)
    assert dest.read_bytes() == blob
    assert not list(dest.parent.glob("*.part")), "partial file left behind"


def test_download_missing_leaves_no_partial(store, tmp_path):
    dest = tmp_path / "pack.pack"
    with pytest.raises(ObjectNotFound):
        store.download(ContentStore.digest_bytes(b"absent"), dest)
    assert not dest.exists()


def test_download_rejects_corrupt_bytes(store, tmp_path):
    digest = store.put(b"honest")
    store.s3.put_object(Bucket=store.bucket, Key=store.key_for(digest), Body=b"lies!!")
    dest = tmp_path / "pack.pack"
    with pytest.raises(CorruptObject):
        store.download(digest, dest)
    assert not dest.exists(), "a failed download must not leave a usable file"


@pytest.mark.slow
def test_put_file_handles_multipart_sized_packs(store, tmp_path):
    """Real packfiles blow past the single-PUT comfort zone."""
    blob = os.urandom(9 * 1024 * 1024)
    path = tmp_path / "big.pack"
    path.write_bytes(blob)

    digest = store.put_file(path)
    assert digest == ContentStore.digest_bytes(blob)
    assert store.get(digest) == blob
    assert store.stats["put_multipart"] == 1

    assert store.put_file(path) == digest
    assert store.stats["put_deduped"] >= 1


def test_put_file_small_uses_conditional_put(store, tmp_path):
    path = tmp_path / "small.pack"
    path.write_bytes(b"tiny pack")
    digest = store.put_file(path)
    assert store.get(digest) == b"tiny pack"
    assert store.stats["put"] == 1


def test_iter_all_enumerates_the_store(store):
    digests = {store.put(f"obj-{i}".encode()) for i in range(4)}
    assert {obj.digest for obj in store.iter_all()} == digests
    assert all(obj.size > 0 for obj in store.iter_all())


def test_delete_removes(store):
    digest = store.put(b"transient")
    store.delete(digest)
    assert not store.exists(digest)
