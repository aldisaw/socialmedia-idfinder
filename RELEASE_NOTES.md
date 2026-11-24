# Release Notes — Social ID Finder v1.1.0

**Release Date:** 2026-11-24  
**Release Type:** Security-Hardened Release (Windows 64-bit Desktop Application)  
**Official Distribution:** **Browser Edition** (`ONEDIR` ZIP and `ONEFILE` EXE)

---

## What's New in v1.1.0

### Extraction Reliability Upgrade (All 5 Platforms)
- **X / Twitter SSR Extraction (Method A — HTTPX):** Parses X's Relay/TanStack SSR streaming payloads (`self.$R` / `self.$_TSR`) to extract verified `User` `rest_id` while strictly excluding Tweet/Post IDs.
- **Playwright Chromium Fallback (Method B):** Stateless headless Chromium (`chromium_headless_shell`) automatically renders JavaScript-heavy public profiles when HTTPX alone returns an unhydrated client-side shell. Zero login, zero imported browser cookies, zero stealth evasion, and zero CAPTCHA bypass.
- **Instagram Authoritative Profile Extraction:** Separates canonical profile-root numeric IDs (`PolarisProfile.props.id`, `profile_id`, `pk`) from post-level 17-digit `178414...` Graph FBID stubs.
- **TikTok Hydration & Identifier Integrity:** Parses `__UNIVERSAL_DATA_FOR_REHYDRATION__` with `secUid` strictly labeled `SEC_UID` and never misclassified as `NUMERIC_USER_ID`.
- **Facebook Sequential Public Extraction:**
  - `profile.php?id=<digits>` and numeric profile URLs (`facebook.com/<numeric_id>`) extract IDs as `URL_EXTRACTED`, strictly capped at `MEDIUM` / `PARTIALLY_VERIFIED`.
  - Vanity URLs try HTTPX first, then Playwright Chromium; root-profile `selectedID`/`userID` payloads (excluding posts, photos, comments, stories, feedback, attachments, and Page objects) are recorded as `HTML_JSON_EXTRACTED` or `BROWSER_EXTRACTED` under evidence validation.
  - Login redirects and login walls report `LOGIN_REQUIRED`; CAPTCHA interstitials report `CAPTCHA_REQUIRED`; genuine unrelated HTTP 400 errors remain `ERROR`.
- **Unified Evidence Model & SQLite v2 Migration:** Added `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, `URL_EXTRACTED`, `HTML_JSON_EXTRACTED`, `BROWSER_EXTRACTED`, `acquisition_method`, `response_status`, and `final_url` across `SearchResult`, CSV/JSON/TXT exports, GUI filters, and automatic non-destructive SQLite `v1` → `v2` migration.

### Security & Reliability Hardening
- **SSRF & DNS Rebinding Protection (HTTPX + Playwright):** Blocks `localhost`, `.local`/`.internal` hosts, loopback, private, link-local/metadata (`169.254.169.254`), multicast, reserved, unspecified, and obfuscated numeric IPv4 addresses, embedded URL credentials, non-80/443 ports, and non-HTTP(S) schemes across initial URLs, every manual HTTP redirect hop, and every Playwright navigation, redirect, iframe, `fetch`, `XHR`, and subresource. `_SSRFSafeNetworkBackend` pins validated public IPs at TCP connect time while preserving native TLS SNI verification.
- **Playwright Isolation & Resource Safety:** Ephemeral browser context (`accept_downloads=False`, `service_workers="block"`, `ignore_https_errors=False`), immediate popup closure, heavy asset blocking (`image`, `media`, `font`), max 2 concurrent Chromium instances, and shielded cancellation-safe shutdown to prevent orphan Chromium processes.
- **CSV Formula Injection Neutralization & Atomic Exports:** Neutralizes leading `=`, `+`, `-`, `@`, tab, CR, and LF in CSV cells only (JSON/TXT untouched); validates target paths (rejects Windows reserved device names, directories, symlinks, extension mismatches, DB overwrites) and writes atomically via temp file + `fsync` + `os.replace`.
- **Data, Cookie, Backup & Logger Hardening:** Stateless zero-cookie concurrent HTTPX client; 10 MB HTTP/DOM response cap; WAL-consistent SQLite Online Backup API (`Connection.backup`); expanded log redaction (URL credentials, `Basic`/`Bearer`/`Digest`/`Token` auth, session tokens, exception objects, tracebacks, CRLF); normalizer rejection of URL credentials, non-standard ports, IP literals, and YouTube `..` traversal; User-Agent sanitization; cooldown re-check inside semaphore and preservation across settings saves.

---

## Official Release Assets (Browser Edition Only)

| File | Size | Description |
| :--- | ---: | :--- |
| **`SocialIDFinder_Browser_ONEDIR.zip`** | **216.39 MB** ZIP (**505.67 MB** extracted, 706 files) | Recommended full directory package bundling PySide6, HTTPX, Playwright driver, and Chromium headless shell. Extract all files and run `SocialIDFinder.exe` inside the folder. |
| **`SocialIDFinder_Browser.exe`** | **209.46 MB** | Portable single-file executable with bundled Chromium runtime. First startup takes several seconds during temporary unpacking. |
| **`SHA256SUMS.txt`** | — | SHA-256 checksums for all published release files. |
| **`RELEASE_NOTES.md`** | — | This document. |
| **`USER_GUIDE.md`** | — | End-user installation, usage, and troubleshooting guide. |

Verify downloads against `SHA256SUMS.txt` before running:
```powershell
Get-FileHash .\SocialIDFinder_Browser_ONEDIR.zip -Algorithm SHA256
Get-FileHash .\SocialIDFinder_Browser.exe -Algorithm SHA256
```

---

## Verification Summary & Notices

- **Tests:** **284 passed** offline unit, integration, packaging, and security tests (`pytest -q`).
- **Runtime Storage:** All writable data (SQLite DB/WAL, logs, backups, default exports) writes strictly to `%APPDATA%\Social ID Finder\`.
- **Unsigned Executables (SmartScreen):** Binaries are not Authenticode code-signed. Verify SHA-256 checksums and download source before approving SmartScreen prompts.
- **Clean Windows VM Verification Pending:** Isolated clean-folder tests passed on Windows 11 x64 with Python/`.venv` stripped from `PATH`; independent verification on a separate clean Windows VM has not yet been performed.
