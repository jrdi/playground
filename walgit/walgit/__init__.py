"""walgit -- a git server whose source of truth is a write-ahead log in S3."""

from .config import Settings

__all__ = ["Settings"]
