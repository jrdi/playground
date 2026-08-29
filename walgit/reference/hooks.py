"""Exercise 4b -- the push path, as a real git hook.

When you run `git push`, git's `receive-pack` on the server side:

1. receives the packfile and indexes it into a **quarantine** directory
   (``$GIT_QUARANTINE_PATH``) -- the objects exist on disk but are invisible to
   the rest of the repository;
2. runs ``pre-receive`` with the proposed ref updates on stdin;
3. if and only if that hook exits zero, migrates the quarantined objects into
   the repository and commits the ref transaction;
4. runs ``post-receive``.

Step 3 is a gift: it gives us an abort point *after* we have the actual bytes
and *before* the push is acknowledged. So `pre-receive` is where walgit uploads
the pack to the object store, writes the WAL entry, and compare-and-swaps the
index. If any of that fails we exit non-zero and git tells the client the push
was rejected -- nothing was acknowledged, and nothing was persisted halfway.

Run as: ``python -m walgit.hooks pre-receive <repo_id>``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .config import Settings
from .errors import CasConflict, RefConflict, WalGitError
from .gitcmd import is_zero
from .wal import RefUpdate, WalEntry, WalLog

PENDING_FILE = "walgit-pending.json"


def parse_updates(stdin_text: str) -> list[RefUpdate]:
    """Parse receive-pack's stdin: ``<old-oid> <new-oid> <ref-name>`` per line.

    git uses an all-zero oid for "did not exist" and for "delete it"; walgit
    uses ``None`` for both, so the zero-oid convention stops at this boundary.
    """
    updates = []
    for line in stdin_text.splitlines():
        line = line.strip()
        if not line:
            continue
        old, new, ref = line.split(" ", 2)
        updates.append(
            RefUpdate(
                ref=ref,
                old=None if is_zero(old) else old,
                new=None if is_zero(new) else new,
            )
        )
    return updates


def find_quarantine_pack(repo: Path) -> Path | None:
    """Locate the packfile receive-pack just staged, if any.

    ``receive.unpackLimit=0`` (set at repo init) makes receive-pack keep incoming
    objects as a pack instead of exploding them into loose objects, so there is
    at most one to find. Two shapes of push are easy to get wrong:

    * a push that brings no *new* objects (a branch or tags pointing at commits
      the server already has) still sends a pack -- an empty, 32-byte one. Return
      it like any other; every such push dedupes to the same stored object.
    * a push that only deletes refs sends no pack at all: return ``None``.

    The caller reads ``None`` as "this push carries no objects", which is only
    true if nothing arrived loose. So check: if the quarantine holds any loose
    objects (``<quarantine>/ab/cdef...``), raise ``WalGitError`` instead of
    returning. With a misconfigured ``unpackLimit`` a small push arrives as loose
    objects and no pack, and would otherwise be recorded in the WAL with none of
    its objects in S3 -- acknowledged, visible, and unservable by any other node.
    """
    quarantine = os.environ.get("GIT_QUARANTINE_PATH")
    if not quarantine:
        return None
    qdir = Path(quarantine)
    if not qdir.is_absolute():
        qdir = repo / qdir
    loose = [p for p in qdir.glob("[0-9a-f][0-9a-f]/*") if p.is_file()]
    if loose:
        raise WalGitError(
            f"{len(loose)} object(s) arrived loose instead of in a pack; refusing a push "
            "whose objects would never reach S3 (is receive.unpackLimit still 0?)"
        )
    packs = sorted((qdir / "pack").glob("*.pack"))
    if not packs:
        return None
    if len(packs) > 1:
        raise WalGitError(f"expected one quarantined pack, found {len(packs)}")
    return packs[0]


def pre_receive(repo_id: str, settings: Settings, repo: Path, stdin_text: str) -> int:
    """Make the push durable, or reject it.

    The ordering rule from Exercise 2 applies end to end here: pack to the
    object store, entry object, then the index compare-and-swap. Only when that
    returns do we exit zero and let git update its refs.
    """
    updates = parse_updates(stdin_text)
    if not updates:
        return 0

    wal = WalLog(settings, repo_id)
    pack_path = find_quarantine_pack(repo)

    pack_digest = None
    pack_size = 0
    if pack_path is not None:
        pack_size = pack_path.stat().st_size
        pack_digest = wal.objects.put_file(pack_path)

    entry = WalEntry.new_push(
        ref_updates=updates,
        pack=pack_digest,
        pack_size=pack_size,
        author=os.environ.get("REMOTE_USER", "anonymous"),
    )

    try:
        pointer, index, etag = wal.append(entry)
    except RefConflict as exc:
        _say(f"rejected: {exc}")
        _say("someone else pushed while you were pushing -- fetch and retry")
        return 1
    except CasConflict as exc:
        _say(f"rejected: {exc}")
        return 1

    _say(
        f"seq={pointer.seq} entry={pointer.entry_id[:12]} "
        f"pack={(pack_digest or 'none')[:19]} {pack_size}B "
        f"index-version={index.version}"
    )

    # Hand the cache-bookkeeping to post-receive: the transaction can still be
    # aborted after we return. Losing this file costs a redundant download on
    # the next sync, nothing more.
    if pack_digest and pack_path is not None:
        (repo / PENDING_FILE).write_text(
            json.dumps(
                {
                    "etag": etag,
                    "head_seq": pointer.seq,
                    "pack": pack_digest,
                    # Post-migration the pack keeps its quarantine name, which
                    # git derived from the pack's own content hash.
                    "pack_name": pack_path.stem,
                }
            )
        )
    return 0


def post_receive(repo_id: str, settings: Settings, repo: Path, stdin_text: str) -> int:
    """Record what we already have locally, then optionally compact.

    Pure optimization: without it the next ``sync`` re-downloads the pack this
    node just received. Correctness never depends on it, which is exactly why
    it is safe to do after the point of no return.
    """
    from . import gitcmd
    from .repo import LocalRepo

    # git just moved refs behind our back. If this was the repo's first push,
    # HEAD is still pointing at whatever `git init` guessed, and a clone would
    # come back empty -- so re-point it now rather than waiting for a sync that
    # the next conditional GET will (correctly) answer with a 304.
    gitcmd.set_head(repo, gitcmd.list_refs(repo))

    pending_path = repo / PENDING_FILE
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text())
            local = LocalRepo(settings, repo_id)
            state = local.load_state()
            state.packs[pending["pack"]] = pending["pack_name"]
            state.head_seq = pending["head_seq"]
            state.etag = pending["etag"]
            local.save_state(state)
        finally:
            pending_path.unlink(missing_ok=True)

    if settings.auto_compact:
        from .compact import CompactionPolicy, compact

        try:
            result = compact(settings, repo_id, CompactionPolicy())
            if result:
                _say(
                    f"compacted through seq={result.plan.through_seq} "
                    f"into {result.new_pack[:19]} ({result.new_size}B)"
                )
        except WalGitError as exc:
            _say(f"compaction skipped: {exc}")
    return 0


def _say(message: str) -> None:
    """Write to stderr -- git relays it to the pushing client's terminal."""
    sys.stderr.write(f"walgit: {message}\n")
    sys.stderr.flush()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        _say("usage: python -m walgit.hooks <pre-receive|post-receive> <repo_id>")
        return 2
    hook, repo_id = argv[1], argv[2]
    settings = Settings.from_env()
    repo = Path(os.environ.get("GIT_DIR", ".")).absolute()
    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""

    handlers = {"pre-receive": pre_receive, "post-receive": post_receive}
    handler = handlers.get(hook)
    if handler is None:
        _say(f"unknown hook {hook}")
        return 2
    try:
        return handler(repo_id, settings, repo, stdin_text)
    except Exception as exc:  # noqa: BLE001 - a hook must never leak a traceback as success
        _say(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
