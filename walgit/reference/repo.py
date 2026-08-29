"""Exercise 4a -- the warm local repository.

A node's on-disk repository is a *cache*, never the truth. It can be deleted at
any moment and rebuilt by replaying the WAL: base pack, then each incremental
pack, then set the refs to whatever the index says they are. `make demo` does
exactly that, on purpose, to prove the point.

Staying fresh costs one conditional GET. If S3 answers 304 the cache is already
current and the fetch can be served immediately with no further I/O -- which is
what makes reads cheap enough to scale out.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd
from .objects import Digest
from .config import Settings
from .wal import WalIndex, WalLog

STATE_FILE = "walgit-state.json"


@dataclass
class CacheState:
    """What this node believes about its local copy."""

    etag: str | None = None
    head_seq: int = 0
    base: Digest | None = None
    # object digest -> the pack name git gave it locally, so we can prune later.
    packs: dict[Digest, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "etag": self.etag,
            "head_seq": self.head_seq,
            "base": self.base,
            "packs": dict(self.packs),
        }

    @classmethod
    def from_json(cls, raw: dict) -> "CacheState":
        return cls(
            etag=raw.get("etag"),
            head_seq=raw.get("head_seq", 0),
            base=raw.get("base"),
            packs=dict(raw.get("packs", {})),
        )


@dataclass
class SyncResult:
    changed: bool
    head_seq: int
    downloaded: list[Digest] = field(default_factory=list)
    pruned: list[Digest] = field(default_factory=list)
    up_to_date_via_304: bool = False


class LocalRepo:
    """The warm cache for one repository on this node."""

    def __init__(self, settings: Settings, repo_id: str):
        self.settings = settings
        self.repo_id = repo_id
        self.wal = WalLog(settings, repo_id)
        self.objects = self.wal.objects
        self.path = Path(settings.cache_dir) / f"{repo_id}.git"
        self._lock_depth = 0

    # ------------------------------------------------------------------ state

    @property
    def state_path(self) -> Path:
        return self.path / STATE_FILE

    def load_state(self) -> CacheState:
        if not self.state_path.exists():
            return CacheState()
        try:
            return CacheState.from_json(json.loads(self.state_path.read_text()))
        except (json.JSONDecodeError, KeyError):
            return CacheState()

    def save_state(self, state: CacheState) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_json(), indent=2))
        tmp.replace(self.state_path)

    @contextmanager
    def lock(self):
        """Hold this repository's node-local lock: syncs and pushes take turns.

        Pushes need it as much as syncs do. git moves refs only *after*
        ``pre-receive`` has committed the push to the WAL. A sync landing in that
        window -- a fetch arriving at the same moment is enough -- moves the local
        refs to what the WAL now says; git then finds every ref "at the wrong old
        value", and the client is told a push failed that is in fact durable and
        live. So the server holds this lock across the whole of receive-pack, and
        ``sync`` takes it too.

        It is ``flock``-based, so a ``walgit sync`` run from the CLI waits its turn
        as well. It is re-entrant within one ``LocalRepo``, which is what lets the
        push path call ``sync()`` while already holding it. Instances are per
        request: never share one across threads.
        """
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / "walgit.lock").open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ setup

    def ensure_initialized(self) -> None:
        gitcmd.init_bare(self.path)
        self.install_hooks()

    def install_hooks(self) -> None:
        """Point the repo at hooks that run walgit's push path.

        `pre-receive` is where a push becomes durable. git will not update a
        single ref unless it exits zero, which is precisely the "never
        acknowledge a push until it has been fully persisted" rule.
        """
        hooks = self.path / "walgit-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ("pre-receive", "post-receive"):
            script = hooks / name
            script.write_text(
                "#!/bin/sh\n"
                f'exec "{sys.executable}" -m walgit.hooks {name} "{self.repo_id}"\n'
            )
            script.chmod(0o755)
        gitcmd.git("config", "core.hooksPath", str(hooks), cwd=self.path)

    def destroy(self) -> None:
        """Nuke the cache. The repository still exists -- it lives in S3."""
        shutil.rmtree(self.path, ignore_errors=True)

    # ------------------------------------------------------------------- sync

    def sync(self, force: bool = False) -> SyncResult:
        """Bring the local cache in line with the WAL.

        Do all of it while holding ``self.lock()``. A push in progress holds the
        same lock, and a sync that moves refs underneath it makes git reject a
        push that has already been committed (see ``lock``).

        Steps:

        1. conditional GET on the index. 304 -> nothing to do, return early.
        2. download every live pack we do not already have, and absorb it with
           ``git index-pack``. Note the order: objects first, refs second.
           A ref that points at an object we have not fetched yet is a broken
           repository, and someone may be cloning us right now.
        3. move the refs to exactly what the index says, in one transaction.
        4. drop packs the index no longer references (post-compaction garbage).
        5. record the ETag so the next sync can be a 304.
        """
        with self.lock():
            self.ensure_initialized()
            state = self.load_state()

            loaded = self.wal.load_if_changed(None if force else state.etag)
            if loaded is None:
                return SyncResult(
                    changed=False, head_seq=state.head_seq, up_to_date_via_304=True
                )
            index, etag = loaded

            downloaded = self._fetch_packs(index, state)
            gitcmd.set_refs(self.path, index.refs)
            gitcmd.set_head(self.path, index.refs)
            pruned = self._prune_packs(index, state)

            state.etag = etag
            state.head_seq = index.head_seq
            state.base = index.base.pack if index.base else None
            self.save_state(state)
            return SyncResult(
                changed=True,
                head_seq=index.head_seq,
                downloaded=downloaded,
                pruned=pruned,
            )

    def _fetch_packs(self, index: WalIndex, state: CacheState) -> list[Digest]:
        """Download and absorb every live pack this node is missing.

        ``state.packs`` maps object-store digest -> the stem of the files git wrote
        for that pack locally (``pack-<hash>``), so it is both the "do we have
        this?" check and the bookkeeping that ``_prune_packs`` later needs. Skip
        any digest already in there.

        For the rest: download into a temporary directory, hand the file to
        ``gitcmd.absorb_pack`` (which runs ``git index-pack`` and returns that
        ``pack-<hash>`` stem), and record it in ``state.packs``. Return the list
        of digests actually downloaded.

        Objects only -- do not touch refs here. The caller is responsible for
        ordering objects before refs, and splitting it this way is what makes
        that ordering visible instead of accidental.
        """
        downloaded = []
        with tempfile.TemporaryDirectory(prefix="walgit-pack-") as tmpdir:
            for digest in index.live_packs():
                if digest in state.packs:
                    continue
                local = Path(tmpdir) / f"{digest.split(':')[1]}.pack"
                self.objects.download(digest, local)
                state.packs[digest] = gitcmd.absorb_pack(self.path, local)
                downloaded.append(digest)
        return downloaded

    def _prune_packs(self, index: WalIndex, state: CacheState) -> list[Digest]:
        """Drop local packs the index no longer references.

        After a compaction the base pack contains everything the old incremental
        packs held, so those become dead weight on disk -- this is what reclaims
        it.

        For every digest in ``state.packs`` that is not in
        ``index.live_packs()``: forget it from ``state.packs`` and unlink the
        files git wrote for it in ``objects/pack`` -- ``<stem>.pack`` and
        ``<stem>.idx`` at minimum, plus the sidecars git may have produced
        (``.rev``, ``.promisor``, ``.mtimes``). ``unlink(missing_ok=True)`` will not
        tell you if the stem is wrong, so it is worth knowing it is exactly the
        ``pack-<hash>`` that ``absorb_pack`` returned. Return the pruned digests.

        Unlinking a pack that a concurrent ``upload-pack`` still has open is safe
        on Linux: the file stays alive until that reader closes it.
        """
        live = set(index.live_packs())
        pruned = []
        for digest in list(state.packs):
            if digest in live:
                continue
            name = state.packs.pop(digest)
            for suffix in (".pack", ".idx", ".rev", ".promisor", ".mtimes"):
                candidate = gitcmd.pack_dir(self.path) / f"{name}{suffix}"
                candidate.unlink(missing_ok=True)
            pruned.append(digest)
        return pruned

    # ------------------------------------------------------------------- misc

    def refs(self) -> dict[str, str]:
        return gitcmd.list_refs(self.path)


def materialize_snapshot(
    settings: Settings,
    index: WalIndex,
    through_seq: int,
    dest: Path,
    *,
    refs: dict[str, str],
) -> Path:
    """Build a throwaway repo holding exactly the state at ``through_seq``.

    Compaction uses this to repack a *pinned* prefix of the log without racing
    concurrent pushes: the snapshot is immutable by construction, so whatever
    lands while we repack simply stays in the log after the new base.

    Objects come from the index: the base pack, plus the pack of every pointer
    up to ``through_seq``. Refs come from the caller, because the index cannot
    say what they were at an arbitrary ``through_seq`` -- that history lives in
    the entry objects. Compaction already has it (``plan.refs``); a snapshot of
    the head is simply ``index.refs``. Set the refs only after absorbing every
    pack: a ref pointing at an object not yet in the repo is a broken repository.
    """
    dest = Path(dest)
    gitcmd.init_bare(dest)
    store = WalLog(settings, index.repo_id).objects
    with tempfile.TemporaryDirectory(prefix="walgit-snap-") as tmpdir:
        packs: list[Digest] = []
        if index.base:
            packs.append(index.base.pack)
        packs.extend(e.pack for e in index.entries if e.seq <= through_seq and e.pack)
        for digest in packs:
            local = Path(tmpdir) / f"{digest.split(':')[1]}.pack"
            store.download(digest, local)
            gitcmd.absorb_pack(dest, local)
    gitcmd.set_refs(dest, refs)
    gitcmd.set_head(dest, refs)
    return dest
