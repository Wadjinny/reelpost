"""Serve one JPEG through a short-lived Cloudflare tunnel.

Instagram downloads cover_url from its own servers, so a file on this machine
is not enough. The video itself is uploaded straight to Meta and does not go
through the tunnel.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse

from reelpost.errors import ReelpostError

_TUNNEL_URL = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")


def find_trycloudflare_url(text: str) -> str | None:
    match = _TUNNEL_URL.search(text)
    return match.group(0) if match else None


def make_cover_handler(path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self._serve(write_body=True)

        def do_HEAD(self):  # noqa: N802
            self._serve(write_body=False)

        def _serve(self, write_body: bool) -> None:
            if urlparse(self.path).path != "/cover.jpg":
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if write_body:
                self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def start_cover_server(path: Path) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cover_handler(path))
    thread = threading.Thread(target=server.serve_forever, name="reelpost-cover", daemon=True)
    thread.start()
    return server


@contextmanager
def public_cover(path: Path, *, timeout: float = 45):
    server = start_cover_server(path)
    process = None
    try:
        local = f"http://127.0.0.1:{server.server_address[1]}"
        process, base = start_quick_tunnel(local, timeout=timeout)
        yield base.rstrip("/") + "/cover.jpg"
    finally:
        if process is not None:
            stop_process(process)
        server.shutdown()
        server.server_close()


def start_quick_tunnel(local_url: str, *, timeout: float = 45):
    binary = shutil.which("cloudflared")
    if not binary:
        raise ReelpostError(
            "Instagram has to download the cover from a public URL, and cloudflared was not found. "
            "Install cloudflared, or pass --thumbnail-url with a public JPEG."
        )
    process = subprocess.Popen(
        [binary, "tunnel", "--url", local_url, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    try:
        url = read_tunnel_url(process, timeout)
    except Exception:
        stop_process(process)
        raise
    return process, url


def read_tunnel_url(process: subprocess.Popen, timeout: float) -> str:
    lines: Queue[str | None] = Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, name="reelpost-cloudflared", daemon=True).start()
    collected: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            line = lines.get(timeout=max(0.1, min(0.5, remaining)))
        except Empty:
            if process.poll() is not None and lines.empty():
                break
            continue
        if line is None:
            break
        collected.append(line)
        found = find_trycloudflare_url(line)
        if found:
            return found
    text = "".join(collected)
    found = find_trycloudflare_url(text)
    if found:
        return found
    tail = text[-1000:].strip() or "no output"
    raise ReelpostError(
        "Could not open a temporary public URL for the Instagram cover. "
        "Pass --thumbnail-url with a JPEG Meta can download. "
        f"cloudflared said: {tail}"
    )


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)
