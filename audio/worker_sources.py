"""Private indexer and transfer-client calls for Quarry music."""

import contextlib
import hashlib
import http.cookiejar
import ipaddress
import json
import re
import secrets
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


CATEGORY = "quarry-music"
MAX_BYTES = 16 * 1024**3
AUDIO = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aif", ".aiff"}
EXTRAS = {".jpg", ".jpeg", ".png", ".cue", ".log", ".nfo", ".m3u", ".m3u8", ".sfv", ".md5", ".txt", ".pdf"}


class Review(ValueError):
    """An identity or import decision requires owner review."""


def check(condition, code):
    if not condition:
        raise Review(code)


def normalized(text):
    text = "".join(c for c in unicodedata.normalize("NFKD", text).casefold() if not unicodedata.combining(c))
    text = re.sub(r"(?<=\w)['’](?=\w)", "", text).replace("&", " and ")
    return " ".join(re.findall(r"[^\W_]+", text))


def release_matches(label, artist, album):
    parts = normalized(label).split()
    credit, title = normalized(artist).split(), normalized(album).split()
    if not credit or not title or parts[: len(credit)] != credit:
        return False
    release = parts[len(credit) :]
    if release[: len(title)] != title and release and re.fullmatch(r"(?:19|20)\d{2}", release[0]):
        release = release[1:]
    if release[: len(title)] != title:
        return False
    # A suffix may describe packaging, format or the scene group. Album and edition
    # terms such as "live", "deluxe", "instrumental" or "remastered" must match the request.
    suffix = release[len(title) :]
    formats = {
        "flac",
        "mp3",
        "web",
        "cd",
        "cdda",
        "vinyl",
        "lossless",
        "vbr",
        "v0",
        "cbr",
        "kbps",
        "bit",
        "khz",
        "rip",
        "digipak",
        "retail",
        "digital",
        "lp",
        "ep",
        "single",
        "log",
        "cue",
        "eac",
        "cuesheet",
        "webdl",
        "dl",
        "tracks",
    }
    terms = [
        s
        for s in suffix
        if s not in formats and not s.isdecimal() and not re.fullmatch(r"\d+(?:bit|kbps|khz|cd)", s)
    ]
    # Scene group names come after the final hyphen, never inside the album name.
    tail = label.rsplit("-", 1)[-1].strip()
    return not terms or (
        len(terms) == 1
        and bool(re.fullmatch(r"[A-Za-z0-9]{2,28}", tail))
        and tail.casefold() == terms[0]
        and any(s in formats for s in suffix[:-1])
        and terms[0]
        not in {
            "live",
            "deluxe",
            "remaster",
            "remastered",
            "instrumental",
            "acoustic",
            "bonus",
            "expanded",
            "mono",
            "stereo",
            "anniversary",
            "rerecording",
        }
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def origin(value):
    p = urllib.parse.urlsplit(value)
    check(
        p.scheme in ("http", "https")
        and p.hostname
        and not p.username
        and not p.password
        and not p.query
        and not p.fragment
        and p.path in ("", "/"),
        "invalid_origin",
    )
    allowed = tuple(
        ipaddress.ip_network(n)
        for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
    )
    records = socket.getaddrinfo(
        p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM
    )
    check(
        records and all(any(ipaddress.ip_address(r[4][0]) in net for net in allowed) for r in records),
        "public_origin",
    )
    return value.rstrip("/")


def secret(path):
    p = Path(path)
    check(p.is_file() and not p.is_symlink() and 0 < p.stat().st_size <= 8192, "invalid_service_secret")
    return p.read_text().strip()


def client():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def request(op, address, headers, data=None, maximum=4 * 1024**2):
    req = urllib.request.Request(address, data, headers)
    with op.open(req, timeout=25) as response:
        content = response.read(maximum + 1)
    check(len(content) <= maximum, "oversized_service_response")
    return content


class MusicSources:
    def __init__(self, prowlarr, indexer, qbit, save, prow_key_file, qbit_auth_file):
        self.prowlarr = origin(prowlarr)
        self.qbit = origin(qbit)
        self.indexer = int(indexer)
        self.save = save.rstrip("/")
        check(
            0 < self.indexer < 2**31 and re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)*/quarry-music", self.save),
            "invalid_music_routing",
        )
        self.prow_key_file = prow_key_file
        self.qbit_auth_file = qbit_auth_file

    def candidates(self, artist, album):
        query = urllib.parse.urlencode(
            dict(
                query=f"{artist} {album}", type="search", categories=3000, indexerIds=self.indexer, limit=100
            )
        )
        headers = {"X-Api-Key": secret(self.prow_key_file)}
        rows = json.loads(request(client(), self.prowlarr + "/api/v1/search?" + query, headers))
        check(isinstance(rows, list) and len(rows) <= 2000, "invalid_index_results")
        candidates = []
        for row in rows:
            if row.get("protocol") != "torrent" or row.get("indexerId") != self.indexer:
                continue
            categories = [c.get("id") for c in row.get("categories", []) if isinstance(c, dict)]
            if 3030 in categories or not any(type(c) is int and 3000 <= c < 4000 for c in categories):
                continue
            label = row.get("title")
            if not isinstance(label, str) or len(label) > 500 or not release_matches(label, artist, album):
                continue
            seeds, size = row.get("seeders"), row.get("size")
            if type(seeds) is not int or seeds < 1 or type(size) is not int or not 0 < size <= MAX_BYTES:
                continue
            link = urllib.parse.urlsplit(row.get("downloadUrl") or "")
            if (
                link.scheme + "://" + link.netloc != self.prowlarr
                or link.path != "/" + str(self.indexer) + "/download"
                or link.fragment
            ):
                continue
            # Forward only the relative, indexer-specific proxy path; all credentials stay on this service.
            candidates.append(("flac" in normalized(label).split(), seeds, link.geturl(), size))
        # Only read metainfo for alternate candidates. The worker submits at most one transfer.
        for _, _, link, size in sorted(set(candidates), reverse=True)[:5]:
            try:
                yield request(client(), link, headers), size
            except urllib.error.HTTPError as error:
                if error.code not in (404, 410):
                    raise

    @contextlib.contextmanager
    def transfer_client(self):
        op = client()
        creds = json.loads(secret(self.qbit_auth_file))
        headers = {"Referer": self.qbit + "/"}
        if set(creds) == {"apiKey"}:
            key = creds["apiKey"]
            check(
                isinstance(key, str) and key.isascii() and not any(ch.isspace() for ch in key),
                "invalid_service_secret",
            )
            headers["Authorization"] = "Bearer " + key
        else:
            check(set(creds) == {"username", "password"}, "invalid_service_secret")
            body = urllib.parse.urlencode(creds).encode()
            response = request(op, self.qbit + "/api/v2/auth/login", headers, body, 1024)
            cookies = [
                cookie
                for handler in op.handlers
                if hasattr(handler, "cookiejar")
                for cookie in handler.cookiejar
            ]
            if response.strip() not in (b"", b"Ok.") or not cookies:
                raise ConnectionError("downloader_session_unavailable")
        try:

            def call(endpoint, data=None, extra=None):
                return request(op, self.qbit + "/api/v2/" + endpoint, headers | (extra or {}), data)

            cat = json.loads(call("torrents/categories")).get(CATEGORY)
            check(
                isinstance(cat, dict) and (cat.get("savePath") or "").rstrip("/") == self.save,
                "category_location_changed",
            )
            yield call
        finally:
            if "password" in creds:
                with contextlib.suppress(OSError):
                    request(op, self.qbit + "/api/v2/auth/logout", headers, b"", 1024)

    def add(self, call, key, raw):
        boundary = secrets.token_hex(24)
        form = bytearray()
        for name, val in dict(
            category=CATEGORY, savepath=self.save + "/" + key, autoTMM="false", contentLayout="Original"
        ).items():
            form.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
            )
        form.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="album.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
        )
        form.extend(raw)
        form.extend(f"\r\n--{boundary}--\r\n".encode())
        reply = call(
            "torrents/add", bytes(form), {"Content-Type": "multipart/form-data; boundary=" + boundary}
        )
        check(reply.strip() in (b"", b"Ok."), "download_not_acknowledged")


