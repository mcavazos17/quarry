"""Publish the exact music edition after validating torrent bytes and embedded track identity."""

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

try:
    from .worker_sources import AUDIO, check, normalized
except ImportError:
    from worker_sources import AUDIO, check, normalized


def safe_reader(root, relative):
    # The key and the manifest components have been validated. Never follow links supplied by the downloader.
    parts = relative.split("/")
    if os.name == "nt":  # Development fixtures. Production uses Linux dirfd traversal.
        path = root.joinpath(*parts)
        check(path.resolve(strict=True) == path, "download_link")
        return path.open("rb")
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    fd = os.open(root, flags)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.fdopen(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb")
    finally:
        os.close(fd)


def track_identity(path, artist, album, group):
    tag_keys = "artist,album_artist,albumartist,album,title,disc,track,musicbrainz_releasegroupid"
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "flac,mp3,mov,ogg,wav,aiff",
            "-show_entries",
            f"format_tags={tag_keys}:stream=codec_type:stream_disposition=attached_pic:stream_tags={tag_keys}",
            "-of",
            "json",
            "-i",
            str(path),
        ],
        capture_output=True,
        timeout=25,
    )
    check(p.returncode == 0 and len(p.stdout) <= 512 * 1024, "unsupported_audio")
    meta = json.loads(p.stdout)
    streams = meta.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    check(
        len(audio) == 1
        and all(s in audio or s.get("disposition", {}).get("attached_pic") == 1 for s in streams),
        "unexpected_streams",
    )
    tags = {
        k.casefold(): v
        for t in [audio[0].get("tags", {}), meta.get("format", {}).get("tags", {})]
        for k, v in t.items()
        if isinstance(v, str)
    }
    album_artist = tags.get("album_artist") or tags.get("albumartist") or tags.get("artist", "")
    if "musicbrainz_releasegroupid" in tags:
        check(tags["musicbrainz_releasegroupid"].casefold() == group, "release_group_mismatch")
    check(
        normalized(album_artist) == normalized(artist)
        and normalized(tags.get("album", "")) == normalized(album),
        "album_tags_mismatch",
    )

    def ordinal(tag, default):
        value = tags.get(tag, default)
        check(
            isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}(?:/[0-9]{1,3})?", value),
            "missing_track_position",
        )
        i = int(value.split("/")[0])
        check(i > 0, "invalid_track_position")
        return i

    title = normalized(tags.get("title", ""))
    check(title, "missing_track_title")
    return ordinal("disc", "1"), ordinal("track", None), title


def publish(key, spec, artist, album, tracks, downloads, library):
    staging = library.parent / ".quarry-music-staging"
    staging.mkdir(mode=0o750, exist_ok=True)
    check(
        staging.resolve(strict=True) == staging and staging.stat().st_dev == library.stat().st_dev,
        "staging_volume_changed",
    )
    final = library / key
    expected = bytes.fromhex(spec["hashes"])
    piece_size = spec["piece"]
    piece = hashlib.sha1()
    index = used = 0
    rows = []
    with tempfile.TemporaryDirectory(prefix=key + "-", dir=staging) as work:
        work = Path(work)
        # Rename a child, leaving TemporaryDirectory responsible for the surrounding workspace.
        ready = work / "album"
        ready.mkdir(mode=0o750)
        for name, size in spec["entries"]:
            extension = Path(name).suffix.lower()
            is_audio = extension in AUDIO
            target = work / (str(len(rows)) + extension) if is_audio else None
            sha = hashlib.sha256()
            with (
                safe_reader(downloads, key + "/" + name) as src,
                target.open("xb") if target else contextlib.nullcontext() as dst,
            ):
                start = os.fstat(src.fileno())
                check(stat.S_ISREG(start.st_mode) and start.st_size == size, "payload_size_changed")
                remaining = size
                while remaining:
                    chunk = src.read(min(512 * 1024, remaining, piece_size - used))
                    check(chunk, "payload_truncated")
                    piece.update(chunk)
                    used += len(chunk)
                    remaining -= len(chunk)
                    if dst:
                        dst.write(chunk)
                        sha.update(chunk)
                    if used == piece_size:
                        check(piece.digest() == expected[index : index + 20], "piece_hash_mismatch")
                        piece = hashlib.sha1()
                        used = 0
                        index += 20
                check(
                    not src.read(1) and os.fstat(src.fileno()).st_mtime_ns == start.st_mtime_ns,
                    "payload_changed_during_import",
                )
                if dst:
                    dst.flush()
                    os.fsync(dst.fileno())
            if target:
                disc, num, title = track_identity(target, artist, album, key)
                rows.append((disc, num, title, target, sha.hexdigest(), size))
        if used:
            check(piece.digest() == expected[index : index + 20], "piece_hash_mismatch")
            index += 20
        check(index == len(expected), "incomplete_piece_table")
        rows.sort(key=lambda r: (r[0], r[1]))
        check(
            len({(r[0], r[1]) for r in rows}) == len(rows)
            and [r[2] for r in rows] == [normalized(t) for t in tracks],
            "edition_tracklist_mismatch",
        )
        files = []
        for disc, num, _, target, digest, size in rows:
            name = f"{disc:02d}-{num:03d}{target.suffix}"
            target.rename(ready / name)
            os.chmod(ready / name, 0o640)
            files.append({"path": key + "/" + name, "size": size, "sha256": digest})
        # A crash after rename but before the journal commit resumes by comparing immutable output.
        if os.path.lexists(final):
            check(final.resolve(strict=True) == final and final.is_dir(), "library_conflict")
            check(
                {f.name for f in final.iterdir()} == {Path(f["path"]).name for f in files}, "library_conflict"
            )
            for f in files:
                with safe_reader(library, f["path"]) as already:
                    st = os.fstat(already.fileno())
                    check(
                        stat.S_ISREG(st.st_mode)
                        and st.st_size == f["size"]
                        and hashlib.file_digest(already, "sha256").hexdigest() == f["sha256"],
                        "library_conflict",
                    )
        else:
            if os.name != "nt":
                fd = os.open(ready, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            ready.rename(final)
            if os.name != "nt":
                fd = os.open(library, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return files
