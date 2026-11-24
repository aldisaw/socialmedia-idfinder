"""Offline tests for V2 Extraction Reliability Upgrade (Method B Playwright, statuses, v2 evidence)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from config import AppSettings
from core.browser_client import BrowserCapability, BrowserClient, check_browser_capability
from core.engine import ExtractionEngine
from core.http_client import HttpClient
from core.models import (
    AcquisitionMethod,
    Confidence,
    ExtractionMethod,
    IdType,
    Platform,
    ResultStatus,
    ResultValidationStatus,
)
from core.normalizer import normalize_input
from database import CURRENT_SCHEMA_VERSION, DatabaseManager, HistoryRepository
from exports.exporter import EXPORT_FIELDS, export_results


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _FakePage:
    def __init__(
        self,
        *,
        html: str = "",
        final_url: str = "",
        status: int = 200,
        delay: float = 0.0,
        raise_exc: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self._html = html
        self.url = final_url
        self._status = status
        self._delay = delay
        self._raise_exc = raise_exc
        self._events = events if events is not None else []

    def on(self, _event: str, _cb) -> None:
        pass

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 15000):
        self._events.append("page.goto")
        if not self.url:
            self.url = url
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse(status=self._status)

    async def wait_for_selector(self, selector: str, state: str = "attached", timeout: int = 2500):
        self._events.append("page.wait_for_selector")

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self._events.append("page.close")


class _FakeContext:
    def __init__(self, page: _FakePage, events: list[str]) -> None:
        self._page = page
        self._events = events

    async def new_page(self) -> _FakePage:
        return self._page

    async def close(self) -> None:
        self._events.append("context.close")


class _FakeBrowser:
    def __init__(self, page: _FakePage, events: list[str]) -> None:
        self._page = page
        self._events = events

    async def new_context(self, **kwargs) -> _FakeContext:
        return _FakeContext(self._page, self._events)

    async def close(self) -> None:
        self._events.append("browser.close")


class _FakeChromium:
    def __init__(self, page: _FakePage, events: list[str]) -> None:
        self._page = page
        self._events = events

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        self._events.append("chromium.launch")
        return _FakeBrowser(self._page, self._events)


class _FakePlaywright:
    def __init__(self, page: _FakePage, events: list[str]) -> None:
        self.chromium = _FakeChromium(page, events)
        self._events = events

    async def start(self) -> "_FakePlaywright":
        self._events.append("pw.start")
        return self

    async def stop(self) -> None:
        self._events.append("pw.stop")


def _make_mock_browser(
    *,
    html: str = "",
    final_url: str = "",
    status: int = 200,
    delay: float = 0.0,
    raise_exc: Exception | None = None,
    events: list[str] | None = None,
) -> BrowserClient:
    ev = events if events is not None else []
    page = _FakePage(html=html, final_url=final_url, status=status, delay=delay, raise_exc=raise_exc, events=ev)
    return BrowserClient(
        timeout=5.0,
        render_wait=0.0,
        playwright_factory=lambda: _FakePlaywright(page, ev),
    )


# ---------------------------------------------------------------------------
# 1. Playwright capability checks & graceful fallback when missing
# ---------------------------------------------------------------------------

def test_browser_capability_missing_playwright_and_missing_chromium() -> None:
    no_pw = check_browser_capability(_has_playwright=False)
    assert no_pw.status == "DISABLED"
    assert no_pw.available is False
    assert "pip install playwright" in no_pw.reason

    no_chr = check_browser_capability(_has_playwright=True, _chromium_path="")
    assert no_chr.status == "DISABLED"
    assert no_chr.available is False
    assert "playwright install chromium" in no_chr.reason

    ok = check_browser_capability(_has_playwright=True, _chromium_path="C:\\fake\\chrome.exe")
    assert ok.status == "ENABLED"
    assert ok.available is True


async def test_engine_gracefully_handles_missing_browser_capability() -> None:
    disabled_browser = BrowserClient(
        capability=BrowserCapability(status="DISABLED", available=False, reason="Playwright missing")
    )
    http_client = HttpClient(
        timeout=5.0,
        retry_limit=0,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html><title>Instagram</title><body>csr shell</body></html>")),
    )
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)
    async with ExtractionEngine(settings=settings, http_client=http_client, browser_client=disabled_browser) as eng:
        res = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await http_client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.acquisition_method == AcquisitionMethod.HTTP.value


# ---------------------------------------------------------------------------
# 2. Instagram Method B: rendered profile, missing ID, wrong user, login, CAPTCHA
# ---------------------------------------------------------------------------

async def test_instagram_browser_fallback_extracts_polaris_profile_id() -> None:
    events: list[str] = []
    rendered_html = """
    <html><head><title>Instagram (@instagram) • Instagram photos and videos</title></head><body>
      <script type="application/json">
        {
          "posts": [{"user": {"id": "17841400039600391", "username": "instagram"}}],
          "resource": {"__dr": "PolarisProfilePostsTabRoot.react"},
          "props": {"id": "25025320", "number_of_preloaded_posts": 12}
        }
      </script>
    </body></html>
    """
    http_client = HttpClient(
        timeout=5.0,
        retry_limit=0,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html><title>Instagram</title><body>shell</body></html>")),
    )
    browser = _make_mock_browser(
        html=rendered_html,
        final_url="https://www.instagram.com/instagram",
        status=200,
        events=events,
    )
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)
    async with ExtractionEngine(settings=settings, http_client=http_client, browser_client=browser) as eng:
        res = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await http_client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "25025320"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.acquisition_method == AcquisitionMethod.BROWSER.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.confidence == Confidence.HIGH.value
    assert res.response_status == 200
    assert events[-4:] == ["page.close", "context.close", "browser.close", "pw.stop"]


async def test_instagram_browser_wrong_username_login_and_captcha() -> None:
    http_shell = lambda req: httpx.Response(200, text="<html><title>Instagram</title><body>shell</body></html>")
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)

    # Wrong username in rendered Polaris profile
    wrong_html = """
    <html><head><title>Other (@other_acct) • Instagram</title></head><body>
      <script type="application/json">{"resource":{"__dr":"PolarisProfilePostsTabRoot.react"},"props":{"id":"999999"}}</script>
    </body></html>
    """
    hc1 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_shell))
    b1 = _make_mock_browser(html=wrong_html, final_url="https://www.instagram.com/other_acct")
    async with ExtractionEngine(settings=settings, http_client=hc1, browser_client=b1) as eng:
        r_wrong = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await hc1.aclose()
    assert r_wrong.status == ResultStatus.NOT_FOUND.value
    assert r_wrong.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value

    # Login wall in browser
    hc2 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_shell))
    b2 = _make_mock_browser(
        html="<html><head><title>Login • Instagram</title></head><body>Log in to Instagram</body></html>",
        final_url="https://www.instagram.com/accounts/login/?next=/instagram",
    )
    async with ExtractionEngine(settings=settings, http_client=hc2, browser_client=b2) as eng:
        r_login = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await hc2.aclose()
    assert r_login.status == ResultStatus.LOGIN_REQUIRED.value
    assert r_login.acquisition_method == AcquisitionMethod.BROWSER.value

    # CAPTCHA wall in browser
    hc3 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_shell))
    b3 = _make_mock_browser(
        html="<html><head><title>Security Check</title></head><body>Verify you are human captcha-verify</body></html>",
        final_url="https://www.instagram.com/instagram",
    )
    async with ExtractionEngine(settings=settings, http_client=hc3, browser_client=b3) as eng:
        r_cap = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await hc3.aclose()
    assert r_cap.status == ResultStatus.CAPTCHA_REQUIRED.value


# ---------------------------------------------------------------------------
# 3. TikTok Method B: user.id, secUid distinction, wrong uniqueId, CAPTCHA
# ---------------------------------------------------------------------------

async def test_tiktok_browser_fallback_numeric_id_and_sec_uid_distinction() -> None:
    http_loader = lambda req: httpx.Response(200, text="<html><body><script id='slardar-config'>{}</script></body></html>")
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)

    # Full hydration with numeric id + secUid -> prefers NUMERIC_USER_ID
    tt_html = """
    <html><head><title>TikTok - Make Your Day</title></head><body>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {"__DEFAULT_SCOPE__":{"webapp.user-detail":{"userInfo":{"user":{"id":"107955","uniqueId":"tiktok","secUid":"MS4wLjABAAAA_secuid_01234567890"}}}}}</script>
    </body></html>
    """
    hc1 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_loader))
    b1 = _make_mock_browser(html=tt_html, final_url="https://www.tiktok.com/@tiktok")
    async with ExtractionEngine(settings=settings, http_client=hc1, browser_client=b1) as eng:
        r_num = await eng.extract(normalize_input("https://www.tiktok.com/@tiktok"))
    await hc1.aclose()
    assert r_num.status == ResultStatus.FOUND.value
    assert r_num.user_id == "107955"
    assert r_num.id_type == IdType.NUMERIC_USER_ID.value
    assert r_num.acquisition_method == AcquisitionMethod.BROWSER.value
    assert r_num.confidence == Confidence.HIGH.value

    # Only secUid present -> strictly labeled SEC_UID
    tt_sec_only = """
    <html><head><title>TikTok - Make Your Day</title></head><body>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {"__DEFAULT_SCOPE__":{"webapp.user-detail":{"userInfo":{"user":{"uniqueId":"tiktok","secUid":"MS4wLjABAAAA_only_secuid_0123456789"}}}}}</script>
    </body></html>
    """
    hc2 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_loader))
    b2 = _make_mock_browser(html=tt_sec_only, final_url="https://www.tiktok.com/@tiktok")
    async with ExtractionEngine(settings=settings, http_client=hc2, browser_client=b2) as eng:
        r_sec = await eng.extract(normalize_input("https://www.tiktok.com/@tiktok"))
    await hc2.aclose()
    assert r_sec.status == ResultStatus.FOUND.value
    assert r_sec.id_type == IdType.SEC_UID.value
    assert r_sec.id_type != IdType.NUMERIC_USER_ID.value

    # TikTok CAPTCHA page
    hc3 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(http_loader))
    b3 = _make_mock_browser(
        html="<html><head><title>Verify to continue</title></head><body><div class='tiktok-verify-page'></div></body></html>",
        final_url="https://www.tiktok.com/@tiktok",
    )
    async with ExtractionEngine(settings=settings, http_client=hc3, browser_client=b3) as eng:
        r_cap = await eng.extract(normalize_input("https://www.tiktok.com/@tiktok"))
    await hc3.aclose()
    assert r_cap.status == ResultStatus.CAPTCHA_REQUIRED.value


# ---------------------------------------------------------------------------
# 4. Browser timeout, cancellation & guaranteed resource cleanup
# ---------------------------------------------------------------------------

async def test_browser_timeout_and_cancellation_cleanup() -> None:
    events_timeout: list[str] = []
    b_slow = _make_mock_browser(delay=2.0, events=events_timeout)
    r_to = await b_slow.fetch_rendered("https://www.tiktok.com/@tiktok", timeout=0.1)
    assert r_to.error_type == "TIMEOUT"
    assert events_timeout[-4:] == ["page.close", "context.close", "browser.close", "pw.stop"]

    events_cancel: list[str] = []
    b_cancel = _make_mock_browser(delay=5.0, events=events_cancel)
    task = asyncio.create_task(b_cancel.fetch_rendered("https://www.tiktok.com/@tiktok", timeout=10.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events_cancel[-4:] == ["page.close", "context.close", "browser.close", "pw.stop"]


# ---------------------------------------------------------------------------
# 5. SQLite v1 -> v2 migration + unified evidence export
# ---------------------------------------------------------------------------

def test_sqlite_v1_to_v2_migration_and_export_fields(tmp_path: Path) -> None:
    db_file = tmp_path / "v1_upgrade.db"
    # Manually create a v1 database first (without acquisition_method/response_status/final_url)
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (1, '2026-01-01', 'v1');
        CREATE TABLE batch_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, total_items INTEGER NOT NULL DEFAULT 0, completed_items INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_updated_at TEXT NOT NULL);
        CREATE TABLE batch_items (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, position INTEGER NOT NULL, original_input TEXT NOT NULL, canonical_key TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', profile_url TEXT NOT NULL DEFAULT '', direct_id TEXT, status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, response_time REAL NOT NULL DEFAULT 0.0, error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE search_history (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_item_id INTEGER, input TEXT NOT NULL, platform TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', profile_url TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '', id_type TEXT NOT NULL DEFAULT 'UNKNOWN', extraction_method TEXT NOT NULL DEFAULT 'NONE', extraction_source TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'NOT_VERIFIED', validation_status TEXT NOT NULL DEFAULT 'UNVERIFIED', status TEXT NOT NULL DEFAULT 'NOT_FOUND', response_time REAL NOT NULL DEFAULT 0.0, error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO search_history (input, platform, user_id, status, created_at) VALUES ('https://www.youtube.com/@Google', 'youtube', 'UCK8sQmJBp8GCxrOtXWBpyEA', 'FOUND', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    # Initialize DatabaseManager -> upgrades v1 -> v2 seamlessly without losing row
    db = DatabaseManager(db_path=db_file)
    assert db.initialize() == CURRENT_SCHEMA_VERSION
    repo = HistoryRepository(db)
    rows = repo.list_results()
    assert len(rows) == 1
    assert rows[0].user_id == "UCK8sQmJBp8GCxrOtXWBpyEA"
    assert rows[0].acquisition_method == "HTTP"
    assert rows[0].response_status == 0

    # Export JSON includes unified evidence fields
    out = export_results(rows, tmp_path / "v2.json", "json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert tuple(data[0].keys()) == EXPORT_FIELDS
    assert "acquisition_method" in data[0]
    assert "response_status" in data[0]
    assert "final_url" in data[0]


async def test_browser_fallback_disabled_stays_http_only() -> None:
    events: list[str] = []
    http_client = HttpClient(
        timeout=5.0,
        retry_limit=0,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html><title>Instagram</title><body>shell</body></html>")),
    )
    browser = _make_mock_browser(html="<html>should never be called</html>", events=events)
    settings = AppSettings(enable_browser_fallback=False, retry_limit=0)
    async with ExtractionEngine(settings=settings, http_client=http_client, browser_client=browser) as eng:
        res = await eng.extract(normalize_input("https://www.instagram.com/instagram"))
    await http_client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.acquisition_method == AcquisitionMethod.HTTP.value
    assert events == []

    # Facebook vanity with browser fallback disabled must stay HTTP-only (LOGIN_REQUIRED, no browser call)
    fb_events: list[str] = []
    def fb_login_handler(req: httpx.Request) -> httpx.Response:
        if "/login/" in str(req.url):
            return httpx.Response(400, text="<html><title>Error</title><body>Error</body></html>")
        return httpx.Response(302, headers={"Location": "https://www.facebook.com/login/?next=https://www.facebook.com/zuck"})

    hc_fb = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(fb_login_handler))
    br_fb = _make_mock_browser(html="<html>should never be called</html>", events=fb_events)
    async with ExtractionEngine(settings=settings, http_client=hc_fb, browser_client=br_fb) as eng_fb:
        res_fb = await eng_fb.extract(normalize_input("https://www.facebook.com/zuck"))
    await hc_fb.aclose()

    assert res_fb.status == ResultStatus.LOGIN_REQUIRED.value
    assert res_fb.acquisition_method == AcquisitionMethod.HTTP.value
    assert res_fb.response_status == 400
    assert fb_events == []


def test_v1_0_0_full_user_database_migrates_preserving_history_settings_and_batches(tmp_path: Path) -> None:
    from database import BatchRepository, SettingsRepository

    db_file = tmp_path / "v1_0_0_user.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (1, '2026-11-24T12:00:00+00:00', 'Initial schema');
        CREATE TABLE batch_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, total_items INTEGER NOT NULL DEFAULT 0, completed_items INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_updated_at TEXT NOT NULL);
        CREATE TABLE batch_items (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, position INTEGER NOT NULL, original_input TEXT NOT NULL, canonical_key TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', profile_url TEXT NOT NULL DEFAULT '', direct_id TEXT, status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, response_time REAL NOT NULL DEFAULT 0.0, error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE search_history (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_item_id INTEGER, input TEXT NOT NULL, platform TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', profile_url TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '', id_type TEXT NOT NULL DEFAULT 'UNKNOWN', extraction_method TEXT NOT NULL DEFAULT 'NONE', extraction_source TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'NOT_VERIFIED', validation_status TEXT NOT NULL DEFAULT 'UNVERIFIED', status TEXT NOT NULL DEFAULT 'NOT_FOUND', response_time REAL NOT NULL DEFAULT 0.0, error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

        INSERT INTO app_settings VALUES ('request_timeout', '25.0', '2026-11-24');
        INSERT INTO app_settings VALUES ('max_concurrency', '4', '2026-11-24');
        INSERT INTO batch_sessions VALUES (1, 'COMPLETED', 1, 1, '2026-11-24', '2026-11-24', '2026-11-24', '2026-11-24');
        INSERT INTO batch_items VALUES (1, 1, 0, 'https://www.youtube.com/@Google', 'youtube:@google', 'youtube', '@Google', 'https://www.youtube.com/@Google', NULL, 'FOUND', 0, 0.4, '', '2026-11-24', '2026-11-24');
        INSERT INTO search_history (batch_item_id, input, platform, username, user_id, id_type, extraction_method, extraction_source, confidence, validation_status, status, response_time, created_at)
        VALUES (1, 'https://www.youtube.com/@Google', 'youtube', '@Google', 'UCK8sQmJBp8GCxrOtXWBpyEA', 'CHANNEL_ID', 'EMBEDDED_JSON', 'ytInitialData', 'HIGH', 'VERIFIED', 'FOUND', 0.4, '2026-11-24T12:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = DatabaseManager(db_path=db_file)
    assert db.initialize() == CURRENT_SCHEMA_VERSION

    # Verify history, settings, and batch session + items all preserved intact
    h = HistoryRepository(db).list_results()
    assert len(h) == 1 and h[0].user_id == "UCK8sQmJBp8GCxrOtXWBpyEA"

    s = SettingsRepository(db).load()
    assert s.request_timeout == 25.0
    assert s.max_concurrency == 4
    assert s.enable_browser_fallback is True  # new default filled cleanly

    brepo = BatchRepository(db)
    sessions = brepo.list_sessions()
    assert len(sessions) == 1 and sessions[0].status == "COMPLETED"
    items = brepo.get_batch_items(1)
    assert len(items) == 1 and items[0].original_input == "https://www.youtube.com/@Google"

    # Persist new v2 BROWSER_EXTRACTED metadata and verify round-trip
    from core.models import SearchResult

    hrepo = HistoryRepository(db)
    hrepo.save_result(
        SearchResult(
            platform=Platform.FACEBOOK.value,
            input="https://www.facebook.com/zuck",
            username="zuck",
            profile_url="https://www.facebook.com/profile.php?id=4",
            user_id="4",
            id_type=IdType.NUMERIC_PROFILE_ID.value,
            acquisition_method=AcquisitionMethod.BROWSER.value,
            extraction_method=ExtractionMethod.BROWSER_EXTRACTED.value,
            extraction_source="browser_dom:html_json:userID",
            response_status=200,
            final_url="https://www.facebook.com/zuck",
            confidence=Confidence.HIGH.value,
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        )
    )
    reloaded = hrepo.list_results(platform=Platform.FACEBOOK.value)
    assert len(reloaded) == 1
    assert reloaded[0].acquisition_method == AcquisitionMethod.BROWSER.value
    assert reloaded[0].extraction_method == ExtractionMethod.BROWSER_EXTRACTED.value
    assert reloaded[0].response_status == 200
    assert reloaded[0].final_url == "https://www.facebook.com/zuck"
