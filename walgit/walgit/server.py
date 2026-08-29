"""Smart-HTTP git server. Provided for you -- no exercises here.

This is deliberately thin: it delegates the entire git wire protocol to
``git http-backend`` (the same CGI program that runs behind Apache on a real
git host), and adds two things of its own:

* a ``sync()`` before every request, so the warm cache is verified against the
  WAL before we serve from it;
* for pushes, the repository's lock held across the whole of receive-pack, so
  no sync can move refs between ``pre-receive`` committing a push and git
  updating its refs (see ``LocalRepo.lock``). The price: on one node, fetches of
  a repository wait for an in-flight push to that repository to finish.

    GET  /<repo>/info/refs?service=git-upload-pack
    POST /<repo>/git-upload-pack       <- clone / fetch
    POST /<repo>/git-receive-pack      <- push, which fires the hooks

Repositories are created on first push; there is no separate "create repo" API.
"""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import Settings
from .repo import LocalRepo


class GitHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    settings: Settings
    quiet: bool = False

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._serve()

    def do_POST(self):  # noqa: N802
        self._serve()

    def log_message(self, fmt, *args):  # pragma: no cover
        if not self.quiet:
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ guts

    def _serve(self):
        parsed = urlparse(self.path)
        parts = parsed.path.lstrip("/").split("/", 1)
        repo_id = parts[0]
        rest = "/" + parts[1] if len(parts) > 1 else "/"
        if not repo_id or repo_id.startswith("."):
            self.send_error(404, "no repository in path")
            return

        # Read the body before taking any lock: a slow uploader must not stall
        # every other request for this repository while its bytes trickle in.
        body = self._read_body()
        repo = LocalRepo(self.settings, repo_id)
        is_push = self.command == "POST" and rest == "/git-receive-pack"

        with (repo.lock() if is_push else nullcontext()):
            try:
                repo.ensure_initialized()
                repo.sync()
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"sync failed: {exc}")
                return

            env = {
                **self.settings.to_env(),
                "PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
                "GIT_PROJECT_ROOT": str(self.settings.cache_dir),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": f"/{repo_id}.git{rest}",
                "QUERY_STRING": parsed.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_ADDR": self.client_address[0],
                "REMOTE_USER": self.headers.get("X-Walgit-User", "anonymous"),
                "GIT_COMMITTER_NAME": "walgit",
                "GIT_COMMITTER_EMAIL": "walgit@localhost",
            }
            # Protocol v2 is opt-in via this header; without forwarding it, clients
            # silently fall back to v0 and you lose ref filtering on fetch.
            if self.headers.get("Git-Protocol"):
                env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]

            # For a push, the pre-receive and post-receive hooks run inside this
            # call -- still under the lock, which is the whole point. The corollary:
            # a hook must never leave a child running that inherited this call's
            # output pipes. capture_output waits for EOF on them, so the call would
            # not return, the lock would not be released, and every sync or fetch
            # of this repository on this node would queue behind it.
            proc = subprocess.run(
                ["git", "http-backend"], input=body, capture_output=True, env=env
            )
        if proc.returncode != 0:
            self.send_error(500, f"http-backend failed: {proc.stderr.decode()[:400]}")
            return
        self._send_cgi_response(proc.stdout, proc.stderr)

    def _read_body(self) -> bytes:
        """Read the request body, de-chunking if necessary.

        git uses chunked transfer-encoding for pushes whose size it does not
        know up front. ``http-backend`` expects a plain body plus an accurate
        CONTENT_LENGTH -- in a normal deployment the web server de-chunks for
        it, so we have to do the same.
        """
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                size = int(size_line.split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_cgi_response(self, stdout: bytes, stderr: bytes) -> None:
        header_blob, _, body = stdout.partition(b"\r\n\r\n")
        if not _:
            header_blob, _, body = stdout.partition(b"\n\n")

        status = 200
        reason = "OK"
        headers = []
        for raw in header_blob.replace(b"\r\n", b"\n").split(b"\n"):
            if not raw.strip():
                continue
            name, _, value = raw.decode("latin-1").partition(":")
            value = value.strip()
            if name.lower() == "status":
                code, _, rest = value.partition(" ")
                status, reason = int(code), rest or "OK"
            else:
                headers.append((name, value))

        self.send_response(status, reason)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if stderr and not self.quiet:  # pragma: no cover
            import sys

            sys.stderr.write(stderr.decode(errors="replace"))


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8080, quiet: bool = False):
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    handler = type(
        "BoundGitHTTPHandler", (GitHTTPHandler,), {"settings": settings, "quiet": quiet}
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    httpd = make_server(settings, host, port)
    print(f"walgit serving on http://{host}:{httpd.server_address[1]}")
    print(f"  bucket={settings.bucket} endpoint={settings.endpoint_url}")
    print(f"  cache={settings.cache_dir}")
    print(f"  try: git clone http://{host}:{httpd.server_address[1]}/demo")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
