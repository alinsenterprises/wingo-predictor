"""A tiny HTTP status endpoint for a headless daemon.

The analyser has no pages to serve - it scrapes, analyses and pushes to
Telegram - but every runtime (Freebuff preview, systemd, Lightsail) wants one
port to health-check. ``GET /`` answers with exactly the snapshot the Telegram
``/status`` command uses, which never contains credentials.

The server runs in a daemon thread; if the port is busy the caller logs a
warning and the analyser keeps running without it.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

Snapshot = Callable[[], dict[str, Any]]


def _make_handler(snapshot: Snapshot):
    class StatusHandler(BaseHTTPRequestHandler):
        server_version = "wingo-analyzer"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/healthz", "/status"):
                self._send(404, {"error": "not found"})
                return
            try:
                payload: dict[str, Any] = {"status": "ok", **snapshot()}
            except Exception as exc:  # a probe must never take the daemon down
                log.warning("status snapshot failed: %s", exc)
                payload = {"status": "degraded", "error": str(exc)}
            self._send(200, payload)

        def do_HEAD(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            self._send(200 if path in ("/", "/healthz", "/status") else 404, {})

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2, default=str).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client hung up; irrelevant for a status probe

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("status-http %s", fmt % args)

    return StatusHandler


class StatusServer:
    """Serve ``snapshot()`` as JSON on ``host:port`` from a daemon thread.

    Binding happens in :meth:`start`, so a busy port surfaces as ``OSError``
    to the caller instead of breaking construction.
    """

    def __init__(self, snapshot: Snapshot, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._snapshot = snapshot
        self._host = host
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.host, self.port = host, port

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(
            (self._host, self._port), _make_handler(self._snapshot)
        )
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="status-http", daemon=True
        )
        self._thread.start()
        log.info("status endpoint listening on http://%s:%d/", self.host, self.port)

    def stop(self) -> None:
        if self._httpd is None or self._thread is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread = None
