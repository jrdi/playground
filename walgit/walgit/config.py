"""Runtime configuration, read from the environment.

Everything is overridable so the test-suite can point at a throwaway bucket and
the hooks (which run as a separate process inside git) can inherit the same
settings from their parent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_BUCKET = "walgit"
DEFAULT_ACCESS_KEY = "walgit"
DEFAULT_SECRET_KEY = "walgitwalgit"


@dataclass(frozen=True)
class Settings:
    endpoint_url: str = DEFAULT_ENDPOINT
    access_key: str = DEFAULT_ACCESS_KEY
    secret_key: str = DEFAULT_SECRET_KEY
    region: str = "us-east-1"
    bucket: str = DEFAULT_BUCKET
    cache_dir: Path = Path(".walgit-cache")
    auto_compact: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        return cls(
            endpoint_url=env.get("WALGIT_S3_ENDPOINT", DEFAULT_ENDPOINT),
            access_key=env.get("WALGIT_S3_ACCESS_KEY", DEFAULT_ACCESS_KEY),
            secret_key=env.get("WALGIT_S3_SECRET_KEY", DEFAULT_SECRET_KEY),
            region=env.get("WALGIT_S3_REGION", "us-east-1"),
            bucket=env.get("WALGIT_S3_BUCKET", DEFAULT_BUCKET),
            cache_dir=Path(env.get("WALGIT_CACHE_DIR", ".walgit-cache")).absolute(),
            auto_compact=env.get("WALGIT_AUTO_COMPACT", "") not in ("", "0", "false"),
        )

    def to_env(self) -> dict[str, str]:
        """Serialize back to env vars, so child processes (git hooks) agree with us."""
        return {
            "WALGIT_S3_ENDPOINT": self.endpoint_url,
            "WALGIT_S3_ACCESS_KEY": self.access_key,
            "WALGIT_S3_SECRET_KEY": self.secret_key,
            "WALGIT_S3_REGION": self.region,
            "WALGIT_S3_BUCKET": self.bucket,
            "WALGIT_CACHE_DIR": str(self.cache_dir),
            "WALGIT_AUTO_COMPACT": "1" if self.auto_compact else "0",
        }

    def with_bucket(self, bucket: str) -> "Settings":
        return replace(self, bucket=bucket)
