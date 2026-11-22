import contextlib
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import MusicWorker
from worker_sources import CATEGORY, MusicSources, Review, manifest, release_matches


GROUP = "12345678-1234-4abc-a123-012345678900"
ARTIST = "Quarry Fixture Artist"
ALBUM = "Two Signals"
TITLES = ["First Signal", "Second Signal"]


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def make_torrent(files):
    payload = b"".join(files.values())
    unit = 16384
    info = {
        b"name": b"Music",
        b"piece length": unit,
        b"private": 1,
        b"pieces": b"".join(
            hashlib.sha1(payload[i : i + unit]).digest() for i in range(0, len(payload), unit)
        ),
        b"files": [{b"length": len(data), b"path": [name.encode()]} for name, data in files.items()],
    }
    return bencode({b"info": info}), hashlib.sha1(bencode(info)).hexdigest()


class FakeTransfers:
    save = "/downloads/quarry-music"

    def __init__(self, files):
        self.files = files
        self.raw, self.hash = make_torrent(files)
        self.transfers = []
        self.calls = 0
        self.drop_ack = False

    def candidates(self, artist, album):
        if (artist, album) != (ARTIST, ALBUM):
            raise AssertionError("unexpected search")
        yield self.raw, sum(map(len, self.files.values()))

    @contextlib.contextmanager
    def transfer_client(self):
        def call(path):
            if path == "torrents/info?hashes=" + self.hash:
                return json.dumps(self.transfers).encode()
            if path == "torrents/files?hash=" + self.hash:
                return json.dumps(
                    [
                        dict(name="Music/" + name, size=len(data), progress=1)
                        for name, data in self.files.items()
                    ]
                ).encode()
            raise AssertionError("unexpected downloader operation")

        yield call

    def add(self, call, key, raw):
        if self.transfers or raw != self.raw:
            raise AssertionError("duplicate or modified torrent")
        self.calls += 1
        self.transfers.append(
            dict(
                hash=self.hash,
                category=CATEGORY,
                state="stalledUP",
                progress=1,
                amount_left=0,
                save_path=self.save + "/" + key,
            )
        )
        if self.drop_ack:
            raise OSError("Synthetic lost acknowledgment")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state, self.downloads, self.library = [self.root / x for x in ("state", "downloads", "albums")]
        for p in (self.state, self.downloads, self.library):
            p.mkdir()

    def original(self, name, title, number, album=ALBUM):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe required for media fixture")
        p = self.root / name
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={number * 220}:duration=0.2",
                "-c:a",
                "flac",
                "-metadata",
                "artist=" + ARTIST,
                "-metadata",
                "album=" + album,
                "-metadata",
                "title=" + title,
                "-metadata",
                f"track={number}",
                "-y",
                str(p),
            ],
            check=True,
            timeout=20,
        )
        return p.read_bytes()

    def worker(self, files):
        self.source = FakeTransfers(files)
        self.spool = self.downloads / GROUP / "Music"
        self.spool.mkdir(parents=True)
        for name, data in files.items():
            (self.spool / name).write_bytes(data)
        return MusicWorker(self.state, self.downloads, self.library, self.source)

    def test_album_release_boundaries(self):
        self.assertTrue(
            release_matches("Quarry_Fixture_Artist-Two_Signals-CD-FLAC-2026-GROUP", ARTIST, ALBUM)
        )
        for label in [
            "Quarry Fixture Artist - Two Signals Live [FLAC]",
            "Quarry Fixture Artist - Two Signals deluxe",
            "Other Artist - Two Signals",
        ]:
            self.assertFalse(release_matches(label, ARTIST, ALBUM))

    def test_scene_packaging_and_spelling(self):
        for label in [
            "Quarry_Fixture_Artist-Two_Signals-Digipak-CD-FLAC-2023-GROUP",
            "Quarry Fixture Artist - 2023 - Two Signals [FLAC 24bit 96kHz]",
            "Quarry Fixture Artist - Two Signals (001122334455) CD FLAC (log + cue)",
        ]:
            self.assertTrue(release_matches(label, ARTIST, ALBUM), label)
        self.assertTrue(release_matches("Artiste - One and Two [FLAC]", "Artísté", "One & Two"))
        self.assertFalse(
            release_matches("Quarry Fixture Artist - Two Signals Remastered [FLAC]", ARTIST, ALBUM)
        )

    def test_tracker_display_size_is_rounded(self):
        # Prowlarr reads the size cell from IPTorrents search HTML. The torrent manifest contains exact bytes.
        actual = 1234511
        raw, _ = make_torrent({"one.flac": b"x" * actual})
        _h, spec = manifest(raw, int(1.2 * 1024**2))
        self.assertEqual(spec["entries"][0][1], actual)
        with self.assertRaisesRegex(Review, "release_size_changed"):
            manifest(raw, 2 * 1024**2)

    def test_incompatible_first_metainfo_uses_next_match_without_dispatching_twice(self):
        worker = self.worker({"one.flac": b"one", "two.flac": b"two"})
        other, _ = make_torrent({"wrong-edition.flac": b"one"})
        with patch.object(self.source, "candidates", return_value=iter([(other, 3), (self.source.raw, 6)])):
            worker.submit(GROUP, ARTIST, ALBUM, TITLES)
            worker.once()
        self.assertEqual(worker.status(GROUP)["status"], "downloading")
        self.assertEqual(self.source.calls, 1)

    def test_temporary_index_error_recovers_without_losing_request(self):
        worker = self.worker({"one.flac": b"one", "two.flac": b"two"})
        worker.submit(GROUP, ARTIST, ALBUM, TITLES)
        with patch.object(
            self.source, "candidates", side_effect=ConnectionError("indexer temporarily unavailable")
        ):
            worker.once()
        self.assertEqual(worker.status(GROUP)["status"], "searching")
        self.assertEqual(self.source.calls, 0)
        with worker.db() as db:
            db.execute("UPDATE albums SET next=0")
        worker.once()
        self.assertEqual(self.source.calls, 1)

    def test_indexer_category_and_exact_identity(self):
        key = self.root / "key"
        key.write_text("private-fixture-key")
        s = MusicSources(
            "http://127.0.0.1:17170",
            1,
            "http://127.0.0.1:17171",
            "/downloads/quarry-music",
            key,
            self.root / "unused",
        )
        base = dict(
            indexerId=1,
            protocol="torrent",
            categories=[dict(id=3040)],
            title=ARTIST + " - " + ALBUM + " [FLAC]",
            size=200,
            seeders=4,
            downloadUrl=s.prowlarr + "/1/download?link=selected",
        )
        rows = [
            {**base, "indexerId": 2},
            {**base, "categories": [dict(id=3030)]},
            {**base, "title": ARTIST + " - " + ALBUM + " Deluxe"},
        ]
        with patch("worker_sources.request", return_value=json.dumps(rows).encode()) as req:
            self.assertEqual(list(s.candidates(ARTIST, ALBUM)), [])
            self.assertEqual(req.call_count, 1)
        with patch(
            "worker_sources.request", side_effect=[json.dumps(rows + [base]).encode(), b"selected"]
        ) as req:
            self.assertEqual(list(s.candidates(ARTIST, ALBUM)), [(b"selected", 200)])
            self.assertEqual(req.call_args.args[1], base["downloadUrl"])

    def test_missing_tracklist_never_dispatches(self):
        worker = self.worker({"one.flac": b"fixture"})
        self.assertEqual(worker.submit(GROUP, ARTIST, ALBUM, None)["status"], "review")
        worker.once()
        self.assertEqual(self.source.calls, 0)

    def test_rejects_mismatched_edition_before_download(self):
        worker = self.worker({"one.flac": b"fixture"})
        worker.submit(GROUP, ARTIST, ALBUM, TITLES)
        worker.once()
        self.assertEqual(worker.status(GROUP)["reason"], "edition_file_count_mismatch")
        self.assertFalse(self.source.transfers)

    def test_foreign_download_never_adopted(self):
        worker = self.worker({"a.flac": b"one", "b.flac": b"two"})
        self.source.transfers.append(dict(hash=self.source.hash, category="quarry", save_path="/elsewhere"))
        worker.submit(GROUP, ARTIST, ALBUM, TITLES)
        worker.once()
        self.assertEqual(worker.status(GROUP)["reason"], "transfer_already_owned")
        self.assertEqual(self.source.calls, 0)

    def test_invalid_paths_and_unexpected_formats(self):
        for filename in ["../escape.flac", "packed.rar", "launch.exe", r"bad\name.flac"]:
            raw, _ = make_torrent({filename: b"x"})
            with self.assertRaises(Review):
                manifest(raw, 1)

    def test_publish_recovery_keeps_seed_and_bytes(self):
        files = {
            "b.flac": self.original("two.flac", TITLES[1], 2),
            "a.flac": self.original("one.flac", TITLES[0], 1),
        }
        worker = self.worker(files)
        worker.submit(GROUP, ARTIST, ALBUM, TITLES)
        self.source.drop_ack = True
        worker.once()
        self.assertEqual(worker.status(GROUP)["status"], "downloading")
        # Advance the synthetic retry clock after a lost HTTP acknowledgment, then reopen the process.
        with worker.db() as db:
            db.execute("UPDATE albums SET next=0")
        worker = MusicWorker(self.state, self.downloads, self.library, self.source)
        worker.submit(GROUP, ARTIST, ALBUM, TITLES)
        worker.once()
        result = worker.status(GROUP)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.source.calls, 1)
        for idx, row in enumerate(result["files"]):
            raw = files[["a.flac", "b.flac"][idx]]
            self.assertEqual((self.library / row["path"]).read_bytes(), raw)
            self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
        for n, raw in files.items():
            self.assertEqual((self.spool / n).read_bytes(), raw)

    def test_embedded_title_disagrees(self):
        worker = self.worker({"one.flac": self.original("one.flac", "Unexpected Recording", 1)})
        worker.submit(GROUP, ARTIST, ALBUM, [TITLES[0]])
        worker.once()
        worker.once()
        self.assertEqual(worker.status(GROUP)["reason"], "edition_tracklist_mismatch")
        self.assertFalse((self.library / GROUP).exists())

    def test_corrupt_bytes_never_published(self):
        file = self.original("one.flac", TITLES[0], 1)
        worker = self.worker({"one.flac": file})
        worker.submit(GROUP, ARTIST, ALBUM, [TITLES[0]])
        worker.once()
        (self.spool / "one.flac").write_bytes(file[:-1] + bytes([file[-1] ^ 1]))
        worker.once()
        self.assertEqual(worker.status(GROUP)["reason"], "piece_hash_mismatch")
        self.assertFalse((self.library / GROUP).exists())

    def test_downloader_add_accepts_v52_response_and_defers_to_hash_observation(self):
        from unittest.mock import Mock

        s = MusicSources(
            "http://127.0.0.1:9696",
            1,
            "http://127.0.0.1:8080",
            "/downloads/quarry-music",
            self.root / "key",
            self.root / "auth",
        )
        for reply in (
            b"Ok.",
            b"",
            json.dumps(
                {"success_count": 1, "failure_count": 0, "pending_count": 0, "added_torrent_ids": ["a" * 40]}
            ).encode(),
        ):
            call = Mock(return_value=reply)
            s.add(call, GROUP, b"synthetic torrent bytes")
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0], "torrents/add")

    def test_legacy_acknowledgment_review_observes_same_download_after_restart(self):
        payload = self.original("one.flac", TITLES[0], 1)
        worker = self.worker({"one.flac": payload})
        worker.submit(GROUP, ARTIST, ALBUM, [TITLES[0]])
        worker.once()
        with worker.db() as db:
            db.execute(
                "UPDATE albums SET status='review',reason='download_not_acknowledged' WHERE id=?", (GROUP,)
            )
        restored = MusicWorker(self.state, self.downloads, self.library, self.source)
        self.assertEqual(restored.status(GROUP)["status"], "downloading")
        restored.once()
        self.assertEqual(restored.status(GROUP)["status"], "complete")
        self.assertEqual(self.source.calls, 1)


if __name__ == "__main__":
    unittest.main()
