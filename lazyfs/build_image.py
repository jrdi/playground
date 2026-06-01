#!/usr/bin/env python3
"""Build a content-addressed "image" from a directory.  [MILESTONE 1]

This mimics how a lazy-loading container image is packaged. We split every file
into fixed-size chunks, hash each chunk, and store the chunks as content-addressed
blobs. The output is:

  <out>/image.json     the metadata index: the directory tree plus, per file, the
                       ordered list of chunk hashes. Crucially this holds NO file
                       bytes, so it is tiny and loads instantly.
  <out>/store/<hash>   the chunk blobs, one per unique chunk. Identical chunks
                       (even across different files) are stored exactly once.

Content-addressing is what gives us dedup for free: the address *is* the hash of
the bytes, so the same content can only ever land in one place.

Usage:
  python build_image.py <source-dir> <out-dir>     # writes <out-dir>/image.json + store/
"""
import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from image import DirNode, FileNode, Node

DEFAULT_CHUNK_SIZE = 64 * 1024  # 64 KiB


def build(source: Path, out: Path, chunk_size: int) -> None:
    store = out / "store"
    store.mkdir(parents=True, exist_ok=True)

    # The index we are building: maps an absolute-in-image path (e.g. "/sub/a.txt")
    # to a node describing that file or directory.
    nodes: dict[str, Node] = {}
    # Hashes we have already written to the store, so we never write a blob twice.
    seen: set[str] = set()
    stats = {
        "files": 0,
        "dirs": 0,
        "logical_bytes": 0,  # total bytes across all files
        "stored_bytes": 0,   # bytes actually written to the store (post-dedup)
        "chunks_total": 0,   # chunk references across all files
        "unique_blobs": 0,   # distinct chunks stored
    }

    def add_node(path: Path, virtual_path: str) -> None:
        """Recursively add `path` (whose path *inside the image* is `virtual_path`) to the index."""
        st = path.lstat()

        if path.is_dir():
            stats["dirs"] += 1
            children = sorted(path.iterdir(), key=lambda p: p.name)
            nodes[virtual_path] = DirNode(
                mode=st.st_mode,
                mtime=st.st_mtime,
                children=[child.name for child in children],
            )
            for child in children:
                child_virtual_path = virtual_path.rstrip("/") + "/" + child.name
                add_node(child, child_virtual_path)
            return

        stats["files"] += 1
        stats["logical_bytes"] += st.st_size
        chunk_hashes: list[str] = []
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h = hashlib.sha256(chunk).hexdigest()
                chunk_hashes.append(h)
                stats["chunks_total"] += 1
                if h not in seen:
                    seen.add(h)
                    stats["unique_blobs"] += 1
                    stats["stored_bytes"] += len(chunk)
                    (store / h).write_bytes(chunk)

        nodes[virtual_path] = FileNode(
            mode=st.st_mode,
            mtime=st.st_mtime,
            size=st.st_size,
            chunks=chunk_hashes,
        )

    add_node(source, "/")

    # Flatten the dataclass nodes back to plain dicts for JSON. `image.py` rebuilds
    # them with `node_from_dict` on the read side.
    image = {
        "chunk_size": chunk_size,
        "nodes": {path: asdict(node) for path, node in nodes.items()},
    }
    index_path = out / "image.json"
    index_path.write_text(json.dumps(image, indent=2))

    index_kib = index_path.stat().st_size / 1024
    logical_mib = stats["logical_bytes"] / 1024 / 1024
    stored_mib = stats["stored_bytes"] / 1024 / 1024
    dedup = (stats["logical_bytes"] / stats["stored_bytes"]) if stats["stored_bytes"] else 1.0

    print(f"Built image from {source}")
    print(f"  files / dirs        : {stats['files']} / {stats['dirs']}")
    print(f"  logical size        : {logical_mib:.2f} MiB")
    print(f"  chunk refs / unique : {stats['chunks_total']} / {stats['unique_blobs']}")
    print(f"  store size on disk  : {stored_mib:.2f} MiB  (dedup {dedup:.2f}x)")
    print(f"  index size          : {index_kib:.1f} KiB  <- this is all that mounts")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="directory to package")
    ap.add_argument("out", type=Path, help="output dir for image.json + store/")
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    args = ap.parse_args()
    build(args.source.resolve(), args.out.resolve(), args.chunk_size)


if __name__ == "__main__":
    main()
