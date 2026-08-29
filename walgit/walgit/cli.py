"""Command line. Provided for you -- `show` in particular is your debugger.

    python -m walgit serve            # smart-HTTP git server
    python -m walgit show demo        # pretty-print the WAL index in S3
    python -m walgit compact demo     # run one compaction pass
    python -m walgit gc demo --grace 0
    python -m walgit drop-cache demo  # delete the local repo; S3 is the truth
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import s3 as s3mod
from .compact import CompactionPolicy, compact, gc
from .config import Settings
from .repo import LocalRepo, materialize_snapshot
from .server import serve
from .wal import WalLog


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}"


def cmd_show(args, settings: Settings) -> int:
    wal = WalLog(settings, args.repo)
    index, etag = wal.load()
    entries = wal.read_entries(index.entries)
    print(f"repo      {index.repo_id}")
    print(f"etag      {etag}")
    print(f"version   {index.version}   next_seq={index.next_seq}   head_seq={index.head_seq}")
    if index.base:
        print(
            f"base      {index.base.pack[:26]}...  through_seq={index.base.through_seq}  "
            f"{human(index.base.pack_size)}  ({len(index.base.refs)} refs)"
        )
    else:
        print("base      <none, never compacted>")
    print(
        f"entries   {len(index.entries)}  ({human(index.incremental_bytes())} incremental; "
        f"the ref updates below cost {len(entries)} GET(s) of wal/ objects)"
    )
    for pointer in index.entries:
        packs = pointer.pack[:26] + "..." if pointer.pack else "<no pack>"
        print(f"  seq={pointer.seq:<5} {packs} {human(pointer.pack_size):>8}")
        for update in entries[pointer.entry_id].ref_updates:
            old = (update.old or "-")[:8]
            new = (update.new or "deleted")[:8]
            print(f"        {update.ref}: {old} -> {new}")
    print("refs")
    for ref, oid in sorted(index.refs.items()):
        print(f"  {oid[:12]}  {ref}")
    return 0


def cmd_compact(args, settings: Settings) -> int:
    policy = CompactionPolicy(
        min_entries=args.min_entries,
        max_incremental_bytes=args.max_bytes,
        keep_recent=args.keep_recent,
    )
    result = compact(settings, args.repo, policy)
    if result is None:
        print("nothing to compact under the current policy")
        return 0
    print(
        f"compacted seqs {result.plan.entry_seqs[0]}..{result.plan.through_seq} "
        f"({human(result.plan.input_bytes)} in {len(result.plan.input_packs)} packs) "
        f"-> {result.new_pack[:26]}... {human(result.new_size)} "
        f"in {result.attempts} swap attempt(s)"
    )
    return 0


def cmd_gc(args, settings: Settings) -> int:
    result = gc(settings, args.repo, grace_seconds=args.grace)
    print(f"deleted {len(result.deleted_packs)} pack(s), {len(result.deleted_entries)} entry object(s)")
    print(f"retained {len(result.retained_by_grace)} object(s) inside the grace window")
    return 0


def cmd_sync(args, settings: Settings) -> int:
    repo = LocalRepo(settings, args.repo)
    result = repo.sync(force=args.force)
    if result.up_to_date_via_304:
        print("304 not modified -- cache already current")
    else:
        print(
            f"synced to seq={result.head_seq}; "
            f"downloaded {len(result.downloaded)} pack(s), pruned {len(result.pruned)}"
        )
    return 0


def cmd_drop_cache(args, settings: Settings) -> int:
    LocalRepo(settings, args.repo).destroy()
    print("local cache deleted; the repository still exists in S3")
    return 0


def cmd_destroy_repo(args, settings: Settings) -> int:
    """Delete a repository outright: index, WAL entries, packs, local cache."""
    client = s3mod.client(settings)
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=settings.bucket, Prefix=f"repos/{args.repo}/"):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.bucket, Delete={"Objects": keys})
            deleted += len(keys)
    LocalRepo(settings, args.repo).destroy()
    print(f"destroyed {args.repo}: {deleted} object(s) removed from S3, cache deleted")
    return 0


def cmd_materialize(args, settings: Settings) -> int:
    wal = WalLog(settings, args.repo)
    index, _ = wal.load()
    dest = materialize_snapshot(
        settings, index, index.head_seq, Path(args.dest), refs=index.refs
    )
    print(f"materialized {args.repo} at seq={index.head_seq} into {dest}")
    return 0


def cmd_serve(args, settings: Settings) -> int:  # pragma: no cover
    s3mod.ensure_bucket(settings)
    serve(settings, host=args.host, port=args.port)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="walgit")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the smart-HTTP git server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("show", help="pretty-print a repository's WAL index")
    p.add_argument("repo")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("compact", help="run one compaction pass")
    p.add_argument("repo")
    p.add_argument("--min-entries", type=int, default=8)
    p.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024)
    p.add_argument("--keep-recent", type=int, default=0)
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("gc", help="delete unreferenced packs and entry objects")
    p.add_argument("repo")
    p.add_argument("--grace", type=float, default=3600.0)
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("sync", help="refresh the local cache from the WAL")
    p.add_argument("repo")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("drop-cache", help="delete this node's local copy")
    p.add_argument("repo")
    p.set_defaults(func=cmd_drop_cache)

    p = sub.add_parser("destroy-repo", help="delete a repository from S3 entirely")
    p.add_argument("repo")
    p.set_defaults(func=cmd_destroy_repo)

    p = sub.add_parser("materialize", help="rebuild a repo from S3 into a directory")
    p.add_argument("repo")
    p.add_argument("dest")
    p.set_defaults(func=cmd_materialize)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    return args.func(args, settings)
