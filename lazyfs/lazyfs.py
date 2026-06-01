#!/usr/bin/env python3
"""A lazy-loading, content-addressed FUSE filesystem.

The whole point: we mount an `image.json` index *instantly* (it has no file bytes),
and only fetch a file's content from the content-addressed store when a process
actually `read()`s it -- chunk by chunk, cached in memory after first touch.

FUSE in one paragraph: the kernel exposes a mountpoint, and every filesystem syscall
on it (`stat`, `readdir`, `open`, `read`, ...) is forwarded to us in userspace as a
method call. fusepy turns those into the methods below. The kernel always hands us
absolute paths starting with "/", which is why the index keys are stored that way.

Usage:
  python lazyfs.py <image.json> <store-dir> <mountpoint>   # mounts in the foreground
  fusermount -u <mountpoint>                                # unmount
"""
import argparse
import errno
import json
import logging
import os
import threading
from pathlib import Path

from fuse import FUSE, FuseOSError, Operations

from image import DirNode, FileNode, Node, node_from_dict

logger = logging.getLogger("lazyfs")


class LazyImageFS(Operations):
    # We hand the kernel plain (float seconds) timestamps, not nanosecond ints.
    use_ns = False

    def __init__(self, image_path: str, store_path: str):
        image = json.loads(Path(image_path).read_text())
        self.chunk_size: int = image["chunk_size"]
        # Rebuild typed nodes from the plain dicts json.loads gives us; from here on we
        # work with DirNode/FileNode rather than raw dicts.
        self.nodes: dict[str, Node] = {
            path: node_from_dict(d) for path, d in image["nodes"].items()
        }
        self.store = Path(store_path)

        # Our in-memory "page cache": chunk hash -> bytes. A chunk is fetched from the
        # store at most once; after that every read of it is a hit.
        self.chunk_cache: dict[str, bytes] = {}
        # NB: do NOT name this `self.lock` -- `lock` is itself a FUSE operation name, so
        # an instance attribute would shadow the method and the kernel's lock op would
        # try to "call" the mutex. Hence `_cache_lock`. It guards the cache + counters,
        # and matters if you serve FUSE with multiple threads (single-threaded here).
        self._cache_lock = threading.Lock()

        # Counters powering the [fetch]/[hit] instrumentation logged below.
        self.n_fetches = 0
        self.bytes_fetched = 0
        self.n_cache_hits = 0

        index_kib = Path(image_path).stat().st_size / 1024
        logger.info(
            f"[mount] index loaded: {len(self.nodes)} nodes, {index_kib:.1f} KiB, "
            f"0 chunks fetched (every file is browsable, none is downloaded yet)"
        )

    def _node(self, path: str) -> Node:
        """Look up a node by its absolute in-image path, or raise ENOENT."""
        node = self.nodes.get(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        return node

    # ---- metadata: served entirely from the index, never touches the store ----

    def getattr(self, path: str, fh=None) -> dict:
        """Return a stat(2)-like dict for `path`. Called constantly (every ls, every open)."""
        node = self._node(path)
        if isinstance(node, DirNode):
            return {
                "st_mode": node.mode,
                "st_nlink": 2,
                "st_size": 0,
                "st_atime": node.mtime,
                "st_mtime": node.mtime,
                "st_ctime": node.mtime,
                "st_uid": os.getuid(),
                "st_gid": os.getgid(),
            }

        return {
            "st_mode": node.mode,
            "st_nlink": 1,
            "st_size": node.size,
            "st_atime": node.mtime,
            "st_mtime": node.mtime,
            "st_ctime": node.mtime,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
        }

    # We support no extended attributes; returning None tells fusepy to short-circuit
    # the getxattr/listxattr ops instead of round-tripping to us.
    getxattr = None
    listxattr = None

    def readdir(self, path: str, fh: int) -> list[str]:
        """List a directory's entries. Powers `ls`."""
        node = self._node(path)
        if not isinstance(node, DirNode):
            raise FuseOSError(errno.ENOTDIR)

        return [".", "..", *node.children]

    def open(self, path: str, flags: int) -> int:
        """Called when something opens a file. We only allow reading."""
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise FuseOSError(errno.EROFS)

        self._node(path)
        return 0

    # ---- data: this is where laziness lives ----------------------------------

    def _log_chunk_access(self, tag: str, h: str, nbytes: int) -> None:
        total = self.n_fetches + self.n_cache_hits
        hit_pct = self.n_cache_hits / total * 100 if total else 0.0
        logger.info(
            f"[{tag}] {h[:12]} ({nbytes} B)  "
            f"cumulative fetched={self.bytes_fetched / 1024:.0f} KiB  "
            f"hits={self.n_cache_hits} ({hit_pct:.1f}%)"
        )

    def _get_chunk(self, h: str) -> bytes:
        """Return a chunk's bytes by its hash, fetching from the store on a cache miss."""
        with self._cache_lock:
            cached = self.chunk_cache.get(h)
            if cached is not None:
                self.n_cache_hits += 1
                self._log_chunk_access("hit", h, len(cached))
                return cached

        # Cache miss: this is the only place that touches the store -- the "remote" fetch.
        data = (self.store / h).read_bytes()
        with self._cache_lock:
            self.chunk_cache[h] = data
            self.n_fetches += 1
            self.bytes_fetched += len(data)
        self._log_chunk_access("fetch", h, len(data))
        return data

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """Return `size` bytes at `offset` from `path`, fetching only the chunks it touches.

        This is the payoff: a read of bytes [offset, offset+size) maps to a *range of
        chunks*, and we fetch only those -- not the whole file.
        """
        node = self._node(path)
        if not isinstance(node, FileNode):
            raise FuseOSError(errno.EISDIR)

        pos = offset
        out = bytearray()
        end = min(offset + size, node.size)
        while pos < end:
            idx = pos // self.chunk_size
            chunk = self._get_chunk(node.chunks[idx])
            cstart = pos - idx * self.chunk_size
            cend = min(len(chunk), cstart + end - pos)
            out.extend(chunk[cstart:cend])
            pos += cend - cstart

        return bytes(out)

    def statfs(self, path: str) -> dict:
        """Filesystem-wide stats (used by `df`). Plausible constants are fine here."""
        return {
            "f_bsize": self.chunk_size,
            "f_frsize": self.chunk_size,
            "f_blocks": 1 << 20,
            "f_bfree": 0,
            "f_bavail": 0,
            "f_namemax": 255,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", help="path to image.json")
    ap.add_argument("store", help="path to the content-addressed store/ dir")
    ap.add_argument("mountpoint")
    ap.add_argument("--debug", action="store_true", help="verbose logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    fs = LazyImageFS(args.image, args.store)
    # foreground=True keeps the server in this process (Ctrl-C / fusermount -u to stop);
    # nothreads keeps the [fetch] log ordered and readable; ro marks the mount read-only.
    FUSE(fs, args.mountpoint, foreground=True, nothreads=True, ro=True)


if __name__ == "__main__":
    main()
