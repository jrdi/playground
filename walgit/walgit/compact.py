"""Exercise 3 -- compaction and garbage collection.

A repository that has been pushed to a thousand times is a thousand packfiles.
Clones would have to download all of them, and git would have to search every
one of them for every object. Compaction collapses a prefix of the log into a
single *base* pack, so the steady state is "one big pack plus the handful of
pushes since the last compaction".

The hard part is not the repacking -- git does that. The hard part is swapping
the result in without stopping the world:

* the repack runs against a **pinned snapshot** of the log (seqs 1..through),
  built in a scratch repo, so concurrent pushes cannot change what we are
  packing underneath us;
* the swap is a compare-and-swap that *splices*: it replaces the compacted
  prefix and keeps whatever tail arrived while we were busy;
* if another compactor got there first, the plan is refused rather than applied
  on top of a world it no longer describes;
* and nothing is deleted at swap time. Deletion is a separate, later, sweep
  with a grace period, because readers hold references to pack digests they
  read out of an older index.

That last rule is the one people get wrong. Reclaiming space in the same step
as the swap is how you make a clone that started 200ms ago fail.
"""

from __future__ import annotations

import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import gitcmd
from .objects import Digest
from .config import Settings
from .errors import CasConflict, CompactionRaced
from .repo import materialize_snapshot
from .wal import Base, WalEntry, WalIndex, WalLog

MAX_SWAP_ATTEMPTS = 8


@dataclass(frozen=True)
class CompactionPolicy:
    """When is it worth paying for a repack?"""

    min_entries: int = 8
    max_incremental_bytes: int = 4 * 1024 * 1024
    keep_recent: int = 0  # leave the N newest entries out of the base


@dataclass(frozen=True)
class CompactionPlan:
    through_seq: int
    entry_seqs: tuple[int, ...]
    input_packs: tuple[Digest, ...]
    input_bytes: int
    refs: dict[str, str]


@dataclass
class CompactionResult:
    plan: CompactionPlan
    new_pack: Digest
    new_size: int
    attempts: int
    index: WalIndex


@dataclass
class GcResult:
    deleted_packs: list[Digest] = field(default_factory=list)
    deleted_entries: list[str] = field(default_factory=list)
    retained_by_grace: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- policy


def should_compact(index: WalIndex, policy: CompactionPolicy) -> bool:
    """Is this repository worth compacting right now?

    Two independent triggers -- entry count (clone latency: every entry is a
    pack git must open) and incremental bytes (storage amplification). Either
    one firing is enough.
    """
    raise NotImplementedError(
        "EXERCISE 3.1: should_compact -- implement me (see the docstring above)"
    )


def plan_compaction(
    index: WalIndex, policy: CompactionPolicy, entries: Mapping[str, WalEntry]
) -> CompactionPlan | None:
    """Pin down exactly which prefix of the log this compaction will subsume.

    The plan is a promise about a specific range of sequence numbers; it is
    checked again at swap time. Capturing ``refs`` here (the ref state *as of*
    ``through_seq``, not "now") is what lets the new base carry its own
    snapshot, so a future compaction can still answer ``refs_at``.

    ``entries`` maps ``entry_id -> WalEntry`` for the live pointers
    (``WalLog.read_entries``). It is needed for that snapshot and nothing else:
    pointers are fixed-size and carry no ref updates, so the history up to the
    cut has to be replayed from the entry objects themselves.
    """
    raise NotImplementedError(
        "EXERCISE 3.2: plan_compaction -- implement me (see the docstring above)"
    )


def apply_compaction(
    index: WalIndex, plan: CompactionPlan, new_pack: Digest, new_size: int
) -> WalIndex:
    """Splice a finished compaction into ``index``, returning a new index.

    Pure function, no I/O -- so the concurrency argument is checkable by
    reading it. Refuses (``CompactionRaced``) when the plan no longer describes
    reality:

    * somebody already compacted through this point, or further;
    * the prefix of entries the plan covers is not the prefix that is there now.

    Entries appended *after* ``through_seq`` are preserved untouched: those are
    the pushes that landed while we were repacking, and they are still needed
    on top of the new base.
    """
    raise NotImplementedError(
        "EXERCISE 3.3: apply_compaction -- implement me (see the docstring above)"
    )


# ------------------------------------------------------------------ execution


