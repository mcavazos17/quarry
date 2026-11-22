import json
from pathlib import Path
import tempfile
import unittest
from tracker_admission import Admission


class TrackerAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.file = self.root / "account.json"
        self.guard = Admission(self.root, self.file, lambda: 10000)
        self.facts = dict(tracker="torrentleech", observedAt=9998, waitUntil=0, uploadedBytes=4_000_000_000,
                          downloadedBytes=8_000_000_000, availableSlots=3, downloadAllowed=True)
        self.prefs = dict.fromkeys(("dht", "pex", "lsd", "max_ratio_enabled", "max_seeding_time_enabled",
                                    "max_inactive_seeding_time_enabled"), False)
        self.torrents = []

    def call(self, path):
        return json.dumps({"app/preferences": self.prefs, "torrents/info": self.torrents}[path]).encode()

    def reserve(self, key, size, **updates):
        self.file.write_text(json.dumps(self.facts | updates))
        self.guard.reserve(key, size, self.call)

    def test_album_and_book_cannot_spend_the_same_credit_twice(self):
        self.reserve("album", 2_000_000_000)
        self.reserve("album", 2_000_000_000)
        with self.assertRaises(OSError):
            self.reserve("book", 1)
        with self.assertRaises(OSError):
            self.reserve("book", 1, observedAt=10000)

    def test_tracker_refusal_client_drift_and_partial_transfers_hold_new_jobs(self):
        for updates in ({"observedAt":9500}, {"waitUntil":10001}, {"availableSlots":0}, {"downloadAllowed":False}):
            with self.subTest(updates=updates), self.assertRaises(OSError):
                self.reserve("book", 100, **updates)
        self.prefs["lsd"] = True
        with self.assertRaises(OSError):
            self.reserve("album", 1)
        self.prefs["lsd"] = False
        self.torrents = [{"progress":0.99}]
        with self.assertRaises(OSError):
            self.reserve("album", 1)
