"""Private music request service for Quarry. Cadence owns listener authentication."""

import hmac
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from catalog import Catalog, identifier
from album_requests import AlbumRequests


class AudioAPI:
    def __init__(self, jobs, catalog, key):
        if not 16 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("Invalid audio API key file")
        self.key = key.encode()
        self.jobs = jobs
        self.catalog = catalog

    def route(self, method, target):
        url = urllib.parse.urlsplit(target)
        if url.netloc or url.scheme or url.fragment:
            return 400, {}
        if method == "GET" and url.path == "/api/music/search":
            try:
                args = urllib.parse.parse_qs(url.query, strict_parsing=True, max_num_fields=2)
                if set(args) - {"q", "offset"} or any(len(v) != 1 for v in args.values()):
                    return 400, {}
                query = args["q"][0].strip()
                offset = int(args.get("offset", ["0"])[0])
            except (ValueError, KeyError):
                return 400, {}
            if not 2 <= len(query) <= 200 or not query.isprintable() or offset not in range(0, 181, 20):
                return 400, {}
            return 200, self.catalog.search(query, offset)
        match = re.fullmatch(r"/api/music/requests/([0-9a-f-]{36})", url.path)
        key = identifier(match[1]) if match and not url.query else None
        if not key:
            return 404, {}
        return 200, self.jobs.status(key) if method == "GET" else self.jobs.submit(key)


def handler(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def reply(self, status, obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self):
            if self.command == "GET" and self.path == "/health":
                return self.reply(200, {"ok": True})
            key = self.headers.get("X-Api-Key", "")
            if not key.isascii() or not hmac.compare_digest(key.encode(), api.key):
                return self.reply(401, {})
            if self.command == "POST":
                if self.headers.get("Transfer-Encoding") or self.headers.get(
                    "Content-Length", "0"
                ) not in ("0", "2"):
                    return self.reply(400, {})
                if self.headers.get("Content-Length") == "2" and self.rfile.read(2) != b"{}":
                    return self.reply(400, {})
            try:
                status, value = api.route(self.command, self.path)
            except urllib.error.HTTPError as exc:
                status, value = (404 if exc.code == 404 else 503), {}
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                status, value = 503, {}
            self.reply(status, value)

        do_POST = do_GET = dispatch

    return Handler


def serve():
    from worker import MusicSources, MusicWorker

    env = os.environ
    sources = MusicSources(
        env["PROWLARR_URL"],
        int(env["QUARRY_MUSIC_INDEXER_ID"]),
        env["QBIT_URL"],
        env["QUARRY_MUSIC_SAVE_PATH"],
        Path(env["QUARRY_PROWLARR_KEY_FILE"]),
        Path(env["QUARRY_QBIT_AUTH_FILE"]),
    )
    worker = MusicWorker(
        Path(env["QUARRY_AUDIO_STATE"]),
        Path(env["QUARRY_MUSIC_DOWNLOADS"]),
        Path(env["QUARRY_MUSIC_LIBRARY"]),
        sources,
    )
    catalog = Catalog(env.get("QUARRY_MUSICBRAINZ_URL", "https://musicbrainz.org"))
    jobs = AlbumRequests(Path(env["QUARRY_AUDIO_STATE"]), worker, catalog)
    api = AudioAPI(jobs, catalog, Path(env["QUARRY_AUDIO_KEY_FILE"]).read_text().strip())
    address = env.get("QUARRY_AUDIO_BIND", "127.0.0.1"), int(env.get("QUARRY_AUDIO_PORT", "8088"))
    interval = max(1, min(3600, int(env.get("QUARRY_AUDIO_INTERVAL_SECONDS", "15"))))
    stopping = threading.Event()

    def advance():
        while not stopping.is_set():
            try:
                jobs.once()
                worker.once()
            except Exception as exc:
                logging.warning("Music reconciliation deferred (%s)", type(exc).__name__)
            stopping.wait(interval)

    with ThreadingHTTPServer(address, handler(api)) as server:
        runner = threading.Thread(target=advance, daemon=True)
        runner.start()
        try:
            server.serve_forever()
        finally:
            stopping.set()
            runner.join(timeout=10)


if __name__ == "__main__":
    serve()
