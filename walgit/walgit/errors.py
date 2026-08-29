"""Error taxonomy for walgit.

The distinction that matters most here is between *retryable* and *terminal*
failures:

* ``CasConflict``   -- somebody else won the race. Reload, re-validate, retry.
* ``RefConflict``   -- the push is stale (non-fast-forward / lost update).
                       Retrying is pointless: the client must fetch and rebase.
* ``CompactionRaced`` -- the world moved under a compaction plan. Re-plan.

Getting this classification wrong is how distributed systems end up either
livelocked or silently losing writes.
"""

from __future__ import annotations


class WalGitError(Exception):
    """Base class for every error raised by walgit."""


class CasConflict(WalGitError):
    """A compare-and-swap on the WAL index lost the race.

    Retryable: reload the index, re-apply your change, try again.
    """


class RefConflict(WalGitError):
    """A ref update's expected old value does not match the WAL's state.

    Terminal for this push: the client is out of date. This is the check that
    makes pushes linearizable -- git's own local check is not enough, because
    another instance may have committed a push between the client's fetch and
    its push.
    """

    def __init__(self, ref: str, expected: str | None, actual: str | None):
        self.ref = ref
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"ref {ref}: expected old value {expected or '<absent>'}, "
            f"WAL has {actual or '<absent>'}"
        )


class CompactionRaced(WalGitError):
    """The compaction plan no longer applies to the current index."""


class ObjectNotFound(WalGitError):
    """A content-addressed object is missing from the store."""


class CorruptObject(WalGitError):
    """Bytes read back from the store do not hash to the digest we asked for."""


class GitCommandError(WalGitError):
    """A git subprocess exited non-zero."""

    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed ({returncode}): {stderr.strip()}")
