"""Audio editions selected in Cadence and acquired by Quarry's private audiobook category."""

import collections
import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

try:
    from .worker_sources import AudioSources, MAX_BYTES, Review, check, client, manifest, request, secret
    from .worker_import import publish_verified
except ImportError:
    from worker_sources import AudioSources, MAX_BYTES, Review, check, client, manifest, request, secret
    from worker_import import publish_verified


BOOK_AUDIO = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aiff", ".aif"}


def validate_key(key):
    check(isinstance(key, str) and re.fullmatch("[a-f0-9]{64}", key), "invalid_book_edition")


class BookSources(AudioSources):
    category = "quarry-audiobooks"
    torrent_filename = "audiobook.torrent"

    def page(self, query, page):
        params = urllib.parse.urlencode(
            dict(
                query=query,
                type="search",
                categories=3030,
                indexerIds=self.indexer,
                limit=20,
                offset=page * 20,
            )
        )
        headers = {"X-Api-Key": secret(self.prow_key_file)}
        results = json.loads(request(client(), self.prowlarr + "/api/v1/search?" + params, headers))
        check(isinstance(results, list) and len(results) <= 1000, "invalid_book_results")
        entries = []
        for row in results:
            # Preserve the source slot even when a malformed or out-of-category result is skipped.
            entries.append(None)
            if (
                not isinstance(row, dict)
                or row.get("indexerId") != self.indexer
                or row.get("protocol") != "torrent"
            ):
                continue
            if not any(isinstance(c, dict) and c.get("id") == 3030 for c in (row.get("categories") or [])):
                continue
            guid, title, size = row.get("guid"), row.get("title"), row.get("size")
            if (
                not isinstance(guid, str)
                or not 1 <= len(guid) <= 4096
                or not isinstance(title, str)
                or not 1 <= len(title) <= 500
                or not title.isprintable()
                or "://" in title
                or type(size) is not int
                or not 0 < size <= MAX_BYTES
            ):
                continue
            link = urllib.parse.urlsplit(row.get("downloadUrl") or "")
            if (
                link.scheme + "://" + link.netloc != self.prowlarr
                or link.path != "/" + str(self.indexer) + "/download"
                or link.fragment
            ):
                continue
            tokens = urllib.parse.parse_qs(link.query, max_num_fields=12).get("link", [])
            if len(tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,12000}", tokens[0]):
                continue
            identity = json.dumps(
                [self.indexer, guid, title, size], ensure_ascii=False, separators=(",", ":")
            ).encode()
            item = {"id": hashlib.sha256(identity).hexdigest(), "title": title, "sizeBytes": size}
            seeds = row.get("seeders")
            if type(seeds) is int and 0 <= seeds <= 2147483647:
                item["seeders"] = seeds
            date = row.get("publishDate")
            if isinstance(date, str):
                with contextlib.suppress(ValueError):
                    stamp = datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
                    if stamp.tzinfo:
                        item["publishedUtc"] = stamp.astimezone(datetime.timezone.utc).isoformat()
            entries[-1] = (item, tokens[0])
        return entries

    def torrent(self, token):
        check(
            isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,12000}", token), "invalid_book_locator"
        )
        path = f"/{self.indexer}/download?" + urllib.parse.urlencode(
            {"link": token, "file": "audiobook.torrent"}
        )
        return request(client(), self.prowlarr + path, {"X-Api-Key": secret(self.prow_key_file)})


def book_order(rows):
    def natural(text):
        return tuple((1, int(v)) if v.isdigit() else (0, v.casefold()) for v in re.split(r"([0-9]+)", text))

    ordered = sorted(rows, key=lambda r: (natural(r[0]), r[0]))
    for number, (name, path, digest, size) in enumerate(ordered, 1):
        # M4B can contain a QuickTime text track for chapter markers in addition to spoken audio.
        # Only inspect the staged file; the published bytes, bookmarks and chapter timing are unchanged.
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,mp3,flac,ogg,wav,aiff",
                "-show_entries",
                "stream=codec_type,codec_tag_string:stream_disposition=attached_pic:format=duration:chapter=start_time",
                "-of",
                "json",
                "-i",
                str(path),
            ],
            capture_output=True,
            timeout=30,
        )
        check(result.returncode == 0 and len(result.stdout) <= 1024 * 1024, "unsupported_audiobook_audio")
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        check(any(s.get("codec_type") == "audio" for s in streams), "missing_spoken_audio")
        for s in streams:
            if s.get("codec_type") == "audio" or s.get("disposition", {}).get("attached_pic") == 1:
                continue
            check(
                path.suffix in (".m4b", ".m4a")
                and data.get("chapters")
                and s.get("codec_type") in ("data", "subtitle")
                and s.get("codec_tag_string") in ("text", "tx3g"),
                "unexpected_book_stream",
            )
        yield f"{number:03d}-{Path(name).name}", path, digest, size


