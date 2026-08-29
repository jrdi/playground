"""Exercise 2 -- the WAL and its compare-and-swap index (walgit/wal.py).

The tests that matter here are the concurrent ones. Everything else is
bookkeeping; those two are the reason the design works at all.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from walgit.errors import CasConflict, RefConflict
from walgit.wal import Base, RefUpdate, WalEntry, WalIndex, WalLog

MAIN = "refs/heads/main"
OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40


def push_entry(ref=MAIN, old=None, new=OID_A, pack=None, size=0) -> WalEntry:
    return WalEntry.new_push(
        ref_updates=[RefUpdate(ref=ref, old=old, new=new)], pack=pack, pack_size=size
    )


def push_all(index: WalIndex, *entries: WalEntry) -> tuple[WalIndex, dict[str, WalEntry]]:
    """Apply pushes to ``index``, also returning the entry objects ``refs_at`` needs."""
    by_id = {}
    for entry in entries:
        index = index.apply_push(entry, key=f"wal/{entry.entry_id}.json")
        by_id[entry.entry_id] = entry
    return index, by_id


# ------------------------------------------------------------------ index math


def test_apply_push_assigns_sequence_and_refs():
    index = WalIndex(repo_id="r")
    after = index.apply_push(push_entry(), key="k1")

    assert after.entries[0].seq == 1
    assert after.next_seq == 2
    assert after.refs == {MAIN: OID_A}
    assert after.version == index.version + 1
    assert index.entries == [], "apply_push must not mutate the index it was given"


def test_apply_push_rejects_stale_old_value():
    index = WalIndex(repo_id="r").apply_push(push_entry(), key="k1")
    with pytest.raises(RefConflict) as excinfo:
        index.apply_push(push_entry(old=OID_C, new=OID_B), key="k2")
    assert excinfo.value.ref == MAIN
    assert excinfo.value.actual == OID_A


def test_apply_push_rejects_create_over_existing_ref():
    index = WalIndex(repo_id="r").apply_push(push_entry(), key="k1")
    with pytest.raises(RefConflict):
        index.apply_push(push_entry(old=None, new=OID_B), key="k2")


def test_apply_push_handles_deletes():
    index = WalIndex(repo_id="r").apply_push(push_entry(), key="k1")
    after = index.apply_push(push_entry(old=OID_A, new=None), key="k2")
    assert MAIN not in after.refs
    assert after.head_seq == 2


def test_apply_push_is_idempotent_on_entry_id():
    entry = push_entry()
    index = WalIndex(repo_id="r").apply_push(entry, key="k1")
    again = index.apply_push(entry, key="k1")
    assert len(again.entries) == 1


def test_refs_at_replays_history():
    index, entries = push_all(
        WalIndex(repo_id="r"),
        push_entry(new=OID_A),
        push_entry(old=OID_A, new=OID_B),
        push_entry(ref="refs/heads/side", new=OID_C),
    )

    assert index.refs_at(1, entries) == {MAIN: OID_A}
    assert index.refs_at(2, entries) == {MAIN: OID_B}
    assert index.refs_at(3, entries) == {MAIN: OID_B, "refs/heads/side": OID_C}
    with pytest.raises(KeyError):
        index.refs_at(3, {})  # the index alone cannot answer: history lives in wal/


def test_refs_at_neither_mutates_nor_aliases_the_index():
    """refs_at is a query. Starting the replay from ``base.refs`` itself rather
    than a copy overwrites the base's snapshot on the first call -- which still
    returns the right answer -- and every later call then replays history on
    top of a snapshot that is already wrong. Ask in descending order to see it.
    """
    base = Base(pack="sha256:" + "0" * 64, pack_size=1, through_seq=0, refs={MAIN: OID_A})
    index = WalIndex(repo_id="r", next_seq=1, base=base, refs={MAIN: OID_A})
    index, entries = push_all(
        index, push_entry(old=OID_A, new=OID_B), push_entry(old=OID_B, new=OID_C)
    )
    before = index.to_json()

    assert index.refs_at(2, entries) == {MAIN: OID_C}
    assert index.refs_at(1, entries) == {MAIN: OID_B}, "an earlier query saw a later one's replay"
    assert index.refs_at(0, entries) == {MAIN: OID_A}, "the base snapshot was overwritten"
    assert index.to_json() == before, "refs_at modified the index"

    leaked = index.refs_at(2, entries)
    leaked["refs/heads/leaked"] = OID_A
    assert index.to_json() == before, "refs_at returned a dict the index still holds"


def test_pointer_size_does_not_depend_on_the_push():
    """The rule the index is built on: nothing in it grows with a push. S3 rewrites
    the whole object on every compare-and-swap, so anything a pointer carried
    would be paid for again by every later push -- one 5,000-tag push would tax
    every push after it until the next compaction."""
    tags = [RefUpdate(ref=f"refs/tags/v{i}", old=None, new=OID_A) for i in range(5000)]

    def pointer_json(entry: WalEntry) -> dict:
        return WalIndex(repo_id="r").apply_push(entry, key="wal/k.json").to_json()["entries"][0]

    small = pointer_json(push_entry())
    huge = pointer_json(WalEntry.new_push(ref_updates=tags, pack=None))

    assert "ref_updates" not in small, "ref updates belong in the entry object"
    assert len(json.dumps(small)) == len(json.dumps(huge))


def test_index_json_roundtrip():
    index = WalIndex(repo_id="r").apply_push(push_entry(pack="sha256:" + "0" * 64), "k1")
    assert WalIndex.from_json(json.loads(json.dumps(index.to_json()))).to_json() == index.to_json()


# --------------------------------------------------------------- S3 round trip


def test_load_of_unknown_repo_is_an_empty_log(wal):
    index, etag = wal.load()
    assert etag is None
    assert index.entries == [] and index.refs == {} and index.next_seq == 1


def test_append_persists_entry_then_publishes_it(wal):
    entry = push_entry()
    pointer, index, etag = wal.append(entry)

    assert pointer.seq == 1
    assert etag is not None

    stored = wal.read_entry(pointer.key)
    assert stored.entry_id == entry.entry_id
    assert stored.ref_updates == entry.ref_updates

    reloaded, reloaded_etag = wal.load()
    assert reloaded_etag == etag
    assert reloaded.refs == {MAIN: OID_A}


def test_append_rejects_stale_push(wal):
    wal.append(push_entry())
    with pytest.raises(RefConflict):
        wal.append(push_entry(old=OID_C, new=OID_B))


def test_commit_with_stale_etag_raises_cas_conflict(wal):
    index, etag = wal.load()
    first = index.apply_push(push_entry(), key="k1")
    new_etag = wal.commit(first, etag)

    second = first.apply_push(push_entry(old=OID_A, new=OID_B), key="k2")
    with pytest.raises(CasConflict):
        wal.commit(second, etag)  # etag is now two versions behind

    assert wal.commit(second, new_etag)


def test_commit_creating_an_index_twice_raises(wal):
    index, etag = wal.load()
    assert etag is None
    wal.commit(index.apply_push(push_entry(), "k1"), None)
    with pytest.raises(CasConflict):
        wal.commit(index.apply_push(push_entry(), "k2"), None)


def test_load_if_changed_returns_none_when_current(wal):
    _, _, etag = wal.append(push_entry())
    assert wal.load_if_changed(etag) is None

    wal.append(push_entry(old=OID_A, new=OID_B))
    changed = wal.load_if_changed(etag)
    assert changed is not None
    index, new_etag = changed
    assert new_etag != etag
    assert index.refs == {MAIN: OID_B}


# ------------------------------------------------------------- durability rule


def test_a_failed_swap_leaves_the_push_invisible(wal, monkeypatch):
    """Durable before visible: a crash after writing the entry object must not
    produce a half-applied push. The orphan is unreachable, and the GC's grace
    period is what eventually reclaims it."""
    entry = push_entry()

    def explode(*args, **kwargs):
        raise RuntimeError("node died mid-commit")

    monkeypatch.setattr(wal, "commit", explode)
    with pytest.raises(RuntimeError):
        wal.append(entry)

    index, etag = wal.load()
    assert etag is None, "the index must not exist yet"
    assert index.entries == []

    # ...but the entry object itself is durable, and orphaned.
    monkeypatch.undo()
    orphan = wal.read_entry(wal.entry_key(entry.entry_id))
    assert orphan.entry_id == entry.entry_id


# ----------------------------------------------------------------- concurrency


def test_concurrent_pushes_to_distinct_refs_all_land(settings, repo_id):
    """Eight independent 'nodes' pushing at once. Every push must survive, and
    the sequence numbers must be a gapless 1..8 -- that is linearizability."""
    n = 8

    def push(i: int):
        node = WalLog(settings, repo_id)  # a separate node, separate client
        entry = WalEntry.new_push(
            ref_updates=[RefUpdate(ref=f"refs/heads/b{i}", old=None, new=f"{i:040d}")],
            pack=None,
        )
        return node.append(entry)[0].seq

    with ThreadPoolExecutor(max_workers=n) as pool:
        seqs = sorted(pool.map(push, range(n)))

    assert seqs == list(range(1, n + 1)), "sequence numbers must be unique and gapless"

    index, _ = WalLog(settings, repo_id).load()
    assert len(index.refs) == n
    assert len(index.entries) == n
    assert [e.seq for e in index.entries] == list(range(1, n + 1))


def test_concurrent_pushes_to_the_same_ref_pick_exactly_one_winner(settings, repo_id):
    """The lost-update test. git's local check cannot catch this -- every node
    validated against the same empty state -- so the WAL has to."""
    n = 6
    results = []

    def push(i: int):
        node = WalLog(settings, repo_id)
        entry = WalEntry.new_push(
            ref_updates=[RefUpdate(ref=MAIN, old=None, new=f"{i:040d}")], pack=None
        )
        try:
            return ("ok", node.append(entry)[0].seq)
        except RefConflict:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(push, range(n)))

    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"

    index, _ = WalLog(settings, repo_id).load()
    assert len(index.entries) == 1
    assert index.refs[MAIN] in {f"{i:040d}" for i in range(n)}


def test_append_retries_through_contention(settings, repo_id, monkeypatch):
    """A losing racer must reload and retry, not fail the push."""
    wal = WalLog(settings, repo_id)
    other = WalLog(settings, repo_id)
    original_load = wal.load
    injected = {"done": False}

    def load_then_steal():
        index, etag = original_load()
        if not injected["done"]:
            injected["done"] = True
            # Somebody else commits between our read and our swap.
            other_index, other_etag = other.load()
            other.commit(
                other_index.apply_push(
                    push_entry(ref="refs/heads/other", new=OID_C), "k-other"
                ),
                other_etag,
            )
        return index, etag

    monkeypatch.setattr(wal, "load", load_then_steal)
    pointer, index, _ = wal.append(push_entry())

    assert wal.stats["cas_conflicts"] == 1
    assert pointer.seq == 2, "our push landed after the racer's"
    assert index.refs == {MAIN: OID_A, "refs/heads/other": OID_C}
