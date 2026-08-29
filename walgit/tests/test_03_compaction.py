"""Exercise 3 -- compaction and GC (walgit/compact.py)."""

from __future__ import annotations

import pytest
from conftest import run_git

from walgit import compact as compact_mod
from walgit.compact import (
    CompactionPolicy,
    apply_compaction,
    compact,
    gc,
    plan_compaction,
    should_compact,
    unreferenced_packs,
)
from walgit.errors import CompactionRaced
from walgit.repo import materialize_snapshot
from walgit.wal import RefUpdate, WalEntry, WalIndex, WalLog

MAIN = "refs/heads/main"


def fake_index(n: int, size: int = 1000) -> tuple[WalIndex, dict[str, WalEntry]]:
    """``n`` pushes moving main, plus the entry objects behind them.

    The index alone cannot say what a push did -- pointers carry no ref updates --
    so anything that replays history needs the entries too.
    """
    index, entries = WalIndex(repo_id="r"), {}
    previous = None
    for i in range(n):
        oid = f"{i:040d}"
        entry = WalEntry.new_push(
            ref_updates=[RefUpdate(ref=MAIN, old=previous, new=oid)],
            pack=f"sha256:{i:064d}",
            pack_size=size,
        )
        index = index.apply_push(entry, key=f"k{i}")
        entries[entry.entry_id] = entry
        previous = oid
    return index, entries


# ---------------------------------------------------------------------- policy


def test_should_compact_on_entry_count():
    policy = CompactionPolicy(min_entries=4, max_incremental_bytes=10**9)
    assert not should_compact(fake_index(3)[0], policy)
    assert should_compact(fake_index(4)[0], policy)


def test_should_compact_on_accumulated_bytes():
    policy = CompactionPolicy(min_entries=1000, max_incremental_bytes=2500)
    assert not should_compact(fake_index(2, size=1000)[0], policy)
    assert should_compact(fake_index(3, size=1000)[0], policy)


def test_should_not_compact_an_empty_log():
    assert not should_compact(WalIndex(repo_id="r"), CompactionPolicy(min_entries=1))


def test_keep_recent_leaves_a_tail_uncompacted():
    policy = CompactionPolicy(min_entries=2, keep_recent=2)
    index, entries = fake_index(5)
    plan = plan_compaction(index, policy, entries)
    assert plan.through_seq == 3
    assert plan.entry_seqs == (1, 2, 3)


def test_plan_snapshots_refs_at_the_cut_point():
    """The new base must carry the ref state *as of* through_seq, not 'now'."""
    index, entries = fake_index(5)
    plan = plan_compaction(index, CompactionPolicy(min_entries=2, keep_recent=2), entries)
    assert plan.refs == {MAIN: f"{2:040d}"}
    assert plan.refs != index.refs


def test_plan_is_none_below_threshold():
    index, entries = fake_index(2)
    assert plan_compaction(index, CompactionPolicy(min_entries=8), entries) is None


# ----------------------------------------------------------------- the splice


def test_apply_compaction_replaces_prefix_and_keeps_tail():
    index, entries = fake_index(3)
    plan = plan_compaction(index, CompactionPolicy(min_entries=2, keep_recent=1), entries)

    # A push lands while we were repacking.
    index = index.apply_push(
        WalEntry.new_push(
            ref_updates=[RefUpdate(ref="refs/heads/late", old=None, new="f" * 40)],
            pack="sha256:" + "f" * 64,
            pack_size=10,
        ),
        key="k-late",
    )

    after = apply_compaction(index, plan, "sha256:" + "a" * 64, 500)

    assert after.base.through_seq == 2
    assert after.base.refs == plan.refs
    assert [e.seq for e in after.entries] == [3, 4], "the tail must survive"
    assert after.refs == index.refs, "compaction never changes what the refs are"
    assert after.next_seq == index.next_seq
    assert after.live_packs()[0] == "sha256:" + "a" * 64


