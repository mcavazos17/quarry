# Cadence requests

The listener searches, chooses a release and plays it in Cadence. Cadence sends a private, authenticated request to the audio service in this fork. Its Python worker runs separately from Quarry's Seerr service and uses the existing Prowlarr and qBittorrent Web APIs. Movies and television continue through the Bun service.

## Albums

Each album request is keyed by its MusicBrainz release-group ID. The catalog can search by artist or album, or resolve an exact ID. It falls back to the public Lidarr metadata mirror during a MusicBrainz outage. Both sources use MusicBrainz identifiers. A local Lidarr installation is optional for new Quarry requests.

Quarry persists the request before looking up the edition. It checks the music category and indexer, submits one matching torrent with an explicit save path, and polls that transfer across restarts. It verifies the torrent pieces, audio tags and ordered track list before publishing the album. An edition that cannot be matched goes to review. The original download stays in qBittorrent for seeding. Cadence separately scans the published album and resolves its playable tracks.

## Run the private service

### Audiobooks

Listeners can search a configured Prowlarr audiobook indexer and select a particular edition from Cadence. Each selected result gets its own durable ID. Book downloads use a separate `quarry-audiobooks` qBittorrent category and a dedicated publication directory. Cadence indexes the original files and opens the book in its chapter player.

Set all four book variables to enable these endpoints: `QUARRY_BOOK_INDEXER_ID`, `QUARRY_BOOK_SAVE_PATH`, `QUARRY_BOOK_DOWNLOADS`, and `QUARRY_BOOK_LIBRARY`. Provide the same directory access described for music, using separate download and library folders. Set up the book category with the exact save path configured here. The service shares the existing private state directory and Prowlarr/qBittorrent connections between its workers. Music-only installations can omit all four book variables.

### Container

Build from the repository root using `docker build -f audio/Dockerfile -t quarry-audio:local .`. Attach the container to a private network shared with Cadence. The service listens on port 8088 inside its container. Mount the following directories and files. The service user needs read access to the completed downloads, write access to state and the publication parent, and read access to the secret files.

| Environment variable | Value |
| --- | --- |
| `QUARRY_AUDIO_STATE` | Private writable directory for the request databases |
| `QUARRY_MUSIC_DOWNLOADS` | Read-only mount of the qBittorrent music category folder |
| `QUARRY_MUSIC_LIBRARY` | Dedicated music library directory under a writable publication parent. Mount only this directory read-only into Cadence. |
| `QUARRY_MUSIC_SAVE_PATH` | Absolute category directory as seen by qBittorrent. Create a category named `quarry-music` with this save path. |
| `PROWLARR_URL`, `QUARRY_MUSIC_INDEXER_ID` | Private Prowlarr origin and numeric music indexer ID |
| `QUARRY_MUSIC_EXTRA_INDEXER_IDS` | Optional comma-separated Prowlarr IDs for additional album sources. Music searches include the primary and additional IDs. Audiobook searches use their separately configured indexer. |
| `QUARRY_BOOK_GENERAL_CATEGORIES` | On a worker dedicated to a tracker without an audiobook category, set to `true` to search Audio and Books. Results outside the audiobook category must be explicitly labeled as spoken editions. Payloads still pass the audiobook file checks before download. |
| `QBIT_URL` | Private qBittorrent Web UI origin |
| `QUARRY_PROWLARR_KEY_FILE` | Mounted file containing the Prowlarr API key |
| `QUARRY_QBIT_AUTH_FILE` | Mounted private JSON file containing `username` and `password`, or `apiKey` |
| `QUARRY_AUDIO_KEY_FILE` | Mounted file with the private API key shared with Cadence |

State, downloads and library directories must already exist. The publication parent and library must be on the same filesystem. Provision the secret files privately on the operator's host and mount them read-only. The included image runs as UID/GID 1654 by default.

Set `StreamBox__MusicRequests__Quarry__Enabled=true` in Cadence and set the provider's `Origin`, `SecretFile` and `RootId` or registered `LibraryPath`. Keep the existing Lidarr provider configured until its existing requests have finished. Cadence selects Quarry for new album requests. Existing requests keep their recorded provider.

## Private API

All request endpoints require `X-Api-Key`. `GET /health` is available for a private container health check.

| Method and endpoint | Response |
| --- | --- |
| `GET /api/music/search?q=...&offset=0` | Up to 20 release groups, with a next offset when more results are available |
| `POST /api/music/requests/{release-group-id}` | Accept an album, or return the current status of an existing request. The body can be empty or `{}`. |
| `GET /api/music/requests/{release-group-id}` | Current state. A completed import includes ordered root-relative paths, byte sizes and SHA-256 digests. |
| `GET /api/books/search?q=...&offset=0` | Selectable audiobook editions and their sizes |
| `POST /api/books/requests/{edition-id}` | Queue the selected edition or return its existing receipt |
| `GET /api/books/requests/{edition-id}` | Current state and the ordered files of a completed import |

Run `python -m unittest discover -s audio` with FFmpeg and ffprobe available. The tests use original fixtures and temporary directories.

### Dedicated TorrentLeech sources

Set `QUARRY_TL_INDEXER_ID`, `QUARRY_TL_PROWLARR_URL`, `QUARRY_TL_PROWLARR_KEY_FILE`,
`QUARRY_TL_QBIT_URL`, `QUARRY_TL_QBIT_AUTH_FILE`, `QUARRY_TL_DOWNLOADS` and
`QUARRY_TL_ACCOUNT_FILE` together. Use an isolated Prowlarr endpoint so unrelated applications cannot grab
TorrentLeech results through the shared client. Omit the TorrentLeech ID from the shared album indexer list.
Mount the dedicated download tree read-only in the worker. On the dedicated client configure categories
`quarry-music` at `/downloads/quarry-music` and `quarry-audiobooks` at `/downloads/quarry-audiobooks`.

Albums try the primary tracker first. A transfer keeps its selected client across restarts.
Audiobook searches combine editions from both sources. TorrentLeech results in the general music or book
categories must identify a spoken edition, and its torrent must pass the file manifest check.

The account file is a read-only input from the operator's trusted account verification process.
Required fields are `tracker` (torrentleech), `observedAt` and `waitUntil` (Unix seconds),
`uploadedBytes`, `downloadedBytes`, `availableSlots` and `downloadAllowed`. Missing or expired
evidence leaves the request queued. A confirmation can admit one transfer. The shared reservation
database accounts conservatively for recent debits across music and books. The worker checks the dedicated
client's peer-discovery and seeding settings before dispatch. Keep seeding after import.
