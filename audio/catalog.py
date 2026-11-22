"""Release-group search and edition track lists for the private music request API."""

import collections
import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def identifier(value):
    try:
        parsed = uuid.UUID(value)
        return str(parsed) if parsed.int and str(parsed) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def display(value):
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= 500 and value.isprintable()
        else None
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Catalog:
    def __init__(self, origin):
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not parsed.hostname
        ):
            raise ValueError("Invalid catalog origin")
        if parsed.scheme != "https":
            local = parsed.hostname == "localhost"
            if not local:
                try:
                    addr = ipaddress.ip_address(parsed.hostname)
                    local = addr.is_private and not addr.is_unspecified and not addr.is_multicast
                except ValueError:
                    pass
            if parsed.scheme != "http" or not local:
                raise ValueError("Catalog requires HTTPS or a private fixture address")
        self.origin = origin.rstrip("/")
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()
        self.next_call = 0
        self.primary_retry = 0

    def get(self, path, **params):
        url = self.origin + "/ws/2/" + path + "?" + urllib.parse.urlencode(dict(params, fmt="json"))
        return self.fetch(url, primary=True)

    def fetch(self, url, primary=False):
        with self.lock:
            now = time.monotonic()
            hit = self.cache.get(url)
            if hit and now - hit[0] < 900:
                self.cache.move_to_end(url)
                return json.loads(hit[1])
            if primary and self.primary_retry > now:
                raise OSError("Metadata service temporarily unavailable")
            if self.next_call > now:
                time.sleep(self.next_call - now)
            self.next_call = time.monotonic() + 1.05
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Quarry-Audio/1.0 (+https://github.com/mcavazos17/quarry)",
                },
            )
            try:
                with self.http.open(req, timeout=5) as resp:
                    raw = resp.read(5 * 1024 * 1024 + 1)
            except OSError as exc:
                if primary and (
                    not isinstance(exc, urllib.error.HTTPError) or exc.code == 429 or exc.code >= 500
                ):
                    self.primary_retry = time.monotonic() + 30
                if hit and now - hit[0] < 86400:
                    return json.loads(hit[1])
                raise
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("Catalog response exceeded limit")
            data = json.loads(raw)
            if not isinstance(data, (dict, list)):
                raise ValueError("Unexpected catalog response")
            self.cache[url] = (time.monotonic(), raw)
            self.cache.move_to_end(url)
            while (
                len(self.cache) > 48
                or sum(len(value[1]) for value in self.cache.values()) > 10 * 1024 * 1024
            ):
                self.cache.popitem(last=False)
            return data

    def fallback(self, function, *args):
        try:
            return function(*args)
        except OSError as exc:
            if self.origin != "https://musicbrainz.org" or (
                isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504)
            ):
                raise
            return None

    def mirror(self, path, **params):
        url = "https://api.lidarr.audio/api/v0.4/" + path
        return self.fetch(url + ("?" + urllib.parse.urlencode(params) if params else ""))

    @staticmethod
    def mirror_group(row):
        artist = next((a for a in row.get("artists", []) if a.get("id") == row.get("artistid")), None)
        if not artist:
            return None
        return Catalog.group(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "first-release-date": row.get("releasedate"),
                "artist-credit": [
                    {"artist": {"id": artist.get("id"), "name": artist.get("artistname")}}
                ],
            }
        )

    @staticmethod
    def group(data):
        key = identifier(data.get("id"))
        title = display(data.get("title"))
        credits = data.get("artist-credit")
        if not key or not title or not isinstance(credits, list) or not credits:
            return None
        names = []
        for credit in credits:
            artist = credit.get("artist") or {}
            name = display(credit.get("name") or artist.get("name"))
            join = credit.get("joinphrase", "")
            if not name or not isinstance(join, str) or (join and not join.isprintable()):
                return None
            names.append(name + join)
        name = display("".join(names).strip())
        if not name:
            return None
        item = {"id": key, "title": title, "artist": name}
        first_id = identifier((credits[0].get("artist") or {}).get("id"))
        if first_id:
            item["artistId"] = first_id
        date = data.get("first-release-date") or ""
        if re.match(r"^[1-9][0-9]{3}", date):
            item["year"] = int(date[:4])
        return item

    def lookup(self, key):
        data = self.fallback(lambda: self.get("release-group/" + key, inc="artist-credits"))
        row = self.group(data) if data is not None else self.mirror_group(self.mirror("album/" + key))
        if not row or row["id"] != key:
            raise ValueError("Invalid release group")
        return row

    def search(self, query, offset):
        if query.startswith(("mbid:", "lidarr:")):
            key = identifier(query.split(":", 1)[1].strip())
            if not key or offset:
                return {"items": []}
            try:
                return {"items": [self.lookup(key)]}
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {"items": []}
                raise
        words = re.findall(r"[^\W_]+", query, re.UNICODE)[:24]
        if not words:
            return {"items": []}
        expression = " AND ".join(f'(releasegroup:"{word}" OR artist:"{word}")' for word in words)
        data = self.fallback(
            lambda: self.get("release-group", query=expression, limit=20, offset=offset)
        )
        if data is None:
            rows = self.mirror("search", type="album", query=query)
            if not isinstance(rows, list):
                raise ValueError("Unexpected metadata search response")
            items = [item for row in rows if (item := self.mirror_group(row))]
            page = {"items": items[offset : offset + 20]}
            if len(items) > offset + 20 and offset < 180:
                page["nextOffset"] = offset + 20
            return page
        groups = data["release-groups"]
        if not isinstance(groups, list) or len(groups) > 20:
            raise ValueError("Unexpected search page")
        page = {"items": [item for group in groups if (item := self.group(group))]}
        if len(groups) == 20 and int(data["count"]) > offset + 20 and offset < 180:
            page["nextOffset"] = offset + 20
        return page

    def tracklist(self, group):
        # A request identifies a release group. Prefer the earliest official edition with the
        # requested title and a complete ordered track list. Other track lists need review.
        data = self.fallback(
            lambda: self.get(
                "release",
                **{
                    "release-group": group["id"],
                    "inc": "recordings artist-credits",
                    "limit": 100,
                    "offset": 0,
                },
            )
        )
        candidates = []
        if data is None:
            album = self.mirror("album/" + group["id"])
            if album.get("id") != group["id"]:
                raise ValueError("Release group changed")
            for ed in album.get("Releases", []):
                if (
                    ed.get("Status") != "Official"
                    or (ed.get("Title") or "").casefold() != group["title"].casefold()
                ):
                    continue
                rows = ed.get("Tracks") or []
                if not 0 < len(rows) <= 256 or len(rows) != ed.get("TrackCount"):
                    continue
                ordered = sorted(rows, key=lambda tr: (tr["MediumNumber"], tr["TrackPosition"]))
                titles = [display(tr.get("TrackName")) for tr in ordered]
                if all(titles) and len(
                    {(tr["MediumNumber"], tr["TrackPosition"]) for tr in ordered}
                ) == len(rows):
                    candidates.append((ed.get("ReleaseDate") or "9999", ed.get("Id") or "", titles))
            return min(candidates)[2] if candidates else []
        for edition in data["releases"]:
            if (
                edition.get("status") != "Official"
                or edition.get("title", "").casefold() != group["title"].casefold()
            ):
                continue
            tracks = []
            media = edition.get("media") or []
            for index, disc in enumerate(media, 1):
                rows = disc.get("tracks") or []
                if (
                    disc.get("position", index) != index
                    or not rows
                    or len(rows) != disc.get("track-count", len(rows))
                ):
                    break
                titles = [
                    display(row.get("title") or (row.get("recording") or {}).get("title"))
                    for row in rows
                ]
                if not all(titles):
                    break
                tracks.extend(titles)
            else:
                if tracks and len(tracks) <= 256:
                    candidates.append((edition.get("date") or "9999", edition.get("id") or "", tracks))
        return min(candidates)[2] if candidates else []
