#!/usr/bin/env python3
"""The shared on-disk image model, imported by both `build_image.py` and `lazyfs.py`.

An "image" is two things:
  - a content-addressed `store/<hash>` of raw chunk blobs (written by build_image.py)
  - an `image.json` index: `{"chunk_size": N, "nodes": {<path>: <node>}}`

A node is one of two shapes. We model them as a *sum type* (two dataclasses, not one
class with optional fields) so that an impossible state -- e.g. a directory that carries
`chunks`, or a file that carries `children` -- is simply unconstructable. The `type`
field is a constant discriminant that survives JSON and lets us pick the right class
back on load (see `node_from_dict`).
"""
from dataclasses import dataclass
from typing import Literal


@dataclass
class DirNode:
    mode: int                       # st_mode (perms + type bits)
    mtime: float                    # modification time, float seconds
    children: list[str]             # child *names* (not full paths), sorted
    type: Literal["dir"] = "dir"    # discriminant; constant, but serialized so we can dispatch on load


@dataclass
class FileNode:
    mode: int
    mtime: float
    size: int                       # logical file size in bytes
    chunks: list[str]               # ordered list of chunk hashes; order *is* the file
    type: Literal["file"] = "file"


# A node is exactly one of these two -- nothing else.
Node = DirNode | FileNode


def node_from_dict(d: dict) -> Node:
    """Rebuild the right node class from a plain dict (as produced by json.loads).

    This is the deserialization half of the boundary: `asdict(node)` flattens a
    dataclass to a dict on the way out; this dispatches on the `type` discriminant
    to rebuild it on the way in.
    """
    if d["type"] == "dir":
        return DirNode(**d)
    return FileNode(**d)