def test_apply_compaction_refuses_when_already_compacted():
    index, entries = fake_index(4)
    plan = plan_compaction(index, CompactionPolicy(min_entries=2), entries)
    once = apply_compaction(index, plan, "sha256:" + "a" * 64, 500)
    with pytest.raises(CompactionRaced):
        apply_compaction(once, plan, "sha256:" + "b" * 64, 500)


def test_apply_compaction_refuses_when_the_prefix_moved():
    index, entries = fake_index(4)
    plan = plan_compaction(index, CompactionPolicy(min_entries=2), entries)
    mutated = index.copy()
    mutated.entries = mutated.entries[1:]  # somebody trimmed the log underneath us
    with pytest.raises(CompactionRaced):
        apply_compaction(mutated, plan, "sha256:" + "a" * 64, 500)


def test_refs_at_still_works_after_compaction():
    """The base's ref snapshot is what keeps history queryable post-compaction."""
    index, entries = fake_index(4)
    plan = plan_compaction(index, CompactionPolicy(min_entries=2, keep_recent=1), entries)
    after = apply_compaction(index, plan, "sha256:" + "a" * 64, 500)
    assert after.refs_at(after.base.through_seq, entries) == plan.refs
    assert after.refs_at(4, entries) == index.refs


# ------------------------------------------------------- against real packfiles


@pytest.fixture
def pushed_repo(client, wal):
    """Five real pushes of real git objects into the WAL."""
    oids = []
    for i in range(5):
        oids.append(client.commit(f"commit {i}"))
        client.push()
    return oids


def test_compaction_collapses_the_log(settings, repo_id, wal, pushed_repo):
    before, _ = wal.load()
    assert len(before.entries) == 5
    assert before.base is None

    result = compact(settings, repo_id, CompactionPolicy(min_entries=3))
    assert result is not None

    after, _ = wal.load()
    assert after.base is not None
    assert after.base.through_seq == 5
    assert after.entries == []
    assert after.refs == before.refs, "compaction must not move any ref"
    assert len(after.live_packs()) == 1, "one pack instead of five"


def test_compaction_preserves_every_commit(settings, repo_id, wal, pushed_repo, tmp_path):
    compact(settings, repo_id, CompactionPolicy(min_entries=3))
    index, _ = wal.load()

    rebuilt = materialize_snapshot(
        settings, index, index.head_seq, tmp_path / "check.git", refs=index.refs
    )
    run_git("fsck", "--no-progress", cwd=rebuilt)
    for oid in pushed_repo:
        run_git("cat-file", "-e", oid, cwd=rebuilt)
    assert run_git("rev-parse", MAIN, cwd=rebuilt).stdout.decode().strip() == pushed_repo[-1]


def test_compaction_is_a_noop_below_threshold(settings, repo_id, client):
    client.commit("only one")
    client.push()
    assert compact(settings, repo_id, CompactionPolicy(min_entries=8)) is None


def test_pushes_during_the_repack_are_not_lost(settings, repo_id, wal, client, monkeypatch):
    """The repack runs against a pinned snapshot; whatever lands meanwhile must
    end up sitting on top of the new base, not dropped by the swap."""
    for i in range(4):
        client.commit(f"commit {i}")
        client.push()

    original = compact_mod._repack_snapshot
    sneaked = {}

    def repack_then_race(settings_, index_, plan_):
        out = original(settings_, index_, plan_)
        # A client pushes while we hold a finished pack but have not swapped.
        client.commit("landed mid-compaction")
        pointer, _ = client.push()
        sneaked["seq"] = pointer.seq
        sneaked["oid"] = client.head()
        return out

    monkeypatch.setattr(compact_mod, "_repack_snapshot", repack_then_race)
    result = compact(settings, repo_id, CompactionPolicy(min_entries=3))

    index, _ = wal.load()
    assert result.plan.through_seq == 4
    assert [e.seq for e in index.entries] == [sneaked["seq"]]
    assert index.refs[MAIN] == sneaked["oid"]