def compact(
    settings: Settings,
    repo_id: str,
    policy: CompactionPolicy | None = None,
    max_attempts: int = MAX_SWAP_ATTEMPTS,
) -> CompactionResult | None:
    """Run one compaction pass. Returns ``None`` if the policy says not yet.

    The shape:

    1. load the index. If ``should_compact`` says no, return ``None`` before
       paying for anything else -- with ``WALGIT_AUTO_COMPACT`` on, this runs
       after every push. Otherwise fetch the entry objects
       (``wal.read_entries``) and ask ``plan_compaction`` for a plan. One GET
       per entry is cheap next to step 2, which downloads every one of those
       entries' packs;
    2. repack the pinned snapshot (``_repack_snapshot``) -- the slow part;
    3. re-read the index, because the repack may have taken minutes and
       pushes will have landed, then ``apply_compaction`` and compare-and-swap;
    4. on ``CasConflict``, back off and retry from step 3 -- the plan is still
       valid, we just lost a race. Do *not* redo the repack.

    ``CompactionRaced`` is not retryable and should propagate: it means
    another compactor already subsumed this range, so this pass has nothing
    left to contribute.
    """
    raise NotImplementedError(
        "EXERCISE 3.5: compact -- implement me (see the docstring above)"
    )


def _repack_snapshot(
    settings: Settings, index: WalIndex, plan: CompactionPlan
) -> tuple[Digest, int]:
    """Materialize the pinned prefix, let git repack it, upload the result.

    Four steps:

    1. build a scratch bare repo containing exactly the state at
       ``plan.through_seq`` (that is what ``materialize_snapshot`` is for),
       in a temporary directory that is thrown away afterwards. Hand it
       ``plan.refs``: the plan already replayed history up to the cut, so the
       snapshot never needs the entry objects itself;
    2. ``git repack -a -d -f`` inside it. ``-a`` puts everything in one pack,
       ``-d`` drops the packs it replaces, and ``-f`` recomputes deltas across
       the whole object set instead of reusing the per-push deltas -- that
       last flag is where the size win actually comes from, and it is why
       compaction costs CPU;
    3. expect exactly one ``*.pack`` to come out. Anything else means the
       snapshot was not what the plan described, so raise ``CompactionRaced``
       rather than guessing;
    4. upload it with ``put_file`` and return ``(digest, size_in_bytes)``.

    Nothing here touches the index. This function is pure computation over an
    immutable snapshot, which is exactly why concurrent pushes cannot spoil
    it.
    """
    raise NotImplementedError(
        "EXERCISE 3.4: _repack_snapshot -- implement me (see the docstring above)"
    )


# -------------------------------------------------------------------- sweeping


def unreferenced_packs(
    settings: Settings, repo_id: str, grace_seconds: float
) -> list[Digest]:
    """Packs no live index points at, old enough to be safe to delete.

    The age check is not paranoia. A pack becomes unreferenced the instant a
    compaction swaps the index -- but a clone that read the *previous* index a
    moment ago is still going to ask for it. The grace period is the window in
    which such a reader must finish. It also protects a pack that has been
    uploaded by a push that has not yet won its compare-and-swap: unreferenced,
    but about to be very referenced indeed.
    """
    raise NotImplementedError(
        "EXERCISE 3.6: unreferenced_packs -- implement me (see the docstring above)"
    )


def gc(settings: Settings, repo_id: str, grace_seconds: float = 3600.0) -> GcResult:
    """Delete unreachable packs and superseded WAL entry objects.

    Liveness comes from a *freshly loaded* index -- never from one a caller
    handed you, which may predate a push that made an object live again.

    Two sweeps, same rule. A pack is garbage if it is not in
    ``index.live_packs()``; a WAL entry object is garbage if no pointer in
    ``index.entries`` names its key (compaction is what strands them). In
    both cases, delete only if the object is older than ``grace_seconds`` --
    otherwise record it in ``retained_by_grace`` and leave it alone.

    The grace period is doing two different jobs at once, which is why it
    cannot be dropped: it gives readers holding an older index time to finish
    fetching packs that just became unreferenced, *and* it protects an object
    that has been uploaded by a push whose compare-and-swap has not landed
    yet. That second one is indistinguishable from garbage by inspection --
    only its age tells them apart.
    """
    raise NotImplementedError(
        "EXERCISE 3.7: gc -- implement me (see the docstring above)"
    )
