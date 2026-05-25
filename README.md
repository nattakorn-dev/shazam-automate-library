# shazam-automate-library
Automate thai music library with shazam

[Raw Audio Intake] ➡️  /music/watch    (Intake & Claim Phase)

⬇️

[Shazam ID & Repair] ➡️  Processes metadata, fixes Thai text, embeds covers

⬇️

[Successful Match]  ➡️  /music/library  (Final organized library structure)

[Match Failed/Bad]  ➡️  /music/unmanage (Requires manual inspection)

### 📂 Directory Mapping
* **`/music/watch` (`WATCH_DIR`)**: The landing zone where new, unmanaged audio files (`.mp3`, `.m4a`, `.flac`, `.wav`) are placed. 
* **`/music/library` (`TAG_DIR`)**: The primary destination. Successfully identified tracks are tagged with pristine UTF-8 metadata and moved here under clean `Artist/Album/Track.ext` paths.
* **`/music/unmanage` (`UNMANAGE_DIR`)**: The fallback zone. Files with corrupted headers, empty audio data, or tracks unmatched by Shazam after retries are safely isolated here.

---

## ⚙️ Core Service Components

### 1. `tagger_service.py`
The backbone of this project is an asynchronous Python daemon running continuously to inspect the intake directory. It features:
* **Concurrent Execution with Safe Locking**: Processes up to 3 files concurrently using an `asyncio.Semaphore`. It securely appends a `.processing` suffix to filenames to "claim" tokens, ensuring multi-node setups or separate processes don't collision on the same file.
* **Advanced Thai Encoding Auto-Repair**: Automatically catches and fixes classic Mojibake translation issues (e.g., `à¸žà¸‡à¸©à¹Œà¸ªà¸´à¸—à¸˜à¸´à¹Œ` $\\rightarrow$ `พงษ์สิทธิ์ คำภีร์`), supporting both `UTF-8 inside Latin-1` and `Windows-874 / CP874` text recoveries.
* **Shazam API Rate-Limit Protection**: Embeds an `AsyncRateLimiter` token window (defaulting to a `1.5s` delay with exponential backoff on HTTP 429 errors) to ensure your IP remains clean and untargeted by rate restrictions.
* **Smart Deduplication & Upgrade Logic**: If a track already exists in the `/music/library` pool, the service checks and compares both audio bitrates and file sizes. It will automatically overwrite the existing file if the incoming track is of higher quality, otherwise it skips to preserve space.

### 2. `docker-compose.yml` (The Stack Deployment)
A lightweight stack footprint utilizing highly optimized base images to compile dependencies natively on startup.

```yaml
version: "3.8"

services:
  shazam-tagger:
    image: ghcr.io/nattakorn-dev/shazam-automate-library:latest
    container_name: music_tagger
    restart: unless-stopped
    volumes:
      - path_to_watch:/music/watch
      - path_to_lib:/music/library
      - path_to_unmanage:/music/unmanage


