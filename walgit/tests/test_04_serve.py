"""Exercise 4 -- serving real git (walgit/repo.py, walgit/hooks.py).

Everything here drives the actual `git` binary over HTTP. If these pass, you
have a git server whose only durable state is objects in S3.
"""

from __future__ import annotations

import sys

import pytest
from conftest import GIT_ENV, run_git

from walgit import gitcmd
from walgit.compact import CompactionPolicy, compact
from walgit.repo import LocalRepo
from walgit.wal import RefUpdate, WalEntry, WalLog

MAIN = "refs/heads/main"
pytestmark = pytest.mark.slow


@pytest.fixture
def work(tmp_path):
    """A perfectly ordinary git working repo. It knows nothing about walgit."""
    path = tmp_path / "work"
    path.mkdir()
    run_git("init", "--quiet", "-b", "main", ".", cwd=path)
    return path


def commit(path, message: str) -> str:
    (path / "file.txt").write_text(message)
    run_git("add", "file.txt", cwd=path)
    run_git("commit", "--quiet", "-m", message, cwd=path)
    return run_git("rev-parse", "HEAD", cwd=path).stdout.decode().strip()


def test_push_creates_the_repository_and_a_wal_entry(server, work, repo_id, wal):
    oid = commit(work, "first")
    proc = run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)
    assert b"walgit: seq=1" in proc.stderr, proc.stderr.decode()

    index, _ = wal.load()
    assert index.refs == {MAIN: oid}
    assert len(index.entries) == 1
    assert index.entries[0].pack is not None
    assert wal.objects.exists(index.entries[0].pack)


def test_clone_returns_what_was_pushed(server, work, repo_id, tmp_path):
    oid = commit(work, "hello walgit")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    dest = tmp_path / "clone"
    run_git("clone", "--quiet", f"{server.url}/{repo_id}", str(dest), cwd=tmp_path)
    assert run_git("rev-parse", "HEAD", cwd=dest).stdout.decode().strip() == oid
    assert (dest / "file.txt").read_text() == "hello walgit"


def test_incremental_pushes_append_to_the_log(server, work, repo_id, wal, tmp_path):
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)
    second = commit(work, "two")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    index, _ = wal.load()
    assert [e.seq for e in index.entries] == [1, 2]
    assert index.refs[MAIN] == second

    dest = tmp_path / "clone"
    run_git("clone", "--quiet", f"{server.url}/{repo_id}", str(dest), cwd=tmp_path)
    log = run_git("log", "--format=%s", cwd=dest).stdout.decode().splitlines()
    assert log == ["two", "one"]


def test_the_local_repo_is_only_a_cache(server, work, repo_id, settings, tmp_path):
    """Delete everything on disk. The repository still exists -- in S3."""
    oid = commit(work, "durable")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    local = LocalRepo(settings, repo_id)
    assert local.path.exists()
    local.destroy()
    assert not local.path.exists()

    dest = tmp_path / "clone-after-wipe"
    run_git("clone", "--quiet", f"{server.url}/{repo_id}", str(dest), cwd=tmp_path)
    assert run_git("rev-parse", "HEAD", cwd=dest).stdout.decode().strip() == oid


def test_sync_short_circuits_on_304(server, work, repo_id, settings):
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    local = LocalRepo(settings, repo_id)
    local.sync()
    assert local.sync().up_to_date_via_304, "an unchanged repo must cost one 304"


def test_a_branch_delete_is_a_wal_entry_too(server, work, repo_id, wal):
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)
    run_git("push", f"{server.url}/{repo_id}", "main:refs/heads/doomed", cwd=work)
    run_git("push", f"{server.url}/{repo_id}", "--delete", "doomed", cwd=work)

    index, _ = wal.load()
    assert "refs/heads/doomed" not in index.refs
    assert index.entries[-1].pack is None, "a delete carries no objects"
    deletion = wal.read_entry(index.entries[-1].key)  # what a push did lives in wal/
    assert deletion.ref_updates[0].new is None


def test_the_wal_rejects_a_push_a_stale_node_would_have_accepted(
    server, work, repo_id, settings, wal, monkeypatch
):
    """The reason the ref check is re-done at the WAL.

    We freeze this node's cache and let another node move `main` underneath it.
    git on this node happily accepts the push -- its local ref still matches
    what the client claimed -- and the compare-and-swap is the only thing left
    standing between that and a silently lost update.
    """
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    monkeypatch.setattr(LocalRepo, "sync", lambda self, force=False: None)

    other_node = WalLog(settings, repo_id)
    index, _ = other_node.load()
    other_node.append(
        WalEntry.new_push(
            ref_updates=[RefUpdate(ref=MAIN, old=index.refs[MAIN], new="d" * 40)],
            pack=None,
        )
    )

    commit(work, "two")
    proc = run_git("push", f"{server.url}/{repo_id}", "main", cwd=work, check=False)
    assert proc.returncode != 0, "this push must not be acknowledged"
    assert b"rejected" in proc.stderr

    after, _ = wal.load()
    assert after.refs[MAIN] == "d" * 40, "the other node's push must stand"


