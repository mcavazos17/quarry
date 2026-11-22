"""Durable album acquisition for requests accepted by Quarry's private music API."""

import contextlib
import json
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

try:
    from .worker_sources import AUDIO, CATEGORY, MusicSources, Review, check, manifest, normalized
    from .worker_import import publish
except ImportError:
    from worker_sources import AUDIO, CATEGORY, MusicSources, Review, check, manifest, normalized
    from worker_import import publish

__all__ = ["MusicSources", "MusicWorker"]


def checked_id(key):
    check(isinstance(key, str) and str(uuid.UUID(key)) == key, "invalid_release_group")
    return key


class MusicWorker:
    def __init__(self, state, downloads, library, sources):
        self.state, self.downloads, self.library = map(Path, (state, downloads, library))
        self.sources = sources
        for p in (self.state, self.downloads, self.library):
            check(p.is_absolute() and p.is_dir() and p.resolve(strict=True) == p, "invalid_mount")
        check(
            all(
                a != b and a not in b.parents
                for a in (self.state, self.downloads, self.library)
                for b in (self.state, self.downloads, self.library)
                if a is not b
            ),
            "overlapping_mounts",
        )
        self.path = self.state / "music.sqlite"
        check(
            not any(
                Path(str(self.path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")
            ),
            "invalid_state",
        )
        self.lock = threading.Lock()
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS albums (
              id TEXT PRIMARY KEY, artist TEXT NOT NULL, album TEXT NOT NULL, tracks TEXT NOT NULL,
              status TEXT NOT NULL, reason TEXT, hash TEXT UNIQUE, manifest TEXT, files TEXT,
              sent REAL, next REAL NOT NULL DEFAULT 0)""")
            # Older workers treated an unfamiliar successful add response as terminal, even
            # though the transfer had been registered. Resume observation of that same hash.
            db.execute("""UPDATE albums SET status='downloading', reason=NULL, next=0
                          WHERE status='review' AND reason='download_not_acknowledged'
                          AND length(hash)=40 AND hash NOT GLOB '*[^0-9a-f]*'
                          AND manifest IS NOT NULL AND sent IS NOT NULL AND files IS NULL""")

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def submit(self, key, artist, album, tracks=None):
        checked_id(key)
        check(
            all(
                isinstance(s, str) and 0 < len(s) <= 500 and s.isprintable() and normalized(s)
                for s in (artist, album)
            ),
            "invalid_album",
        )
        tracks = [] if tracks is None else tracks
        check(
            isinstance(tracks, (list, tuple))
            and len(tracks) <= 512
            and all(isinstance(s, str) and 0 < len(s) <= 500 and normalized(s) for s in tracks),
            "invalid_tracklist",
        )
        with self.db() as db:
            db.execute(
                "INSERT INTO albums(id,artist,album,tracks,status,reason) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (
                    key,
                    artist,
                    album,
                    json.dumps(tracks),
                    "searching" if tracks else "review",
                    None if tracks else "missing_tracklist",
                ),
            )
        return self.status(key)

    def status(self, key):
        checked_id(key)
        with self.db() as db:
            row = db.execute("SELECT status,reason,files FROM albums WHERE id=?", (key,)).fetchone()
        result = {"id": key, "status": row["status"] if row else "unknown"}
        if row and row["files"]:
            result["files"] = json.loads(row["files"])
        if row and row["reason"]:
            result["reason"] = row["reason"]
        return result

    def matched(self, entry, h, key):
        return (
            entry.get("hash", "").lower() == h
            and entry.get("category") == CATEGORY
            and (entry.get("save_path") or "").rstrip("/") == self.sources.save + "/" + key
        )

    def acquire(self, row):
        key = row["id"]
        count = len(json.loads(row["tracks"]))
        rejected = None
        for raw, size in self.sources.candidates(row["artist"], row["album"]):
            try:
                h, spec = manifest(raw, size)
                check(
                    sum(Path(p).suffix.lower() in AUDIO for p, _ in spec["entries"]) == count,
                    "edition_file_count_mismatch",
                )
                break
            except Review as error:
                rejected = error
            except (ValueError, TypeError, KeyError):
                rejected = Review("invalid_metainfo")
        else:
            if rejected:
                raise rejected
            self.defer(key, 600)
            return
        with self.sources.transfer_client() as call:
            existing = json.loads(call("torrents/info?hashes=" + h))
            check(
                isinstance(existing, list)
                and (not existing or len(existing) == 1 and self.matched(existing[0], h, key)),
                "transfer_already_owned",
            )
            # Record exact identity and ownership before crossing the acquisition boundary. Only
            # this successful state transition may submit. Later attempts observe the registered hash.
            with self.db() as db:
                claimed = db.execute(
                    "UPDATE albums SET status='downloading', hash=?,manifest=?,sent=?,next=0 WHERE id=? AND status='searching'",
                    (h, json.dumps(spec), time.time(), key),
                ).rowcount
            if claimed and not existing:
                self.sources.add(call, key, raw)

    def defer(self, key, seconds):
        with self.db() as db:
            db.execute(
                "UPDATE albums SET next=? WHERE id=? AND status IN ('searching','downloading')",
                (time.time() + seconds, key),
            )

    def observe(self, row):
        key, h = row["id"], row["hash"]
        with self.sources.transfer_client() as call:
            entries = json.loads(call("torrents/info?hashes=" + h))
            if entries == []:
                check(time.time() - row["sent"] < 1800, "transfer_unconfirmed")
                return
            check(
                isinstance(entries, list) and len(entries) == 1 and self.matched(entries[0], h, key),
                "transfer_identity_changed",
            )
            transfer = entries[0]
            check(transfer.get("state") not in ("error", "missingFiles"), "downloader_error")
            if (
                transfer.get("state")
                not in {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}
                or transfer.get("progress") != 1
                or transfer.get("amount_left") != 0
            ):
                return
            spec = json.loads(row["manifest"])
            actual = json.loads(call("torrents/files?hash=" + h))
            check(
                isinstance(actual, list)
                and all(f.get("progress") == 1 for f in actual)
                and sorted((f["name"], f["size"]) for f in actual)
                == sorted((p, s) for p, s in spec["entries"]),
                "transfer_file_list_changed",
            )
        result = publish(
            key, spec, row["artist"], row["album"], json.loads(row["tracks"]), self.downloads, self.library
        )
        with self.db() as db:
            db.execute(
                "UPDATE albums SET status='complete',reason=NULL,files=?,manifest=NULL WHERE id=? AND status='downloading'",
                (json.dumps(result), key),
            )

    def once(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.db() as db:
                rows = db.execute(
                    "SELECT * FROM albums WHERE status IN ('searching','downloading') AND next<=? ORDER BY next, rowid LIMIT 20",
                    (time.time(),),
                ).fetchall()
            for row in rows:
                try:
                    if row["status"] == "searching":
                        self.acquire(row)
                    else:
                        self.observe(row)
                        self.defer(row["id"], 1)
                except json.JSONDecodeError:
                    self.defer(row["id"], 30)
                except (Review, KeyError, ValueError, TypeError, sqlite3.IntegrityError) as error:
                    with self.db() as db:
                        reason = str(error) if isinstance(error, Review) else "unexpected_provider_data"
                        db.execute(
                            "UPDATE albums SET status='review',reason=? WHERE id=? AND status=?",
                            (reason, row["id"], row["status"]),
                        )
                except subprocess.TimeoutExpired:
                    with self.db() as db:
                        db.execute(
                            "UPDATE albums SET status='review',reason='audio_probe_timeout' WHERE id=? AND status='downloading'",
                            (row["id"],),
                        )
                except OSError:
                    # Network and storage trouble is retryable. A recorded add is observed, never resent.
                    self.defer(row["id"], 30)
        finally:
            self.lock.release()
