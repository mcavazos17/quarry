"""Durable admission while the release metadata service is unavailable."""

import contextlib
import sqlite3
import time
import urllib.error


class AlbumRequests:
    def __init__(self, directory, worker, catalog):
        self.path = directory / "catalog-requests.sqlite"
        self.worker = worker
        self.catalog = catalog
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pending(
                id TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try INTEGER NOT NULL DEFAULT 0)""")

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def status(self, key):
        result = self.worker.status(key)
        if result["status"] != "unknown":
            return result
        with self.connect() as db:
            row = db.execute("SELECT state FROM pending WHERE id=?", (key,)).fetchone()
        if row:
            return {"id": key, "status": row[0]}
        # A worker may have published its initial state between these two reads.
        return self.worker.status(key)

    def submit(self, key):
        result = self.status(key)
        if result["status"] != "unknown":
            return result
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM pending").fetchone()[0] >= 4096:
                raise RuntimeError("Music request storage at capacity")
            db.execute("INSERT OR IGNORE INTO pending(id,state) VALUES(?,'queued')", (key,))
        return self.status(key)

    def once(self):
        now = int(time.time())
        with self.connect() as db:
            row = db.execute(
                "SELECT id,attempts FROM pending WHERE state='queued' AND next_try<=? ORDER BY next_try,rowid LIMIT 1",
                (now,),
            ).fetchone()
        if not row:
            return
        key, n = row
        try:
            if self.worker.status(key)["status"] == "unknown":
                group = self.catalog.lookup(key)
                self.worker.submit(key, group["artist"], group["title"], self.catalog.tracklist(group))
            with self.connect() as db:
                db.execute("DELETE FROM pending WHERE id=?", (key,))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            missing = isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 404)
            wait = min(3600, 15 * 2 ** min(n, 8))
            with self.connect() as db:
                db.execute(
                    "UPDATE pending SET state=?,attempts=attempts+1,next_try=? WHERE id=?",
                    ("review" if missing else "queued", now + wait, key),
                )
