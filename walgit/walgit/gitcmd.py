"""Subprocess plumbing around real git. Provided for you -- no exercises here.

Everything walgit does to a repository is done with git's *plumbing* commands,
never porcelain: `index-pack` to absorb a pack, `update-ref --stdin` to move
refs in one transaction, `repack` to compact. That is exactly what a real git
server does, which is why real `git push` / `git clone` work against this.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitCommandError

ZERO_OID = "0" * 40


def is_zero(oid: str) -> bool:
    """git spells 'this ref does not exist' as an all-zero object id."""
    return set(oid) == {"0"}


def git(
    *args: str,
    cwd: Path | str | None = None,
    input: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    argv = ["git", *args]
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        input=input,
        capture_output=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise GitCommandError(list(args), proc.returncode, proc.stderr.decode(errors="replace"))
    return proc


def git_text(*args: str, **kwargs) -> str:
    return git(*args, **kwargs).stdout.decode().strip()


def init_bare(path: Path) -> None:
    """Create a bare repo configured the way a walgit node needs it."""
    path.mkdir(parents=True, exist_ok=True)
    if not (path / "HEAD").exists():
        git("init", "--bare", "--quiet", str(path))
    # Keep incoming objects as a packfile instead of exploding them into loose
    # objects -- the pre-receive hook needs a pack to upload to the object store.
    git("config", "receive.unpackLimit", "0", cwd=path)
    git("config", "transfer.unpackLimit", "0", cwd=path)
    # Serve pushes over smart HTTP.
    git("config", "http.receivepack", "true", cwd=path)
    git("config", "http.uploadpack", "true", cwd=path)
    # Deleting the last ref is legitimate here; the WAL is the source of truth.
    git("config", "receive.denyDeleteCurrent", "ignore", cwd=path)
    git("config", "receive.denyNonFastForwards", "false", cwd=path)
    git("config", "gc.auto", "0", cwd=path)


def pack_dir(repo: Path) -> Path:
    return repo / "objects" / "pack"


def absorb_pack(repo: Path, pack_path: Path) -> str:
    """Index a packfile into ``repo`` and return its file name stem, ``pack-<hash>``.

    That stem is what git's files in ``objects/pack`` are called (``<stem>.pack``,
    ``<stem>.idx``, ...) and the same shape post-receive records, so whoever keeps
    it can later find -- and delete -- exactly those files. ``git index-pack
    --stdin`` itself prints ``pack<TAB><hash>``; passing that line through would
    name files that do not exist, and a later unlink would miss them silently.

    Idempotent: git names the pack after its own content hash, so absorbing the
    same pack twice is a no-op rather than an error.
    """
    with pack_path.open("rb") as fh:
        proc = git("index-pack", "--stdin", "--fix-thin", cwd=repo, input=fh.read())
    kind, _, digest = proc.stdout.decode().strip().partition("\t")
    if kind not in ("pack", "keep") or not digest:
        raise GitCommandError(["index-pack", "--stdin"], proc.returncode,
                              f"unexpected index-pack output: {proc.stdout!r}")
    return f"pack-{digest}"


def list_refs(repo: Path) -> dict[str, str]:
    out = git_text("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)
    refs: dict[str, str] = {}
    for line in out.splitlines():
        if line.strip():
            name, oid = line.split(" ", 1)
            refs[name] = oid.strip()
    return refs


def set_refs(repo: Path, target: dict[str, str]) -> None:
    """Make ``repo``'s refs exactly ``target``, in one atomic ref transaction."""
    current = list_refs(repo)
    commands = []
    for ref, oid in target.items():
        if current.get(ref) != oid:
            commands.append(f"update {ref}\x00{oid}\x00\x00")
    for ref in current:
        if ref not in target:
            commands.append(f"delete {ref}\x00\x00")
    if not commands:
        return
    payload = "".join(commands).encode()
    git("update-ref", "--stdin", "-z", cwd=repo, input=payload)


def set_head(repo: Path, refs: dict[str, str]) -> None:
    """Point HEAD at a branch that exists, so `git clone` checks something out."""
    for candidate in ("refs/heads/main", "refs/heads/master"):
        if candidate in refs:
            git("symbolic-ref", "HEAD", candidate, cwd=repo)
            return
    branches = sorted(r for r in refs if r.startswith("refs/heads/"))
    if branches:
        git("symbolic-ref", "HEAD", branches[0], cwd=repo)


def object_count(repo: Path) -> int:
    out = git_text("count-objects", "-v", cwd=repo)
    total = 0
    for line in out.splitlines():
        key, _, value = line.partition(": ")
        if key in ("count", "in-pack"):
            total += int(value)
    return total