def test_second_compaction_builds_on_the_first(settings, repo_id, wal, client, pushed_repo):
    compact(settings, repo_id, CompactionPolicy(min_entries=3))
    first, _ = wal.load()

    for i in range(3):
        client.commit(f"later {i}")
        client.push()

    result = compact(settings, repo_id, CompactionPolicy(min_entries=3))
    second, _ = wal.load()

    assert result.plan.input_packs[0] == first.base.pack, "must fold in the old base"
    assert second.base.through_seq == 8
    assert second.entries == []
    assert second.refs[MAIN] == client.head()



def test_compaction_with_keep_recent_replays_entry_objects(
    settings, repo_id, wal, client, tmp_path
):
    """The one compaction path that needs ref *history*. Cutting at seq 3 of 5 means
    the new base must record main as it was after push 3 -- which the index
    cannot say, because pointers carry no ref updates. It has to come from wal/."""
    oids = []
    for i in range(5):
        oids.append(client.commit(f"commit {i}"))
        client.push()

    result = compact(settings, repo_id, CompactionPolicy(min_entries=2, keep_recent=2))
    index, _ = wal.load()

    assert result.plan.through_seq == 3
    assert index.base.refs == {MAIN: oids[2]}, "the base must snapshot the cut, not now"
    assert [e.seq for e in index.entries] == [4, 5], "the recent tail stays in the log"
    assert index.refs == {MAIN: oids[4]}

    rebuilt = materialize_snapshot(
        settings, index, index.base.through_seq, tmp_path / "cut.git", refs=index.base.refs
    )
    run_git("fsck", "--no-progress", cwd=rebuilt)
    assert run_git("rev-parse", MAIN, cwd=rebuilt).stdout.decode().strip() == oids[2]


# ------------------------------------------------------------------------- gc


def test_gc_reclaims_superseded_packs(settings, repo_id, wal, pushed_repo, tmp_path):
    before, _ = wal.load()
    old_packs = set(before.live_packs())

    compact(settings, repo_id, CompactionPolicy(min_entries=3))
    index, _ = wal.load()

    assert set(unreferenced_packs(settings, repo_id, grace_seconds=0)) == old_packs

    result = gc(settings, repo_id, grace_seconds=0)
    assert set(result.deleted_packs) == old_packs
    assert result.deleted_entries, "compacted entry objects are garbage too"

    # The repository is still fully serveable from what is left.
    assert wal.objects.exists(index.base.pack)
    rebuilt = materialize_snapshot(
        settings, index, index.head_seq, tmp_path / "after-gc.git", refs=index.refs
    )
    run_git("fsck", "--no-progress", cwd=rebuilt)
    assert run_git("rev-parse", MAIN, cwd=rebuilt).stdout.decode().strip() == pushed_repo[-1]


def test_gc_never_touches_live_packs(settings, repo_id, wal, pushed_repo):
    index, _ = wal.load()
    result = gc(settings, repo_id, grace_seconds=0)
    assert result.deleted_packs == []
    for pack in index.live_packs():
        assert wal.objects.exists(pack)


def test_grace_period_protects_an_in_flight_push(settings, repo_id, wal, pushed_repo):
    """A pack that has been uploaded but whose swap has not happened yet is
    indistinguishable from garbage. Only its age tells them apart."""
    orphan = wal.objects.put(b"a pack belonging to a push still in progress")
    assert orphan not in wal.load()[0].live_packs()

    kept = gc(settings, repo_id, grace_seconds=3600)
    assert kept.deleted_packs == []
    assert orphan in kept.retained_by_grace
    assert wal.objects.exists(orphan)

    swept = gc(settings, repo_id, grace_seconds=0)
    assert orphan in swept.deleted_packs