def test_a_sync_cannot_land_inside_a_push(server, work, repo_id, wal, monkeypatch, tmp_path):
    """git moves refs only *after* pre-receive has committed the push to the WAL.

    A sync landing in between moves this node's refs to the WAL's new values; git
    then finds every ref at the "wrong" old value, and the client is told a push
    failed that is in fact durable and live. We hand a real concurrent sync that
    exact window -- the hook starts one and gives it two seconds -- and the push
    must still be acknowledged, because a push holds the repository's lock.
    """
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    # Started through a launcher, never as a shell background job: a shell hands
    # its background children every open descriptor, including the write end of
    # http-backend's output pipe. Holding that open, the server never sees EOF,
    # never releases the lock -- and the sync waits on the lock forever.
    launcher = tmp_path / "concurrent_sync.py"
    launcher.write_text(
        "import os, subprocess, sys\n"
        "env = {k: v for k, v in os.environ.items() if k not in\n"
        "       ('GIT_QUARANTINE_PATH', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_DIR')}\n"
        "sync = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import sys; from walgit.config import Settings; '\n"
        "     'from walgit.repo import LocalRepo; LocalRepo(Settings.from_env(), sys.argv[1]).sync(force=True)',\n"
        "     sys.argv[1]],\n"
        "    env=env, cwd='/', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL, close_fds=True,\n"
        ")\n"
        "try:\n"
        "    sync.wait(timeout=2)\n"
        "except subprocess.TimeoutExpired:\n"
        "    pass  # still waiting for the repository lock, which is what the fix should cause\n"
    )
    original = LocalRepo.install_hooks

    def install_racing_hooks(self):
        original(self)
        hook = self.path / "walgit-hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            f'"{sys.executable}" -m walgit.hooks pre-receive "{self.repo_id}" || exit $?\n'
            f'"{sys.executable}" "{launcher}" "{self.repo_id}" </dev/null >/dev/null 2>&1\n'
            "exit 0\n"
        )
        hook.chmod(0o755)

    monkeypatch.setattr(LocalRepo, "install_hooks", install_racing_hooks)

    second = commit(work, "two")
    proc = run_git("push", f"{server.url}/{repo_id}", "main", cwd=work, check=False)
    assert proc.returncode == 0, f"a committed push was reported as failed:\n{proc.stderr.decode()}"

    index, _ = wal.load()
    assert index.refs[MAIN] == second, "the push is in the WAL"
    advertised = run_git("ls-remote", f"{server.url}/{repo_id}", MAIN, cwd=work).stdout.decode().split()
    assert advertised[0] == second, "and the node serves it"


def test_objects_that_arrive_loose_are_refused(server, work, repo_id, wal, monkeypatch):
    """With git's default unpackLimit, a small push arrives as loose objects and no
    pack. A hook that reads "no pack" as "no objects" would record the push in the
    WAL without uploading anything -- acknowledged, and unservable from S3."""
    original = gitcmd.init_bare

    def init_with_default_unpack_limit(path):
        original(path)
        for key in ("receive.unpackLimit", "transfer.unpackLimit"):
            gitcmd.git("config", "--unset", key, cwd=path)

    monkeypatch.setattr(gitcmd, "init_bare", init_with_default_unpack_limit)

    commit(work, "small enough to be unpacked")
    proc = run_git("push", f"{server.url}/{repo_id}", "main", cwd=work, check=False)
    assert proc.returncode != 0, "a push whose objects never reached S3 was acknowledged"
    assert b"loose" in proc.stderr

    index, etag = wal.load()
    assert etag is None and index.entries == [], "nothing may reach the WAL"


def test_clone_works_after_compaction(server, work, repo_id, settings, tmp_path):
    for i in range(4):
        commit(work, f"commit {i}")
        run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)
    head = run_git("rev-parse", "HEAD", cwd=work).stdout.decode().strip()

    result = compact(settings, repo_id, CompactionPolicy(min_entries=3))
    assert result is not None

    LocalRepo(settings, repo_id).destroy()
    dest = tmp_path / "clone-compacted"
    run_git("clone", "--quiet", f"{server.url}/{repo_id}", str(dest), cwd=tmp_path)
    assert run_git("rev-parse", "HEAD", cwd=dest).stdout.decode().strip() == head
    assert run_git("log", "--format=%s", cwd=dest).stdout.decode().splitlines() == [
        "commit 3",
        "commit 2",
        "commit 1",
        "commit 0",
    ]


def test_fetch_after_someone_elses_push(server, work, repo_id, tmp_path):
    commit(work, "one")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    dest = tmp_path / "clone"
    run_git("clone", "--quiet", f"{server.url}/{repo_id}", str(dest), cwd=tmp_path)

    second = commit(work, "two")
    run_git("push", f"{server.url}/{repo_id}", "main", cwd=work)

    run_git("pull", "--quiet", "--ff-only", cwd=dest, env_extra=GIT_ENV)
    assert run_git("rev-parse", "HEAD", cwd=dest).stdout.decode().strip() == second


def test_sync_prunes_the_packs_compaction_replaced(settings, repo_id, client):
    """After a compaction, a node's next sync must delete the packs the new base
    replaced -- from disk, not just from its bookkeeping. If the recorded names don't
    match the files git wrote, the unlink misses silently and every compaction
    leaves the old packs behind for good."""
    for i in range(3):
        client.commit(f"commit {i}")
        client.push()

    node = LocalRepo(settings, repo_id)
    node.sync()
    packs = gitcmd.pack_dir(node.path)
    recorded = dict(node.load_state().packs)
    files_before = {p.name for p in packs.glob("*.pack")}
    assert len(recorded) == 3 and len(files_before) == 3
    for name in recorded.values():
        assert (packs / f"{name}.pack").exists(), f"state records {name!r}, but git wrote no such file"

    compact(settings, repo_id, CompactionPolicy(min_entries=2))
    result = node.sync()

    assert set(result.pruned) == set(recorded)
    leftover = files_before & {p.name for p in packs.glob("*.pack")}
    assert not leftover, f"pruned from the state but still on disk: {sorted(leftover)}"
    assert len(list(packs.glob("*.pack"))) == 1, "only the new base pack remains"
    run_git("fsck", "--no-progress", cwd=node.path)
    assert run_git("rev-parse", "refs/heads/main", cwd=node.path).stdout.decode().strip() == client.head()
