"""Private audio request service for Quarry. Cadence owns listener authentication."""

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
    def __init__(self, jobs, catalog, key, books=None):
        if not 16 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("Invalid audio API key file")
        self.key = key.encode()
        self.jobs = jobs
        self.catalog = catalog
        self.books = books

    def route(self, method, target):
        url = urllib.parse.urlsplit(target)
        if url.netloc or url.scheme or url.fragment:
            return 400, {}
        if method not in ("GET", "POST"):
            return 405, {}
        searches = {"/api/music/search": self.catalog}
        if self.books:
            searches["/api/books/search"] = self.books
        if method == "GET" and url.path in searches:
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
            return 200, searches[url.path].search(query, offset)
        if (
            self.books
            and not url.query
            and (book := re.fullmatch(r"/api/books/requests/([0-9a-f]{64})", url.path))
        ):
            return 200, self.books.status(book[1]) if method == "GET" else self.books.submit(book[1])
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
    secondary = None
    if "QUARRY_TL_INDEXER_ID" in env:
        from tracker_admission import Admission
        check_id = int(env["QUARRY_TL_INDEXER_ID"])
        if env["QUARRY_TL_QBIT_URL"].rstrip("/") == env["QBIT_URL"].rstrip("/"):
            raise ValueError("TorrentLeech requires a dedicated qBittorrent client")
        if env["QUARRY_TL_PROWLARR_URL"].rstrip("/") == env["PROWLARR_URL"].rstrip("/"):
            raise ValueError("TorrentLeech requires an isolated indexer endpoint")
        admission = Admission(env["QUARRY_AUDIO_STATE"], env["QUARRY_TL_ACCOUNT_FILE"])
        tl_connection = (env["QUARRY_TL_PROWLARR_URL"], check_id, env["QUARRY_TL_QBIT_URL"])
        tl_keys = (Path(env["QUARRY_TL_PROWLARR_KEY_FILE"]), Path(env["QUARRY_TL_QBIT_AUTH_FILE"]))
        tl_downloads = Path(env["QUARRY_TL_DOWNLOADS"])
        if not tl_downloads.is_absolute() or tl_downloads.resolve(strict=True) != tl_downloads:
            raise ValueError("Invalid TorrentLeech download mount")
        secondary = (MusicSources(*tl_connection, "/downloads/quarry-music", *tl_keys), tl_downloads / "quarry-music", admission)
    sources = MusicSources(
        env["PROWLARR_URL"],
        int(env["QUARRY_MUSIC_INDEXER_ID"]),
        env["QBIT_URL"],
        env["QUARRY_MUSIC_SAVE_PATH"],
        Path(env["QUARRY_PROWLARR_KEY_FILE"]),
        Path(env["QUARRY_QBIT_AUTH_FILE"]),
        additional_indexers=tuple(int(i) for i in env.get("QUARRY_MUSIC_EXTRA_INDEXER_IDS", "").split(",") if i.strip()),
    )
    worker = MusicWorker(
        Path(env["QUARRY_AUDIO_STATE"]),
        Path(env["QUARRY_MUSIC_DOWNLOADS"]),
        Path(env["QUARRY_MUSIC_LIBRARY"]),
        sources,
        secondary=secondary,
    )
    catalog = Catalog(env.get("QUARRY_MUSICBRAINZ_URL", "https://musicbrainz.org"))
    jobs = AlbumRequests(Path(env["QUARRY_AUDIO_STATE"]), worker, catalog)
    books = None
    if any(name.startswith("QUARRY_BOOK_") for name in env):
        from books import BookSources, BookWorker

        books = BookWorker(
            Path(env["QUARRY_AUDIO_STATE"]),
            Path(env["QUARRY_BOOK_DOWNLOADS"]),
            Path(env["QUARRY_BOOK_LIBRARY"]),
            BookSources(
                env["PROWLARR_URL"],
                int(env["QUARRY_BOOK_INDEXER_ID"]),
                env["QBIT_URL"],
                env["QUARRY_BOOK_SAVE_PATH"],
                Path(env["QUARRY_PROWLARR_KEY_FILE"]),
                Path(env["QUARRY_QBIT_AUTH_FILE"]),
                general_categories=env.get("QUARRY_BOOK_GENERAL_CATEGORIES") == "true",
            ),
        )
        if secondary:
            from books import FederatedBooks
            book_state = Path(env["QUARRY_AUDIO_STATE"]) / "torrentleech-books"
            book_state.mkdir(mode=0o700, exist_ok=True)
            secondary_books = BookWorker(book_state, tl_downloads / "quarry-audiobooks", Path(env["QUARRY_BOOK_LIBRARY"]),
                BookSources(*tl_connection, "/downloads/quarry-audiobooks", *tl_keys, general_categories=True),
                admission=admission)
            books = FederatedBooks(books, secondary_books)
    api = AudioAPI(jobs, catalog, Path(env["QUARRY_AUDIO_KEY_FILE"]).read_text().strip(), books)
    address = env.get("QUARRY_AUDIO_BIND", "127.0.0.1"), int(env.get("QUARRY_AUDIO_PORT", "8088"))
    interval = max(1, min(3600, int(env.get("QUARRY_AUDIO_INTERVAL_SECONDS", "15"))))
    stopping = threading.Event()

    def advance(name, tasks):
        while not stopping.is_set():
            try:
                for task in tasks:
                    task.once()
            except Exception as exc:
                logging.warning("%s reconciliation deferred (%s)", name, type(exc).__name__)
            stopping.wait(interval)

    with ThreadingHTTPServer(address, handler(api)) as server:
        tasks = {"Music": (jobs, worker)}
        if books:
            tasks["Audiobook"] = (books,)
        runners = [
            threading.Thread(target=advance, args=(name, tasks[name]), daemon=True) for name in tasks
        ]
        for runner in runners:
            runner.start()
        try:
            server.serve_forever()
        finally:
            stopping.set()
            for runner in runners:
                runner.join(timeout=10)


if __name__ == "__main__":
    serve()
