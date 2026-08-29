"""Exercise 2 -- the write-ahead log and its compare-and-swap index.

The shape of the system, in one paragraph:

    A push is durable before it is visible. The packfile goes into the object
    store (Exercise 1), then an immutable WAL *entry* object records what that
    pack means (which refs move, from what to what). Neither is reachable
    yet. The push only becomes real when a pointer to that entry is spliced
    into the repository's *index*
    object -- and that splice is an atomic compare-and-swap on S3, using
    If-Match against the ETag we read. One writer wins, everyone else gets a
    412, reloads, re-validates and retries.

That is the whole concurrency control mechanism. No consensus protocol, no
leader election, no lock service: S3's conditional PUT is the linearization
point, so any node can accept any push.

Layout in the bucket::

    repos/<id>/index.json              <- the compare-and-swap target; mutable
    repos/<id>/wal/<entry_id>.json     <- immutable push records
    repos/<id>/objects/sha256/ab/cd/.. <- immutable packfiles (Exercise 1)

One rule decides what goes in the index: **nothing in it may grow with the size
of a push.** S3 cannot append to or patch an object, so every compare-and-swap
rewrites the whole index. Whatever lives there is paid for again by every later
push, and a bigger index is a slower PUT and a wider window in which to lose the
race. So a pointer holds only fixed-size fields -- its sequence number, its
entry's key, its pack's digest and size -- and what a push actually *did* (one
ref update per ref it moved, unbounded) lives only in the entry object.

The price is paid on the read side: replaying ref history costs one GET per
entry (``WalLog.read_entries``). The hot paths never pay it. Validating a push
needs only the current ``refs``, and syncing a node needs only the pack digests,
and both stay in the index. (``refs`` is the one thing left that grows -- with
the number of refs in the repository rather than with any single push. The
README's stretch goals take the same rule one step further.)

What the index is *not* is reconstructible from ``wal/``. Entry objects carry no
sequence number, because order is assigned by whoever wins the swap, and the GC
deletes the entries of compacted pushes. The index is the log's only record of
order.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from botocore.exceptions import ClientError

from . import s3 as s3mod
from .objects import ContentStore, Digest
from .config import Settings
from .errors import CasConflict, RefConflict

MAX_CAS_ATTEMPTS = 12


@dataclass(frozen=True)
class RefUpdate:
    """One ref moving from ``old`` to ``new``.

    ``old is None`` means "this ref must not exist yet" (a create), and
    ``new is None`` means "delete this ref". git's zero-oid is normalized away
    at the edges so the rest of the code never has to think about it.
    """

    ref: str
    old: str | None
    new: str | None

    def to_json(self) -> dict[str, Any]:
        return {"ref": self.ref, "old": self.old, "new": self.new}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "RefUpdate":
        return cls(ref=raw["ref"], old=raw["old"], new=raw["new"])


@dataclass(frozen=True)
class WalEntry:
    """The immutable record of one push, stored as its own S3 object.

    It carries no sequence number: ordering is assigned by whoever wins the
    compare-and-swap on the index, which may take several attempts.
    """

    entry_id: str
    kind: str  # "push" | "compaction"
    pack: Digest | None
    pack_size: int
    ref_updates: tuple[RefUpdate, ...]
    created_at: float
    author: str = "anonymous"

    @classmethod
    def new_push(
        cls,
        ref_updates: Iterable[RefUpdate],
        pack: Digest | None,
        pack_size: int = 0,
        author: str = "anonymous",
    ) -> "WalEntry":
        return cls(
            entry_id=uuid.uuid4().hex,
            kind="push",
            pack=pack,
            pack_size=pack_size,
            ref_updates=tuple(ref_updates),
            created_at=time.time(),
            author=author,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "pack": self.pack,
            "pack_size": self.pack_size,
            "ref_updates": [u.to_json() for u in self.ref_updates],
            "created_at": self.created_at,
            "author": self.author,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "WalEntry":
        return cls(
            entry_id=raw["entry_id"],
            kind=raw["kind"],
            pack=raw["pack"],
            pack_size=raw["pack_size"],
            ref_updates=tuple(RefUpdate.from_json(u) for u in raw["ref_updates"]),
            created_at=raw["created_at"],
            author=raw.get("author", "anonymous"),
        )


@dataclass(frozen=True)
class WalPointer:
    """An entry as seen from the index: where it sits in the log, and where it lives.

    Fixed-size by design: every field is the same size whether the push moved one
    ref or fifty thousand. What the push actually did is in the entry object at
    ``key``. ``pack`` and ``pack_size`` earn their place here because syncing, GC
    and compaction sizing need them for every entry, and fetching each entry
    object just to learn a digest would turn every sync into one GET per entry.
    """

    seq: int
    entry_id: str
    key: str
    pack: Digest | None
    pack_size: int

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "key": self.key,
            "pack": self.pack,
            "pack_size": self.pack_size,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "WalPointer":
        return cls(
            seq=raw["seq"],
            entry_id=raw["entry_id"],
            key=raw["key"],
            pack=raw["pack"],
            pack_size=raw["pack_size"],
        )


@dataclass(frozen=True)
class Base:
    """The compacted prefix of the log: one pack standing in for seqs 1..through_seq."""

    pack: Digest
    pack_size: int
    through_seq: int
    refs: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {
            "pack": self.pack,
            "pack_size": self.pack_size,
            "through_seq": self.through_seq,
            "refs": dict(self.refs),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Base":
        return cls(
            pack=raw["pack"],
            pack_size=raw["pack_size"],
            through_seq=raw["through_seq"],
            refs=dict(raw["refs"]),
        )


@dataclass
class WalIndex:
    """The mutable head of the log. One S3 object, replaced by compare-and-swap.

    ``version`` is not used for concurrency control -- the ETag is -- it exists
    so humans and logs can tell two generations apart at a glance.
    """

    repo_id: str
    version: int = 0
    next_seq: int = 1
    base: Base | None = None
    entries: list[WalPointer] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ (de)serialize

    def to_json(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "version": self.version,
            "next_seq": self.next_seq,
            "base": self.base.to_json() if self.base else None,
            "entries": [e.to_json() for e in self.entries],
            "refs": dict(self.refs),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "WalIndex":
        return cls(
            repo_id=raw["repo_id"],
            version=raw["version"],
            next_seq=raw["next_seq"],
            base=Base.from_json(raw["base"]) if raw.get("base") else None,
            entries=[WalPointer.from_json(e) for e in raw["entries"]],
            refs=dict(raw["refs"]),
        )

    def copy(self) -> "WalIndex":
        return WalIndex.from_json(json.loads(json.dumps(self.to_json())))

    # ------------------------------------------------------------------ query

    @property
    def head_seq(self) -> int:
        """Sequence number of the newest entry (0 for an empty repository)."""
        if self.entries:
            return self.entries[-1].seq
        return self.base.through_seq if self.base else 0

    def live_packs(self) -> list[Digest]:
        """Every pack a reader still needs: the base plus each live entry."""
        packs: list[Digest] = []
        if self.base:
            packs.append(self.base.pack)
        packs.extend(e.pack for e in self.entries if e.pack)
        return packs

    def incremental_bytes(self) -> int:
        return sum(e.pack_size for e in self.entries)

    def refs_at(self, seq: int, entries: Mapping[str, WalEntry]) -> dict[str, str]:
        """Reconstruct the ref state as it stood at sequence ``seq``.

        Returns a fresh dict (``ref name -> oid``) describing the repository the
        instant entry ``seq`` was applied. It is a query, not a mutation: neither
        ``self.refs`` nor any pointer may be modified, and callers routinely keep
        the result around, so do not hand back a dict you still alias.

        Start from the compaction base's ref snapshot (``self.base.refs``, or an
        empty dict when the repository has never been compacted), then replay the
        ref updates of every pointer with ``pointer.seq <= seq``, in order.
        Entries are stored in ascending sequence order, so you can stop at the
        first one past ``seq``.

        The index does not hold those ref updates -- pointers are fixed-size, see
        ``WalPointer`` -- so the caller passes the entry objects in. ``entries``
        maps ``entry_id -> WalEntry`` (``WalLog.read_entries`` builds it) and must
        cover every pointer up to ``seq``. A missing one is a caller bug: let the
        ``KeyError`` escape rather than silently replaying a partial history.
        Taking the entries as an argument, instead of fetching them here, is what
        keeps this function pure and testable without S3.

        Replay with validation **off** (``_apply_updates(..., validate=False)``).
        These updates were validated when they were accepted; re-checking them
        here would compare each update's ``old`` against a half-replayed state
        and raise ``RefConflict`` on perfectly good history.

        Two properties worth holding onto:

        * ``index.refs_at(index.head_seq, entries) == index.refs`` -- replaying
          everything reproduces the live ref state, which is the cheapest
          self-check you have on this function;
        * the earliest answerable point is ``base.through_seq``. Ask for anything
          older and you get the base snapshot, because that history was packed
          away and genuinely no longer exists.

        This is what lets compaction record the refs *as of* its cut point rather
        than "now" (see ``plan_compaction``), and recording them is in turn what
        keeps this function answerable after the next compaction throws the
        entries away. The two depend on each other: drop the snapshot from
        ``Base`` and ref history stops at the most recent compaction.
        """
        raise NotImplementedError(
            "EXERCISE 2.2: WalIndex.refs_at -- implement me (see the docstring above)"
        )

    def find(self, entry_id: str) -> WalPointer | None:
        for pointer in self.entries:
            if pointer.entry_id == entry_id:
                return pointer
        return None

    # ----------------------------------------------------------------- mutate

    def apply_push(self, entry: WalEntry, key: str) -> "WalIndex":
        """Return a *new* index with ``entry`` appended at the next sequence.

        Raises ``RefConflict`` if any ref's expected old value disagrees with
        this index. That check is the reason pushes are linearizable: git
        validated the old values against whatever the accepting node happened
        to have on disk, which may already be stale. The WAL is the authority,
        and it re-validates at the moment of the swap.

        Idempotent on ``entry_id``: replaying an entry that already landed
        returns an equivalent index rather than duplicating the push.
        """
        raise NotImplementedError(
            "EXERCISE 2.3: WalIndex.apply_push -- implement me (see the docstring above)"
        )


def _apply_updates(
    refs: dict[str, str], updates: Iterable[RefUpdate], validate: bool
) -> dict[str, str]:
    """Apply ref updates to ``refs`` in place, optionally enforcing old values.

    With ``validate=True`` each update's ``old`` must equal what ``refs``
    currently holds for that ref -- with ``None`` meaning "the ref must not
    exist" -- and anything else raises ``RefConflict(ref, expected, actual)``.
    This is the check that makes pushes linearizable, so it has to compare
    against the state being handed in, not against anything cached.

    With ``validate=False`` the updates are replayed unconditionally. That
    mode is for ``refs_at``, which is re-deriving history that was already
    accepted: those updates were validated when they landed, and re-checking
    them against a partially-replayed state would fail for no reason.

    Apply the updates in order -- two updates to the same ref inside one push
    must compose, the second seeing the first. ``new is None`` deletes the
    ref. Returns the same dict it mutated.
    """
    raise NotImplementedError(
        "EXERCISE 2.1: _apply_updates -- implement me (see the docstring above)"
    )


class WalLog:
    """S3-backed WAL for one repository."""

    def __init__(self, settings: Settings, repo_id: str):
        self.settings = settings
        self.repo_id = repo_id
        self.bucket = settings.bucket
        self.s3 = s3mod.client(settings)
        self.objects = ContentStore(settings, prefix=f"repos/{repo_id}/objects")
        self.stats: dict[str, int] = {"cas_conflicts": 0, "commits": 0, "loads": 0}

    # ------------------------------------------------------------------- keys

    @property
    def index_key(self) -> str:
        return f"repos/{self.repo_id}/index.json"

    def entry_key(self, entry_id: str) -> str:
        return f"repos/{self.repo_id}/wal/{entry_id}.json"

    # ------------------------------------------------------------------- read

    def load(self) -> tuple[WalIndex, str | None]:
        """Read the index object and its ETag. Returns ``(index, etag)``.

        What to call: ``self.s3.get_object(Bucket=self.bucket, Key=self.index_key)``.
        The response is a dict; the bytes are in ``resp["Body"].read()`` (JSON --
        turn it back into an object with ``WalIndex.from_json(json.loads(...))``)
        and the ETag is ``resp["ETag"]``.

        Return the ETag **verbatim**, surrounding double quotes included (S3 sends
        ``'"967dccd5..."'``). It is an opaque token, not something to parse: the
        only thing you will ever do with it is hand it back to S3 unchanged in
        ``commit``'s ``If-Match``, and a stripped or re-quoted value will never
        match anything.

        The one case to handle -- a repository nobody has pushed to yet:

        * there is no ``index.json`` at all, so ``get_object`` raises
          ``botocore.exceptions.ClientError``;
        * ``s3.is_not_found(exc)`` recognises it; that is **not an error**, it is
          an empty log. Return ``(WalIndex(repo_id=self.repo_id), None)``;
        * the ``None`` is load-bearing: ``commit`` reads it as "no index exists
          yet" and switches to create-if-absent. Returning a fake ETag here
          would make the very first push to every repository fail;
        * any other ``ClientError`` (403, 500, no bucket) is a real failure and
          must propagate. Swallowing it would make an outage look like an empty
          repository -- and the next push would happily create one over the top.

        Optionally bump ``self.stats["loads"]``; nothing asserts on it.

        Tests: ``test_load_of_unknown_repo_is_an_empty_log`` (the ``None`` path),
        ``test_append_persists_entry_then_publishes_it`` (the ETag you return must
        equal the one ``commit`` returned).
        """
        raise NotImplementedError(
            "EXERCISE 2.4: WalLog.load -- implement me (see the docstring above)"
        )

    def load_if_changed(self, etag: str | None) -> tuple[WalIndex, str | None] | None:
        """Conditional GET: ``None`` if ``etag`` is still current, else ``(index, etag)``.

        The question this answers is "has anyone pushed since I last looked?", and
        it answers it without downloading the index when the answer is no. That is
        what lets a warm cache verify itself on every fetch for the cost of one
        tiny round-trip.

        What to call: the same ``get_object`` as ``load``, plus ``IfNoneMatch=etag``.
        S3 then compares that ETag with the object's current one:

        * **different** -> a normal 200 with the new body. Parse it exactly like
          ``load`` and return ``(index, resp["ETag"])``;
        * **same** -> HTTP 304 Not Modified, no body. boto3 does *not* return
          a response for this -- it raises ``ClientError``. Detect it with
          ``s3.is_not_modified(exc)`` and return ``None``.

        Three more cases, all short:

        * ``etag is None`` (the caller has never seen an index): there is nothing
          to compare against, so just return ``self.load()``;
        * 404 (the index vanished, or never existed): the same "empty log" answer
          as ``load`` -- ``(WalIndex(repo_id=self.repo_id), None)``;
        * any other ``ClientError``: re-raise.

        Watch the header name. ``If-None-Match`` on a GET means "send the body
        only if it changed". The same header on a PUT, with the value ``*``,
        means "create only if absent" (that is ``commit``). One header, two
        unrelated meanings -- S3 inherited both from HTTP.

        Test: ``test_load_if_changed_returns_none_when_current``.
        """
        raise NotImplementedError(
            "EXERCISE 2.5: WalLog.load_if_changed -- implement me (see the docstring above)"
        )

    def read_entry(self, key: str) -> WalEntry:
        resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        return WalEntry.from_json(json.loads(resp["Body"].read()))

    def read_entries(
        self, pointers: Iterable[WalPointer], workers: int = 16
    ) -> dict[str, WalEntry]:
        """Fetch the entry objects behind ``pointers``, keyed by ``entry_id``.

        This is the bill for keeping pointers fixed-size: anything that needs to
        know what past pushes *did* pays one GET per entry. The GETs run
        concurrently, for the same reason as in ``ContentStore.missing``. Only
        history readers call this -- compaction planning and ``walgit show`` --
        never the push path or the sync path.
        """
        pointers = list(pointers)
        if not pointers:
            return {}
        with ThreadPoolExecutor(max_workers=min(workers, len(pointers))) as pool:
            entries = list(pool.map(lambda pointer: self.read_entry(pointer.key), pointers))
        return {entry.entry_id: entry for entry in entries}

    def list_entry_keys(self) -> list[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=f"repos/{self.repo_id}/wal/"
        ):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def entry_age(self, key: str) -> float:
        resp = self.s3.head_object(Bucket=self.bucket, Key=key)
        return time.time() - resp["LastModified"].timestamp()

    # ------------------------------------------------------------------ write

    def put_entry(self, entry: WalEntry) -> str:
        """Persist the entry object and return its key. Not yet visible."""
        key = self.entry_key(entry.entry_id)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(entry.to_json(), indent=2).encode(),
            ContentType="application/json",
        )
        return key

    def commit(self, index: WalIndex, etag: str | None) -> str:
        """Compare-and-swap ``index`` into S3. Returns the new ETag.

        This is the linearization point of the whole system: the single PUT that
        decides which of several racing pushes happened first. Everything else in
        the WAL exists to make this one call safe to lose.

        What to call: ``self.s3.put_object`` with

        * ``Bucket=self.bucket``, ``Key=self.index_key``;
        * ``Body=json.dumps(index.to_json(), indent=2).encode()`` -- the indent is
          only for humans reading the bucket; S3 compares ETags, not JSON;
        * ``ContentType="application/json"``;
        * exactly **one** precondition, picked by ``etag``:

          - ``etag is None`` -> ``IfNoneMatch="*"``: write only if no index exists.
            Two nodes racing to create a brand-new repository cannot both win;
          - otherwise -> ``IfMatch=etag``: write only if the object is still
            byte-for-byte the version you loaded.

        Return ``resp["ETag"]`` -- the ETag of what you just wrote, so the caller
        can chain another conditional write or a 304 check without reloading.

        Losing: S3 answers **412 Precondition Failed**, which boto3 raises as a
        ``ClientError``. Recognise it with ``s3.is_precondition_failed(exc)``, then:

        1. increment ``self.stats["cas_conflicts"]``; the test
           ``test_append_retries_through_contention`` asserts it went up
           by exactly one;
        2. ``raise CasConflict(...) from exc``.

        Any other ``ClientError`` propagates untouched.

        Things that are *not* this function's job, so do not add them:

        * retrying. ``append`` owns the loop, because a correct retry has to
          reload and re-run ``apply_push`` -- replaying the same PUT would just
          lose again, forever;
        * modifying ``index``. The version bump already happened in
          ``apply_push``; ``commit`` writes exactly what it is given.

        Tests: ``test_commit_with_stale_etag_raises_cas_conflict`` (the ``IfMatch``
        path), ``test_commit_creating_an_index_twice_raises`` (the ``IfNoneMatch``
        path), ``test_append_retries_through_contention`` (the counter).
        """
        raise NotImplementedError(
            "EXERCISE 2.6: WalLog.commit -- implement me (see the docstring above)"
        )

    def append(
        self, entry: WalEntry, max_attempts: int = MAX_CAS_ATTEMPTS
    ) -> tuple[WalPointer, WalIndex, str]:
        """Durably append a push and make it visible. The heart of the system.

        Order matters and is not negotiable:

        1. the pack is already in the object store (the caller put it there),
        2. the entry object is written -- durable, still unreachable,
        3. the index is compare-and-swapped to point at it -- now visible.

        A crash between 2 and 3 leaves an orphan entry object that no reader can
        ever observe, and that the GC will sweep once it is older than the
        grace period. A crash after 3 is simply a completed push. There is no
        window in which a push is half-visible, and none in which we have
        acknowledged something we have not persisted.

        Retries on ``CasConflict`` only. ``RefConflict`` propagates immediately:
        the client is stale and retrying cannot fix that.
        """
        raise NotImplementedError(
            "EXERCISE 2.7: WalLog.append -- implement me (see the docstring above)"
        )