def manifest(raw, reported_size):
    """Read the original v1 info bytes, reject ambiguous names before submitting a transfer."""
    offset = 0
    info_hash = None
    nodes = 0

    def read(depth):
        nonlocal offset, info_hash, nodes
        nodes += 1
        check(depth < 24 and nodes < 50000 and offset < len(raw), "invalid_metainfo")
        mark = raw[offset : offset + 1]
        if mark in (b"d", b"l"):
            result = {} if mark == b"d" else []
            offset += 1
            while raw[offset : offset + 1] != b"e":
                if isinstance(result, list):
                    result.append(read(depth + 1))
                else:
                    k = read(depth + 1)
                    check(isinstance(k, bytes) and k not in result, "invalid_metainfo")
                    start = offset
                    result[k] = read(depth + 1)
                    if depth == 0 and k == b"info":
                        info_hash = hashlib.sha1(raw[start:offset]).hexdigest()
            offset += 1
            return result
        if mark == b"i":
            end = raw.find(b"e", offset + 1, offset + 23)
            check(
                end > 0 and re.fullmatch(rb"(?:0|-?[1-9][0-9]{0,18})", raw[offset + 1 : end]),
                "invalid_metainfo",
            )
            number = int(raw[offset + 1 : end])
            offset = end + 1
            return number
        end = raw.find(b":", offset, offset + 12)
        check(end > offset and raw[offset:end].isdigit(), "invalid_metainfo")
        n = int(raw[offset:end])
        offset = end + 1 + n
        check(offset <= len(raw), "invalid_metainfo")
        return raw[end + 1 : offset]

    check(isinstance(raw, bytes) and 0 < len(raw) <= 4 * 1024**2, "invalid_metainfo")
    top = read(0)
    check(
        isinstance(top, dict)
        and offset == len(raw)
        and info_hash
        and not ({b"url-list", b"httpseeds", b"nodes"} & top.keys()),
        "unsupported_metainfo",
    )
    info = top[b"info"]
    check(
        isinstance(info, dict)
        and info.get(b"private") == 1
        and not ({b"meta version", b"attr", b"symlink path"} & info.keys()),
        "unsupported_metainfo",
    )

    def component(b):
        check(isinstance(b, bytes) and 0 < len(b) <= 200, "invalid_payload_path")
        s = b.decode("utf-8")
        check(
            s.isprintable()
            and not s.startswith(".")
            and not set("/\\:").intersection(s)
            and not s.endswith((" ", ".")),
            "invalid_payload_path",
        )
        return s

    root = component(info.get(b"name"))
    check(
        info.get(b"name.utf-8", info[b"name"]) == info[b"name"] and (b"files" in info) != (b"length" in info),
        "ambiguous_metainfo",
    )
    files = {}
    rows = info.get(b"files", [{b"path": [], b"length": info.get(b"length")}])
    check(isinstance(rows, list) and 0 < len(rows) <= 700, "invalid_payload_count")
    for row in rows:
        check(isinstance(row, dict) and not ({b"symlink path", b"attr"} & row.keys()), "invalid_payload")
        parts = row.get(b"path")
        check(
            isinstance(parts, list)
            and len(parts) <= 12
            and (parts or b"files" not in info)
            and row.get(b"path.utf-8", parts) == parts,
            "invalid_payload_path",
        )
        path = "/".join([root, *map(component, parts)])
        size = row.get(b"length")
        check(
            PurePosixPath(path).suffix.lower() in AUDIO | EXTRAS
            and type(size) is int
            and size >= 0
            and path not in files,
            "invalid_payload",
        )
        files[path] = size
    folded = {p.casefold() for p in files}
    check(
        len(folded) == len(files)
        and not any(str(parent).casefold() in folded for p in files for parent in PurePosixPath(p).parents),
        "conflicting_payload_paths",
    )
    total = sum(files.values())
    check(
        0 < total <= MAX_BYTES
        and abs(total - reported_size) <= min(64 * 1024**2, max(1, reported_size // 20)),
        "release_size_changed",
    )
    n = info.get(b"piece length")
    hashes = info.get(b"pieces")
    check(
        type(n) is int
        and 0 < n <= 64 * 1024**2
        and isinstance(hashes, bytes)
        and len(hashes) == 20 * ((total + n - 1) // n),
        "invalid_piece_table",
    )
    audio = [p for p in files if PurePosixPath(p).suffix.lower() in AUDIO]
    check(0 < len(audio) <= 512 and all(files[p] > 0 for p in audio), "missing_audio")
    return info_hash, {"entries": list(files.items()), "piece": n, "hashes": hashes.hex()}