class BookWorker:
    def __init__(self, state, downloads, library, sources):
        self.state, self.downloads, self.library = map(Path, (state, downloads, library))
        roots = [self.state, self.downloads, self.library]
        check(
            all(p.is_absolute() and p.is_dir() and p.resolve(strict=True) == p for p in roots),
            "invalid_book_mount",
        )
        check(
            len(set(roots)) == 3 and not any(a in b.parents for a in roots for b in roots),
            "overlapping_book_mounts",
        )
        self.sources = sources
        self.path = self.state / "audiobooks.sqlite"
        check(
            not any(
                Path(str(self.path) + suffix).is_symlink() for suffix in ("", "-journal", "-shm", "-wal")
            ),
            "unsafe_book_state",
        )
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.pages = collections.OrderedDict()
        self.get_pages = threading.Lock()
        self.work = threading.Lock()
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS editions (id TEXT PRIMARY KEY,title TEXT NOT NULL,size INTEGER NOT NULL,token TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS transfers (id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT,hash TEXT UNIQUE,spec TEXT,files TEXT,sent REAL,next REAL NOT NULL DEFAULT 0);
            """)

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=12)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def search(self, q, offset):
        check(
            isinstance(q, str)
            and 2 <= len(q.strip()) <= 200
            and q.isprintable()
            and type(offset) is int
            and 0 <= offset <= 180
            and offset % 20 == 0,
            "invalid_book_search",
        )
        q = q.strip()
        with self.get_pages:
            view = self.pages.get(q)
            if view is None or time.monotonic() >= view[0]:
                first = self.sources.page(q, 0)
                # IPTorrents maps limit/offset to *tracker page numbers*. A tracker page can
                # contain more than 20 rows. Slice source slots across its observed page width.
                view = (time.monotonic() + 180, max(20, len(first)), {0: first})
                self.pages[q] = view
            self.pages.move_to_end(q)
            while len(self.pages) > 12:
                self.pages.popitem(last=False)
            _, width, pages = view
            page, slot = divmod(offset, width)
            selected = []
            while len(selected) < 20:
                if page not in pages:
                    pages[page] = self.sources.page(q, page)
                rows = pages[page]
                selected.extend(rows[slot : slot + 20 - len(selected)])
                if len(rows) < width:
                    break
                page += 1
                slot = 0
        now = time.time()
        items = {}
        with self.db() as db:
            db.execute(
                "DELETE FROM editions WHERE expires<? AND id NOT IN (SELECT id FROM transfers WHERE status='queued')",
                (now - 86400,),
            )
            for entry in selected:
                if entry is None:
                    continue
                item, token = entry
                key = item["id"]
                items[key] = item
                db.execute(
                    """INSERT INTO editions(id,title,size,token,expires) VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET token=excluded.token, expires=excluded.expires
                    WHERE editions.title=excluded.title AND editions.size=excluded.size""",
                    (key, item["title"], item["sizeBytes"], token, now + 1200),
                )
        return {
            "items": list(items.values()),
            "nextOffset": offset + 20 if len(selected) == 20 and offset < 180 else None,
        }

    def submit(self, key):
        validate_key(key)
        with self.db() as db:
            row = db.execute("SELECT expires FROM editions WHERE id=?", (key,)).fetchone()
            if row:
                expired = row["expires"] < time.time()
                db.execute(
                    "INSERT INTO transfers(id,status,reason) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
                    (key, "review" if expired else "queued", "edition_expired" if expired else None),
                )
        return self.status(key)

    def status(self, key):
        validate_key(key)
        with self.db() as db:
            row = db.execute("SELECT status,reason,files FROM transfers WHERE id=?", (key,)).fetchone()
        result = {"id": key, "status": row["status"] if row else "unknown"}
        if row and row["reason"]:
            result["reason"] = row["reason"]
        if row and row["files"]:
            result["files"] = json.loads(row["files"])
        return result

    def matches(self, entry, h, key):
        return (
            entry.get("hash", "").lower() == h
            and entry.get("category") == self.sources.category
            and (entry.get("save_path") or "").rstrip("/") == self.sources.save + "/" + key
        )

    def acquire(self, row):
        key = row["id"]
        with self.db() as db:
            edition = db.execute("SELECT size,token FROM editions WHERE id=?", (key,)).fetchone()
        check(edition is not None, "edition_missing")
        raw = self.sources.torrent(edition["token"])
        h, spec = manifest(raw, edition["size"], audio_formats=BOOK_AUDIO)
        with self.sources.transfer_client() as call:
            existing = json.loads(call("torrents/info?hashes=" + h))
            check(
                isinstance(existing, list)
                and (not existing or len(existing) == 1 and self.matches(existing[0], h, key)),
                "book_transfer_already_owned",
            )
            with self.db() as db:
                claimed = db.execute(
                    "UPDATE transfers SET status='downloading',hash=?,spec=?,sent=?,next=0 WHERE id=? AND status='queued'",
                    (h, json.dumps(spec), time.time(), key),
                ).rowcount
                if claimed:
                    db.execute("DELETE FROM editions WHERE id=?", (key,))
            if claimed and not existing:
                self.sources.add(call, key, raw)

    def observe(self, row):
        key, h = row["id"], row["hash"]
        with self.sources.transfer_client() as call:
            rows = json.loads(call("torrents/info?hashes=" + h))
            if rows == []:
                check(time.time() - row["sent"] < 1800, "book_transfer_unconfirmed")
                return
            check(
                isinstance(rows, list) and len(rows) == 1 and self.matches(rows[0], h, key),
                "book_transfer_identity_changed",
            )
            item = rows[0]
            check(item.get("state") not in ("missingFiles", "error"), "book_transfer_failed")
            if (
                item.get("progress") != 1
                or item.get("amount_left") != 0
                or item.get("state")
                not in ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP")
            ):
                return
            spec = json.loads(row["spec"])
            files = json.loads(call("torrents/files?hash=" + h))
            check(
                isinstance(files, list)
                and all(f.get("progress") == 1 for f in files)
                and sorted((f["name"], f["size"]) for f in files)
                == sorted((p, n) for p, n in spec["entries"]),
                "book_transfer_files_changed",
            )
        result = publish_verified(
            key, spec, self.downloads, self.library, BOOK_AUDIO, ".quarry-book-staging", book_order
        )
        with self.db() as db:
            db.execute(
                "UPDATE transfers SET status='complete',files=?,spec=NULL WHERE id=? AND status='downloading'",
                (json.dumps(result), key),
            )

    def once(self):
        if not self.work.acquire(blocking=False):
            return
        try:
            with self.db() as db:
                jobs = db.execute(
                    "SELECT * FROM transfers WHERE status IN ('queued','downloading') AND next<=? ORDER BY next,rowid LIMIT 20",
                    (time.time(),),
                ).fetchall()
            for row in jobs:
                delay = 1
                try:
                    if row["status"] == "queued":
                        self.acquire(row)
                    else:
                        self.observe(row)
                except (OSError, json.JSONDecodeError):
                    delay = 30
                except (
                    Review,
                    ValueError,
                    TypeError,
                    KeyError,
                    subprocess.TimeoutExpired,
                    sqlite3.IntegrityError,
                ) as error:
                    reason = str(error) if isinstance(error, Review) else "book_release_requires_review"
                    with self.db() as db:
                        db.execute(
                            "UPDATE transfers SET status='review',reason=? WHERE id=? AND status=?",
                            (reason, row["id"], row["status"]),
                        )
                with self.db() as db:
                    db.execute(
                        "UPDATE transfers SET next=? WHERE id=? AND status=?",
                        (time.time() + delay, row["id"], row["status"]),
                    )
        finally:
            self.work.release()
