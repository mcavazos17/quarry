"""Admit a charged transfer only against fresh tracker-account evidence."""
import contextlib
import json
import sqlite3
import time
from pathlib import Path


class Admission:
    def __init__(self, state, evidence, clock=time.time):
        self.evidence = Path(evidence)
        self.clock = clock
        self.db = Path(state) / "tracker-reservations.sqlite"
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS reservations (observed INTEGER PRIMARY KEY, job TEXT, bytes INTEGER)")
            db.commit()

    def snapshot(self):
        try:
            data = json.loads(self.evidence.read_bytes())
            for name in ("observedAt", "uploadedBytes", "downloadedBytes", "availableSlots", "waitUntil"):
                if type(data[name]) is not int or data[name] < 0:
                    raise ValueError()
            now = self.clock()
            if (data.get("tracker") != "torrentleech" or data.get("downloadAllowed") is not True
                or not 0 <= now - data["observedAt"] <= 300 or data["waitUntil"] > now or not data["availableSlots"]):
                raise ValueError()
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise OSError("Fresh tracker account confirmation required") from error
        return data

    def reserve(self, job, size, call):
        info = self.snapshot()
        prefs = json.loads(call("app/preferences"))
        if any(prefs.get(k) is not False for k in ("dht", "pex", "lsd", "max_ratio_enabled",
                                                   "max_seeding_time_enabled", "max_inactive_seeding_time_enabled")):
            raise OSError("Dedicated client settings require verification")
        torrents = json.loads(call("torrents/info"))
        if not isinstance(torrents, list) or any(row.get("progress") != 1 for row in torrents):
            raise OSError("Dedicated download slot occupied")
        # Full-size accounting is conservative for freeleech and partial discounts.
        # Reserve recent charges even if the site's counters have already caught up.
        with contextlib.closing(sqlite3.connect(self.db, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stamp = info["observedAt"]
            previous = db.execute("SELECT job, bytes FROM reservations WHERE observed=?", (stamp,)).fetchone()
            if previous == (job, size):
                return
            if previous:
                raise OSError("Tracker confirmation already used")
            recent = db.execute("SELECT coalesce(sum(bytes),0) FROM reservations WHERE observed>=?",
                                (stamp-1800,)).fetchone()[0]
            projected = info["downloadedBytes"] + recent + size
            if projected >= 6_000_000_000 and 5 * info["uploadedBytes"] < 2 * projected:
                raise OSError("Insufficient verified tracker credit")
            db.execute("INSERT INTO reservations VALUES(?,?,?)", (stamp, job, size))
