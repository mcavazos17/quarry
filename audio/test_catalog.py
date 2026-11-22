import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from album_requests import AlbumRequests
from catalog import Catalog
from server import AudioAPI


ID = "8e966d50-fab5-49ca-9d61-2ea8bd2442c6"
ARTIST = "208d1dea-a5c4-45b7-b51d-daf653bdff3e"


class CatalogTests(unittest.TestCase):
    def test_book_routes_only_admit_explicit_post_to_edition(self):
        books = Mock()
        key = "a" * 64
        books.status.return_value = {"id": key, "status": "unknown"}
        catalog = Mock()
        jobs = Mock()
        api = AudioAPI(jobs, catalog, "s" * 32, books)
        url = "/api/books/requests/" + key
        self.assertEqual(api.route("GET", url), (200, books.status.return_value))
        books.submit.assert_not_called()
        for bad in (url + "?retry=1", url + "0", "/api/music/requests/" + key):
            self.assertEqual(api.route("POST", bad)[0], 404)
        api.route("POST", url)
        books.submit.assert_called_once_with(key)
        api.route("GET", "/api/books/search?q=novel&offset=20")
        books.search.assert_called_once_with("novel", 20)
        catalog.search.assert_not_called()
        jobs.submit.assert_not_called()

    def test_mirror_preserves_group_and_track_order(self):
        c = Catalog("https://musicbrainz.org")
        c.get = Mock(side_effect=urllib.error.HTTPError("", 503, "busy", {}, None))
        row = {
            "id": ID,
            "artistid": ARTIST,
            "title": "Orbit",
            "artists": [{"id": ARTIST, "artistname": "Example"}],
            "Releases": [
                {
                    "Title": "Orbit",
                    "Status": "Official",
                    "TrackCount": 2,
                    "Tracks": [
                        {"MediumNumber": 1, "TrackPosition": 2, "TrackName": "Second"},
                        {"MediumNumber": 1, "TrackPosition": 1, "TrackName": "First"},
                    ],
                }
            ],
        }
        c.mirror = lambda path, **params: [row] if path == "search" else row
        group = c.search("orbit", 0)["items"][0]
        self.assertEqual(group, c.lookup(ID))
        self.assertEqual(ID, group["id"])
        self.assertEqual(c.tracklist(group), ["First", "Second"])

    def test_receipt_survives_restart_and_metadata_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog, worker = Mock(), Mock()
            state = {"status": "unknown", "id": ID}
            worker.status.side_effect = lambda key: dict(state)
            worker.submit.side_effect = lambda *args: state.update(status="searching")
            jobs = AlbumRequests(Path(folder), worker, catalog)
            api = AudioAPI(jobs, catalog, "s" * 32)
            url = "/api/music/requests/" + ID
            self.assertEqual(api.route("POST", url)[1]["status"], "queued")
            catalog.lookup.side_effect = OSError()
            jobs.once()
            worker.submit.assert_not_called()
            jobs = AlbumRequests(Path(folder), worker, catalog)
            self.assertEqual(jobs.status(ID)["status"], "queued")
            with jobs.connect() as db:
                db.execute("UPDATE pending SET next_try=0")
            catalog.lookup.side_effect = None
            catalog.lookup.return_value = {"artist": "Example", "title": "Orbit"}
            catalog.tracklist.return_value = ["First"]
            jobs.once()
            self.assertEqual(api.route("POST", url)[1]["status"], "searching")
            worker.submit.assert_called_once_with(ID, "Example", "Orbit", ["First"])
