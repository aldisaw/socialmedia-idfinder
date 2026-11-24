# Changelog

All notable changes to **Social ID Finder** are documented in this file.

## [1.1.0] - 2026-11-24

### Added
- **X / Twitter SSR Extraction (Method A — HTTPX):** Parses X's `self.$R` / `self.$_TSR` Relay escaped-JSON and TanStack profile blocks while strictly excluding Tweet/Post `rest_id` values.
- **Playwright Chromium Fallback (Method B):** Added `core/browser_client.py` with capability detection (`ENABLED`/`DISABLED`), bounded navigation timeout and DOM wait, ephemeral isolated context, deterministic cleanup, and browser fallback for Instagram, TikTok, and Facebook vanity profiles.
- **Facebook Public Multi-Stage Extraction:** Sequential strategy supporting `profile.php?id=<numeric_id>` and numeric profile URLs (`URL_EXTRACTED`, strictly capped at `MEDIUM`/`PARTIALLY_VERIFIED`) plus vanity URLs via HTTPX → Playwright (`HTML_JSON_EXTRACTED` / `BROWSER_EXTRACTED` from root-profile `selectedID`/`userID`, excluding posts, photos, comments, stories, feedback, attachments, and Page objects).
- **Unified Evidence & Diagnostic Statuses:** Added `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, `URL_EXTRACTED`, `HTML_JSON_EXTRACTED`, and `BROWSER_EXTRACTED` plus `acquisition_method`, `response_status`, and `final_url` across `SearchResult`, SQLite `v1` → `v2` migration, CSV/JSON/TXT exports, and GUI filters.
- **Official Windows Browser Edition Distributions:** Reproducible PyInstaller builds for `SocialIDFinder_Browser_ONEDIR.zip` and `SocialIDFinder_Browser.exe` bundling Playwright driver and Chromium headless shell (`chromium_headless_shell`).

### Fixed
- **Facebook Login Redirect & HTTP 400 Handling:** Inspects redirect history and final URL before generic HTTP 400 error mapping so `302 -> /login/?next=...` (HTTP 400) reports `LOGIN_REQUIRED` instead of `ERROR`, while genuine unrelated HTTP 400 remains `ERROR`; Facebook vanity URLs receiving direct HTTP 400 from raw HTTPX fall back to Playwright when enabled.
- **Instagram Graph FBID Separation:** Separates authoritative profile-root IDs (`PolarisProfile.props.id`, `profile_id`, `pk`) from post-level 17-digit `178414...` Graph FBID stubs.

### Security & Hardening
- **HTTPX SSRF & DNS Rebinding Defense:** Manual hop-by-hop redirect validation (`follow_redirects=False`), blocking of `localhost`, private/loopback/link-local/multicast/reserved/unspecified IPs and non-HTTP(S) schemes, and TCP-level IP pinning via `_SSRFSafeNetworkBackend`.
- **Playwright Context & Route Isolation:** Context-wide `route("**/*")` SSRF validation across navigations, redirects, iframes, `fetch`, `XHR`, and subresources; `accept_downloads=False`; `service_workers="block"`; immediate popup closure; heavy `image`/`media`/`font` asset blocking; browser concurrency capped at `2`; shielded cancellation-safe cleanup.
- **CSV Formula Injection & Atomic Exports:** Neutralizes leading `=`, `+`, `-`, `@`, `\t`, `\r`, `\n` in CSV cells only (JSON/TXT unmodified); validates export paths (rejects Windows reserved device names, directories, symlinks, extension mismatches, DB overwrites) with atomic temp-file replacement.
- **Cookie, Backup, Logger & Input Hardening:** Stateless `_NoOpCookies` eliminating concurrent cookie leakage; WAL-consistent SQLite Online Backup API (`sqlite3.Connection.backup`); expanded log redaction (URL credentials, `Basic`/`Bearer`/`Digest`/`Token` auth, session cookies/tokens, exceptions/tracebacks, CRLF); normalizer rejection of URL credentials, non-standard ports, IP literals, and YouTube `..` traversal; 10 MB HTTP/DOM response cap; `User-Agent` CRLF/length sanitization; cooldown re-check inside semaphore and preservation across settings saves; startup/shutdown logger lifecycle fixes.

## [1.0.0] - 2026-11-24

### Added
- **Native Windows Desktop GUI (PySide6):** Dark-mode interface with Dashboard, Single Search, Batch Search, Results, History, Settings, Live Activity Log, multi-resolution Windows icon, and About dialog.
- **Offline Input Normalization & Platform Detection:** Strict `urlsplit` domain matching, mobile/www/alias normalization, tracking parameter stripping, reserved-path rejection, bare-username `AMBIGUOUS_INPUT` handling, and canonical batch deduplication across Instagram, TikTok, X (Twitter), Facebook, and YouTube.
- **Multi-Strategy Extraction Engine:** Ordered 4-priority pipeline (Embedded JSON → Structured Metadata → HTML Attributes → Identity-Paired Regex Fallback) via centralized `httpx.AsyncClient` with pooling, `Retry-After` compliance, max 1 transient retry, and zero cookie persistence.
- **Concurrent Batch Processing:** Bounded worker pool (`asyncio.Queue` + `asyncio.Semaphore`, 1–10 concurrency), separate per-request (15s), per-item (30s), and batch (60s) timeouts, per-platform HTTP 429 cooldown tracker, graceful `Stop All` cancellation, live counters, and `Retry Failed`.
- **Validation, Confidence Scoring & Export Hardening:** Strict per-platform ID format & type validation (`NUMERIC_USER_ID`, `NUMERIC_PROFILE_ID`, `CHANNEL_ID`, `SEC_UID`), evidence-based `HIGH`/`MEDIUM`/`LOW`/`NOT_VERIFIED` scoring, duplicate ID detection without merging accounts, Results filtering/sorting/Copy Selected IDs, and UTF-8 credential-redacted CSV/JSON/TXT exports.
- **SQLite Persistence & Crash Recovery:** AppData-isolated `social_id_finder.db` in WAL mode with foreign keys, schema versioning, pre-migration backups, corruption protection, persistent history/settings, and interrupted batch detection & manual resume.
