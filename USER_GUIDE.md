# Social ID Finder v1.1.0 — User Guide

## 1. Official Packages & Installation

Social ID Finder v1.1.0 is distributed as **Windows 64-bit Browser Edition**, bundling PySide6, HTTPX, Playwright, and Chromium headless shell. **No Python installation is required.**

| Package | Size | Best For |
| :--- | ---: | :--- |
| **`SocialIDFinder_Browser_ONEDIR.zip`** | 216.39 MB ZIP (505.67 MB extracted) | **Recommended** for daily use — fast startup after extracting the complete folder. |
| **`SocialIDFinder_Browser.exe`** | 209.46 MB | Portable single-file binary — slower startup while unpacking bundled Chromium to temp storage. |

### Installing ONEDIR
1. Download `SocialIDFinder_Browser_ONEDIR.zip` and `SHA256SUMS.txt` from [GitHub Releases](https://github.com/aldisaw/socialmedia-idfinder/releases).
2. Verify SHA-256: `Get-FileHash .\SocialIDFinder_Browser_ONEDIR.zip -Algorithm SHA256`.
3. Extract the entire ZIP archive. **Do not move `SocialIDFinder.exe` out of its folder** (`_internal/` must stay beside it).
4. Run `SocialIDFinder.exe`.

### Running ONEFILE
1. Download `SocialIDFinder_Browser.exe` and `SHA256SUMS.txt`.
2. Verify SHA-256: `Get-FileHash .\SocialIDFinder_Browser.exe -Algorithm SHA256`.
3. Run `SocialIDFinder_Browser.exe` and wait a few seconds for startup unpacking.

### Windows SmartScreen Notice
Executables are **not Authenticode-signed**. Confirm the download source and verify the SHA-256 hash against `SHA256SUMS.txt` before selecting **More info → Run anyway**.

### Runtime Data Location
All writable data is stored strictly under `%APPDATA%\Social ID Finder\` (never inside the executable folder):
- `social_id_finder.db` (+ `-wal` / `-shm`) — SQLite schema v2 history, batch sessions, batch items, and settings
- `logs\app.log` — Rotating diagnostic log (credentials, auth headers, session tokens, and CRLF redacted)
- `exports\` — Default export folder for CSV, JSON, and TXT files
- `*.bak` — WAL-consistent SQLite backups

---

## 2. Single Search

1. Click **Single Search** in the left sidebar.
2. Paste a profile URL (keep `Auto-detect from URL`) or enter a username and select a platform manually.
3. Click **Search** (click **Stop** or **Stop All Tasks** to cancel).
4. Method A (**HTTPX**) runs first. On Instagram, TikTok, and Facebook vanity URLs, if HTTPX cannot obtain a verified public ID and **Browser fallback** is enabled, Method B (**Playwright Chromium**) runs automatically.
5. For Facebook numeric URLs (`profile.php?id=<digits>` or `facebook.com/<numeric_id>`), the ID is recorded as `URL_EXTRACTED` and strictly capped at `MEDIUM` / `PARTIALLY_VERIFIED`.
6. Review **Latest Result** and click **Copy User ID** if found.

---

## 3. Batch Search (Maximum 10 Entries)

1. Click **Batch Search** in the sidebar.
2. Enter up to **10** mixed profile URLs or usernames (one per line). Canonical duplicates are deduplicated offline.
3. Optionally set `Default platform for plain usernames:` (default `URL Auto-detect Only`).
4. Click **Start Batch** and monitor live counters, active platform/strategy, and `Elapsed:` time.
5. Click **Stop All** to cancel cleanly while preserving completed results.
6. Click **Retry Failed** to re-run eligible failed rows (`ERROR`, `TIMEOUT`, `CANCELLED`, `RATE_LIMITED`) without overwriting previously `FOUND` rows.
7. **Batch Recovery:** Interrupted batches from an unexpected exit are marked `INTERRUPTED` on restart (never auto-started). Use `Saved Sessions:` → **Load** → **Resume Batch** to continue unfinished entries.

---

## 4. Results, History & Exports

- **Results:** Filter by text, status, or confidence; sort rows; click **Copy ID** or **Copy Selected IDs**; choose export scope (`Filtered`, `Selected`, or `All`) and click **Export CSV**, **Export JSON**, or **Export TXT**.
- **History:** Browse persisted SQLite records with filters (`All Platforms`, `All Statuses`, `All Confidence`, `Date: Newest`/`Oldest`, text search); **Refresh**; **Delete Selected**; **Clear History**; or export filtered history.
- **Export Hardening:** Exports use UTF-8, path validation, atomic file replacement, and credential redaction. CSV cells starting with `=`, `+`, `-`, `@`, tab, CR, or LF are prefixed with `'` to prevent spreadsheet formula injection (JSON and TXT remain unmodified).

---

## 5. Settings

Open **Settings** to configure and persist non-secret preferences:
- `Per-request timeout:` (default `15.0 s`), `Per-item timeout:` (default `30.0 s`), `Batch timeout:` (default `60.0 s`)
- `Max concurrent requests (1-10):` (default `3`), `Transient retry limit (max 1):` (default `1`), `Rate-limit cooldown:` (default `15.0 s`)
- `User-Agent:` (automatically stripped of control/CRLF characters)
- `Browser fallback:` toggle & live `Browser runtime:` status (`ENABLED` in Browser Edition)
- `Debug mode:` verbose strategy logging (sensitive values always redacted)
- Click **Save Settings** or **Reset to Defaults**.

---

## 6. Backup & Restore

- Automated pre-migration backups use SQLite's WAL-consistent Online Backup API.
- For manual backup, close Social ID Finder and copy `%APPDATA%\Social ID Finder\social_id_finder.db`.
- To restore, close the app, replace `social_id_finder.db` (removing stale `-wal`/`-shm` files), and relaunch.

---

## 7. Troubleshooting

- **`LOGIN_REQUIRED`:** Platform requires login; authentication is never bypassed. For Facebook numeric profiles, use `facebook.com/profile.php?id=<NUMERIC_ID>`.
- **`CAPTCHA_REQUIRED` / `RATE_LIMITED`:** Wait for cooldown and lower `Max concurrent requests` in **Settings**.
- **`BLOCKED`:** HTTP 403 restriction or SSRF policy blocked an unsafe redirect/non-HTTP(S) URL.
- **`TIMEOUT`:** Increase timeouts in **Settings** and retry.
- **Export Error:** Ensure target filename matches `.csv`/`.json`/`.txt` and is not a directory, symlink, database file, or Windows reserved name.
- **Corrupted Database Dialog:** Original `.db` file is never overwritten. Close the app, move `%APPDATA%\Social ID Finder\social_id_finder.db`, and restart.
- **Launch Error on Fresh Windows:** Ensure the full `ONEDIR` folder was extracted intact and install Microsoft Visual C++ Redistributable (x64) if `vcruntime140.dll` is missing.
