import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from books import BookWorker, BookSources
from test_worker import FakeTransfers


def item(i, size):
    title = f"Fixture Narrator - Book {i}"
    id = hashlib.sha256(f"fixture-{i}".encode()).hexdigest()
    return {"id": id, "title": title, "sizeBytes": size}


class BookFixture(FakeTransfers):
    category = "quarry-audiobooks"
    save = "/downloads/quarry-audiobooks"

    def __init__(self, files):
        super().__init__(files)
        self.entry = item(1, sum(map(len, files.values())))
        self.token = "private-opaque-index-token"
        self.fetches = 0

    def page(self, q, page):
        return [(self.entry, self.token)] if page == 0 else []

    def torrent(self, token):
        if token != self.token:
            raise AssertionError("wrong selected edition")
        self.fetches += 1
        return self.raw

    def add(self, call, key, raw):
        super().add(call, key, raw)
        self.transfers[0]["category"] = self.category


class BooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = [self.root / name for name in ("state", "downloads", "library")]
        for p in self.paths:
            p.mkdir()

    def make(self, files):
        self.src = BookFixture(files)
        worker = BookWorker(*self.paths, self.src)
        self.key = self.src.entry["id"]
        payload = self.paths[1] / self.key / "Music"
        payload.mkdir(parents=True)
        for name, data in files.items():
            (payload / name).write_bytes(data)
        return worker

    def audio(self, filename):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe required for media fixtures")
        target = self.root / filename
        args = ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
        if target.suffix == ".m4b":
            meta = self.root / "fixture.ffmeta"
            meta.write_text(
                ";FFMETADATA1\ntitle=Original Spoken Fixture\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=First\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Second\n"
            )
            args += [
                "-f",
                "ffmetadata",
                "-i",
                str(meta),
                "-map",
                "0:a",
                "-map_metadata",
                "1",
                "-map_chapters",
                "1",
                "-c:a",
                "aac",
            ]
        else:
            args += ["-c:a", "libmp3lame"]
        subprocess.run(args + ["-y", str(target)], check=True, capture_output=True, timeout=25)
        return target.read_bytes()

    def test_selected_edition_cache_survives_restart_and_m4b_chapters_survive_import(self):
        originals = {
            "Disc 10.m4b": self.audio("ten.m4b"),
            "Disc 2.mp3": self.audio("two.mp3"),
            "edition.txt": b"fixture",
        }
        worker = self.make(originals)
        result = worker.search("original book", 0)
        self.assertEqual(result["items"], [self.src.entry])
        self.assertNotIn(self.src.token, json.dumps(result))
        self.assertEqual(worker.status(self.key)["status"], "unknown")
        reopened = BookWorker(*self.paths, self.src)
        self.assertEqual(reopened.submit(self.key)["status"], "queued")
        self.assertEqual(reopened.submit(self.key)["status"], "queued")
        reopened.once()
        again = BookWorker(*self.paths, self.src)
        self.assertEqual(again.submit(self.key)["status"], "downloading")
        again.once()
        status = again.status(self.key)
        self.assertEqual(status["status"], "complete", status)
        self.assertEqual(self.src.calls, 1)
        self.assertEqual(self.src.fetches, 1)
        self.assertEqual(
            [f["path"] for f in status["files"]],
            [self.key + "/001-Disc 2.mp3", self.key + "/002-Disc 10.m4b"],
        )
        for record, name in zip(status["files"], ["Disc 2.mp3", "Disc 10.m4b"]):
            self.assertEqual((self.paths[2] / record["path"]).read_bytes(), originals[name])
            self.assertEqual((self.paths[1] / self.key / "Music" / name).read_bytes(), originals[name])
            self.assertEqual(record["sha256"], hashlib.sha256(originals[name]).hexdigest())
        m4b = self.paths[2] / status["files"][1]["path"]
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", "-i", str(m4b)]
        )
        self.assertEqual([c["tags"]["title"] for c in json.loads(probe)["chapters"]], ["First", "Second"])

    def test_size_category_and_guid_bound_edition_token(self):
        secret = self.root / "prowlarr.key"
        secret.write_text("fixture")
        s = BookSources(
            "http://127.0.0.1:9696",
            7,
            "http://127.0.0.1:8080",
            BookFixture.save,
            secret,
            self.root / "unused",
        )
        row = dict(
            indexerId=7,
            protocol="torrent",
            guid="edition-guid",
            title="Selected spoken edition",
            size=30000,
            downloadUrl=s.prowlarr + "/7/download?link=opaque&apikey=fixture-placeholder",
            categories=[dict(id=3030)],
            seeders=2,
        )
        rows = [
            {**row, "categories": [dict(id=3040)]},
            {**row, "indexerId": 9},
            {**row, "downloadUrl": "https://invalid.example/7/download?link=opaque"},
            row,
        ]
        with patch("books.request", return_value=json.dumps(rows).encode()) as req:
            result = s.page("Selected book", 0)
            self.assertIn("categories=3030", req.call_args.args[1])
        self.assertEqual(result[:3], [None, None, None])
        identity = hashlib.sha256(
            json.dumps([7, row["guid"], row["title"], row["size"]], separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(
            result[3], (dict(id=identity, title=row["title"], sizeBytes=row["size"], seeders=2), "opaque")
        )

    def test_tracker_pages_keep_every_result(self):
        worker = self.make({"a.mp3": b"fixture"})
        entries = [(item(i, 12), "private") for i in range(48)]
        with patch.object(self.src, "page", side_effect=lambda q, p: entries[p * 25 : (p + 1) * 25]):
            pages = [worker.search("book", k) for k in (0, 20, 40)]
        self.assertEqual([r["id"] for p in pages for r in p["items"]], [a["id"] for a, t in entries])
        self.assertEqual([p["nextOffset"] for p in pages], [20, 40, None])

    def test_unknown_or_expired_edition_cannot_initiate_download(self):
        worker = self.make({"a.mp3": b"fixture"})
        self.assertEqual(worker.submit(self.key)["status"], "unknown")
        worker.search("author", 0)
        with worker.db() as db:
            db.execute("UPDATE editions SET expires=0")
        self.assertEqual(worker.submit(self.key)["status"], "review")
        worker.once()
        self.assertEqual(self.src.fetches, 0)
        self.assertEqual(self.src.calls, 0)

    def test_completed_foreign_torrent_is_left_alone(self):
        worker = self.make({"a.mp3": b"fixture"})
        self.src.transfers.append(dict(hash=self.src.hash, category="quarry-music", save_path="/other"))
        worker.search("book", 0)
        worker.submit(self.key)
        worker.once()
        self.assertEqual(worker.status(self.key)["reason"], "book_transfer_already_owned")
        self.assertEqual(self.src.calls, 0)


if __name__ == "__main__":
    unittest.main()
