"""Offline security & reliability hardening tests covering H-01..H-03, M-01..M-06, L-01..L-05."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from config import AppSettings, MAX_USER_AGENT_LENGTH, USER_AGENT
from core.browser_client import BrowserClient, MAX_BROWSER_CONCURRENCY
from core.http_client import HttpClient, _SSRFSafeNetworkBackend
from core.logger import SanitizingFilter, sanitize_log_text, setup_logging, shutdown_logging
from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from core.normalizer import _is_valid_hostname, normalize_input
from core.scheduler import BatchScheduler, PlatformCooldownTracker
from core.url_security import UrlSecurityError, is_blocked_ip, validate_public_url
from database.database import DatabaseManager
from exports.exporter import export_results, neutralize_csv_cell, validate_export_path


# ---------------------------------------------------------------------------
# 1. H-01: URL Security, IP/DNS SSRF Protection, Redirect Validation & DNS Rebinding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "::ffff:127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "169.254.169.254",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12::1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "::",
        "100.64.0.1",
    ],
)
def test_is_blocked_ip_rejects_all_internal_and_reserved_ranges(ip: str) -> None:
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_is_blocked_ip_allows_global_public_ips(ip: str) -> None:
    assert is_blocked_ip(ip) is False


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost/admin",
        "https://sub.localhost/test",
        "http://127.0.0.1/secret",
        "http://[::1]/secret",
        "http://10.0.0.5/",
        "http://172.16.5.4/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://2130706433/",       # decimal loopback
        "http://0x7f000001/",       # hex loopback
        "http://0177.0.0.1/",       # octal loopback
        "http://metadata.google.internal/",
        "http://intranet.local/",
        "file:///C:/Windows/win.ini",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "gopher://127.0.0.1:6379/",
        "https://user:pass@example.com/",
        "https://example.com:8080/path",
    ],
)
def test_validate_public_url_rejects_ssrf_schemes_ips_and_hosts(bad_url: str) -> None:
    with pytest.raises(UrlSecurityError):
        validate_public_url(bad_url, resolve_dns=False)


async def test_http_client_blocks_initial_ssrf_and_non_http_without_network() -> None:
    called = False

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="should never execute")

    client = HttpClient(transport=httpx.MockTransport(handler), retry_limit=0)
    try:
        for url in (
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/health",
            "file:///etc/passwd",
        ):
            res = await client.get(url)
            assert res.error_type == "SSRF_BLOCKED"
            assert not res.ok
    finally:
        await client.aclose()
    assert called is False


@pytest.mark.parametrize(
    "redirect_target",
    [
        "http://127.0.0.1/internal",
        "http://[::1]/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/secret",
        "http://localhost/login",
        "file:///C:/Windows/win.ini",
        "ftp://example.com/dump",
        "javascript:alert(1)",
    ],
)
async def test_http_client_blocks_malicious_redirects(redirect_target: str) -> None:
    visited: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        visited.append(str(req.url))
        if str(req.url) == "https://www.instagram.com/alice":
            return httpx.Response(302, headers={"Location": redirect_target})
        return httpx.Response(200, text="LEAKED_INTERNAL_DATA")

    client = HttpClient(transport=httpx.MockTransport(handler), retry_limit=0)
    try:
        res = await client.get("https://www.instagram.com/alice")
    finally:
        await client.aclose()

    assert res.error_type == "SSRF_BLOCKED"
    assert res.text == ""
    assert visited == ["https://www.instagram.com/alice"]


async def test_http_client_blocks_dns_resolving_to_private_ip_and_respects_max_redirects() -> None:
    # DNS resolves public-looking domain to loopback
    def evil_dns(host: str, _port: int) -> list[str]:
        if host == "rebind.example.com":
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    def handler(req: httpx.Request) -> httpx.Response:
        if "hop" in str(req.url):
            n = int(str(req.url).rsplit("hop", 1)[1])
            return httpx.Response(302, headers={"Location": f"https://www.example.com/hop{n + 1}"})
        return httpx.Response(302, headers={"Location": "https://rebind.example.com/target"})

    client = HttpClient(
        transport=httpx.MockTransport(handler),
        dns_resolver=evil_dns,
        max_redirects=2,
        retry_limit=0,
    )
    try:
        res_dns = await client.get("https://www.example.com/start")
        assert res_dns.error_type == "SSRF_BLOCKED"

        res_loop = await client.get("https://www.example.com/hop0")
        assert res_loop.error_type == "TOO_MANY_REDIRECTS"
    finally:
        await client.aclose()


async def test_ssrf_network_backend_prevents_dns_rebinding_at_connect_time() -> None:
    calls = {"n": 0}
    dialed: list[tuple[str, int]] = []

    class FakeInnerBackend:
        async def connect_tcp(self, host: str, port: int, **_kwargs: Any) -> str:
            dialed.append((host, port))
            return f"connected:{host}:{port}"

        async def sleep(self, _s: float) -> None:
            return None

    def rebinding_resolver(_host: str, _port: int) -> list[str]:
        calls["n"] += 1
        # First lookup returns public IP, second lookup rebinds to loopback
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    backend = _SSRFSafeNetworkBackend(FakeInnerBackend(), dns_resolver=rebinding_resolver)

    # 1st connect: pins and dials validated public IP directly (never passes raw hostname to OS dial)
    stream = await backend.connect_tcp("rebind.example.com", 443)
    assert stream == "connected:93.184.216.34:443"
    assert dialed == [("93.184.216.34", 443)]

    # 2nd connect: DNS rebinds to 127.0.0.1 -> blocked BEFORE inner connect_tcp runs
    with pytest.raises(UrlSecurityError):
        await backend.connect_tcp("rebind.example.com", 443)
    assert len(dialed) == 1


# ---------------------------------------------------------------------------
# 2. H-02 & M-02: Playwright SSRF Route Interception, Popups, Context Isolation, Concurrency & Cleanup
# ---------------------------------------------------------------------------

class _FakeRoute:
    def __init__(self, url: str, resource_type: str = "document", is_nav: bool = True) -> None:
        self.request = type(
            "Req",
            (),
            {
                "url": url,
                "resource_type": resource_type,
                "is_navigation_request": lambda _self: is_nav,
            },
        )()
        self.aborted: list[str] = []
        self.continued: bool = False

    async def abort(self, reason: str = "failed") -> None:
        self.aborted.append(reason)

    async def continue_(self) -> None:
        self.continued = True


async def test_browser_client_blocks_ssrf_subresources_redirects_and_popups() -> None:
    captured_context_kwargs: dict[str, Any] = {}
    route_results: dict[str, str] = {}
    popup_closed = {"closed": False}

    class FakePopup:
        async def close(self) -> None:
            popup_closed["closed"] = True

    class FakePage:
        def __init__(self, ctx: Any) -> None:
            self._ctx = ctx
            self.url = "https://www.instagram.com/alice"

        def on(self, _ev: str, _fn: Any) -> None:
            return None

        async def goto(self, _url: str, **_kwargs: Any) -> Any:
            # Simulate route interceptor being called for main nav, XHR to 127.0.0.1, iframe to file://, and image asset
            handler = self._ctx.route_handler
            assert handler is not None
            for label, r in (
                ("public_doc", _FakeRoute("https://www.instagram.com/alice", "document", True)),
                ("private_xhr", _FakeRoute("http://127.0.0.1:8080/api", "xhr", False)),
                ("metadata_fetch", _FakeRoute("http://169.254.169.254/latest/meta-data/", "fetch", False)),
                ("file_iframe", _FakeRoute("file:///C:/Windows/win.ini", "document", False)),
                ("image_asset", _FakeRoute("https://cdn.instagram.com/pic.jpg", "image", False)),
            ):
                await handler(r)
                route_results[label] = "aborted" if r.aborted else ("continued" if r.continued else "none")
            # Simulate popup spawned by page JS
            if self._ctx.page_handler:
                self._ctx.page_handler(FakePopup())
            return type("Resp", (), {"status": 200})()

        async def content(self) -> str:
            return "<html><body>ok</body></html>"

        async def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.route_handler = None
            self.page_handler = None

        async def route(self, _pattern: str, fn: Any) -> None:
            self.route_handler = fn

        def on(self, ev: str, fn: Any) -> None:
            if ev == "page":
                self.page_handler = fn

        async def new_page(self) -> FakePage:
            return FakePage(self)

        async def close(self) -> None:
            return None

    class FakeBrowser:
        async def new_context(self, **kwargs: Any) -> FakeContext:
            captured_context_kwargs.update(kwargs)
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakePW:
        chromium = type("C", (), {"launch": lambda _self, **_kw: _async_ret(FakeBrowser())})()

        async def stop(self) -> None:
            return None

    async def _async_ret(val: Any) -> Any:
        return val

    class FakeCM:
        async def start(self) -> FakePW:
            return FakePW()

    bc = BrowserClient(render_wait=0.0, playwright_factory=lambda: FakeCM())

    # 1. Initial unsafe URL rejected before Playwright launches
    blocked_initial = await bc.fetch_rendered("http://127.0.0.1/secret")
    assert blocked_initial.error_type == "SSRF_BLOCKED"

    # 2. Safe URL runs with hardened context + route interceptor + popup closure
    ok_res = await bc.fetch_rendered("https://www.instagram.com/alice")
    await asyncio.sleep(0.02)
    assert ok_res.ok
    assert captured_context_kwargs["accept_downloads"] is False
    assert captured_context_kwargs["service_workers"] == "block"
    assert captured_context_kwargs["ignore_https_errors"] is False
    assert route_results == {
        "public_doc": "continued",
        "private_xhr": "aborted",
        "metadata_fetch": "aborted",
        "file_iframe": "aborted",
        "image_asset": "aborted",
    }
    assert popup_closed["closed"] is True


async def test_browser_client_blocks_navigation_redirect_to_private_ip() -> None:
    class FakePage:
        def __init__(self, ctx: Any) -> None:
            self._ctx = ctx
            self.url = "https://www.instagram.com/alice"

        def on(self, _ev: str, _fn: Any) -> None:
            return None

        async def goto(self, _url: str, **_kwargs: Any) -> Any:
            r = _FakeRoute("http://10.0.0.1/internal", "document", True)
            await self._ctx.route_handler(r)
            assert r.aborted == ["blockedbyclient"]
            raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")

        async def close(self) -> None:
            return None

    class FakeContext:
        route_handler = None

        async def route(self, _pat: str, fn: Any) -> None:
            self.route_handler = fn

        def on(self, _ev: str, _fn: Any) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage(self)

        async def close(self) -> None:
            return None

    class FakeBrowser:
        async def new_context(self, **_kw: Any) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakePW:
        async def _launch(self, **_kw: Any) -> FakeBrowser:
            return FakeBrowser()

        def __init__(self) -> None:
            self.chromium = type("C", (), {"launch": self._launch})()

        async def stop(self) -> None:
            return None

    class FakeCM:
        async def start(self) -> FakePW:
            return FakePW()

    bc = BrowserClient(render_wait=0.0, playwright_factory=lambda: FakeCM())
    res = await bc.fetch_rendered("https://www.instagram.com/alice")
    assert res.error_type == "SSRF_BLOCKED"


async def test_browser_concurrency_capped_at_2_and_cancellation_safe_cleanup() -> None:
    active = 0
    peak = 0
    cleanup_events: list[str] = []

    class FakePage:
        url = "https://www.instagram.com/alice"

        def on(self, _ev: str, _fn: Any) -> None:
            return None

        async def goto(self, _url: str, **_kw: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.08)
            active -= 1
            return type("Resp", (), {"status": 200})()

        async def content(self) -> str:
            return "<html>ok</html>"

        async def close(self) -> None:
            cleanup_events.append("page")

    class FakeContext:
        async def route(self, _p: str, _fn: Any) -> None:
            return None

        def on(self, _e: str, _fn: Any) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            cleanup_events.append("context")

    class FakeBrowser:
        async def new_context(self, **_kw: Any) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            await asyncio.sleep(0.01)
            cleanup_events.append("browser")

    class FakePW:
        async def _launch(self, **_kw: Any) -> FakeBrowser:
            return FakeBrowser()

        def __init__(self) -> None:
            self.chromium = type("C", (), {"launch": self._launch})()

        async def stop(self) -> None:
            cleanup_events.append("pw")

    class FakeCM:
        async def start(self) -> FakePW:
            return FakePW()

    bc = BrowserClient(render_wait=0.0, playwright_factory=lambda: FakeCM())
    # Launch 5 concurrent fetches; semaphore must cap peak concurrency at MAX_BROWSER_CONCURRENCY (2)
    results = await asyncio.gather(*(bc.fetch_rendered(f"https://www.instagram.com/u{i}") for i in range(5)))
    assert all(r.ok for r in results)
    assert peak <= MAX_BROWSER_CONCURRENCY

    # Cancel mid-navigation -> shielded cleanup must still close page, context, browser, pw
    cleanup_events.clear()
    task = asyncio.create_task(bc.fetch_rendered("https://www.instagram.com/cancelme"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_events == ["page", "context", "browser", "pw"]


# ---------------------------------------------------------------------------
# 3. H-03 & M-05: CSV Formula Injection, Export Path Validation & Atomic Write
# ---------------------------------------------------------------------------

def test_csv_formula_injection_neutralized_without_altering_json_or_txt(tmp_path: Path) -> None:
    payloads = ["=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(1,2)", "\t=1+1", "\r=1+1", "\n=1+1", "  =1+1"]
    for p in payloads:
        assert neutralize_csv_cell(p).startswith("'")
    assert neutralize_csv_cell("normal_user") == "normal_user"
    assert neutralize_csv_cell("https://x.com/X") == "https://x.com/X"

    res = SearchResult(
        platform=Platform.YOUTUBE.value,
        input="=HYPERLINK(\"http://example.com\")",
        username="@Google",
        profile_url="https://www.youtube.com/@Google",
        user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
        id_type=IdType.CHANNEL_ID.value,
        extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
        extraction_source="+source",
        confidence=Confidence.HIGH.value,
        validation_status=ResultValidationStatus.VERIFIED.value,
        status=ResultStatus.FOUND.value,
        error_message="-minus_note",
    )

    csv_file = export_results([res], tmp_path / "safe.csv", "csv")
    with csv_file.open("r", newline="", encoding="utf-8") as fh:
        row = list(csv.DictReader(fh))[0]
    assert row["input"] == "'=HYPERLINK(\"http://example.com\")"
    assert row["username"] == "'@Google"
    assert row["extraction_source"] == "'+source"
    assert row["error_message"] == "'-minus_note"

    # JSON and TXT must remain unmodified
    json_file = export_results([res], tmp_path / "safe.json", "json")
    jrow = json.loads(json_file.read_text(encoding="utf-8"))[0]
    assert jrow["input"] == '=HYPERLINK("http://example.com")'
    assert jrow["username"] == "@Google"
    assert jrow["extraction_source"] == "+source"

    txt_file = export_results([res], tmp_path / "safe.txt", "txt")
    txt = txt_file.read_text(encoding="utf-8")
    assert "username=@Google" in txt
    assert "extraction_source=+source" in txt


def test_export_path_validation_rejects_bad_paths_and_leaves_no_tmp_files(tmp_path: Path) -> None:
    res = SearchResult(platform="x", input="https://x.com/X", status=ResultStatus.FOUND.value)

    with pytest.raises(ValueError):
        validate_export_path(tmp_path / "NUL.csv", "csv")
    with pytest.raises(ValueError):
        validate_export_path(tmp_path / "CON.json", "json")
    with pytest.raises(ValueError):
        validate_export_path(tmp_path, "csv")
    with pytest.raises(ValueError):
        validate_export_path(tmp_path / "wrong.txt", "csv")
    with pytest.raises(ValueError):
        validate_export_path(tmp_path / "social_id_finder.db", "csv")

    out = export_results([res], tmp_path / "atomic.json", "json")
    assert out.exists()
    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []


# ---------------------------------------------------------------------------
# 4. M-01 & L-01: Concurrent Cookie Isolation & Response Size Cap
# ---------------------------------------------------------------------------

async def test_http_client_no_cookie_leakage_across_concurrent_requests() -> None:
    seen_cookie_headers: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        if "cookie" in req.headers:
            seen_cookie_headers.append(req.headers["cookie"])
        if req.url.path == "/first":
            return httpx.Response(200, text="a", headers={"Set-Cookie": "sessionid=secret_abc; Path=/"})
        await asyncio.sleep(0.02)
        return httpx.Response(200, text="b")

    client = HttpClient(transport=httpx.MockTransport(handler), retry_limit=0)
    try:
        await asyncio.gather(
            client.get("https://www.instagram.com/first"),
            client.get("https://www.instagram.com/second"),
            client.get("https://www.instagram.com/third"),
        )
        await client.get("https://www.instagram.com/fourth")
    finally:
        await client.aclose()

    assert seen_cookie_headers == []


async def test_http_client_enforces_max_response_bytes() -> None:
    big_body = "A" * 5000

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big_body)

    client = HttpClient(
        transport=httpx.MockTransport(handler),
        max_response_bytes=2048,
        retry_limit=0,
    )
    try:
        res = await client.get("https://www.example.com/big")
    finally:
        await client.aclose()

    assert res.error_type == "RESPONSE_TOO_LARGE"
    assert res.text == ""


# ---------------------------------------------------------------------------
# 5. M-03: WAL-Consistent SQLite Online Backup API
# ---------------------------------------------------------------------------

def test_sqlite_online_backup_captures_uncheckpointed_wal_data(tmp_path: Path) -> None:
    db_file = tmp_path / "wal_test.db"
    db = DatabaseManager(db_path=db_file)
    db.initialize()

    # Hold an active read snapshot so WAL frames stay in .db-wal while writing
    reader = sqlite3.connect(str(db_file))
    reader.execute("BEGIN;")
    reader.execute("SELECT COUNT(*) FROM app_settings;").fetchall()

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?);",
            ("debug_mode", "1", "2026-11-24T00:00:00+00:00"),
        )

    backup_path = db.create_backup(reason="wal_audit")
    reader.close()

    assert backup_path is not None and backup_path.exists()
    bconn = sqlite3.connect(str(backup_path))
    try:
        chk = bconn.execute("PRAGMA integrity_check;").fetchone()[0]
        row = bconn.execute("SELECT value FROM app_settings WHERE key='debug_mode';").fetchone()
    finally:
        bconn.close()

    assert chk == "ok"
    assert row is not None and row[0] == "1"


# ---------------------------------------------------------------------------
# 6. M-04 & L-04: Logger Redaction (URL Creds, Basic/Bearer, Sessions, Exceptions, CRLF) & Lifecycle
# ---------------------------------------------------------------------------

def test_logger_redacts_url_creds_basic_auth_sessions_exceptions_and_crlf() -> None:
    samples = [
        ("https://alice:s3cr3tPass@instagram.com/alice", "s3cr3tPass"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Proxy-Authorization: Bearer eyJhbGciOi.secret", "eyJhbGciOi"),
        ("Cookie: sessionid=sess999; csrftoken=csrf888; xs=fbxs777; api_key=key666", "sess999"),
    ]
    for text, secret in samples:
        redacted = sanitize_log_text(text)
        assert secret not in redacted
        assert "[REDACTED]" in redacted

    # Verify SanitizingFilter handles non-str exception args, tracebacks, and CRLF log forging
    filt = SanitizingFilter()
    try:
        raise RuntimeError("Failed on https://bob:hunter2@x.com/u\r\nFAKE [INFO] injected")
    except RuntimeError as exc:
        rec = logging.LogRecord(
            name="social_id_finder.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Network fail: %s",
            args=(exc,),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        assert filt.filter(rec) is True
        rendered = rec.getMessage()
        assert "hunter2" not in rendered
        assert "\r" not in rendered and "\n" not in rendered
        assert rec.exc_text is not None and "hunter2" not in rec.exc_text

    # Lifecycle: setup + clean shutdown without error
    setup_logging(debug=False)
    shutdown_logging()


# ---------------------------------------------------------------------------
# 7. M-06, L-02, L-03: Normalizer Edge Cases, Cooldown Race/Preservation, User-Agent Sanitization
# ---------------------------------------------------------------------------

def test_normalizer_rejects_url_credentials_ports_ip_literals_and_youtube_traversal() -> None:
    assert _is_valid_hostname("127.0.0.1") is False
    assert _is_valid_hostname("localhost") is False
    assert _is_valid_hostname("www.youtube.com") is True

    for bad in (
        "https://alice:secret@x.com/X",
        "https://www.facebook.com:8080/zuck",
        "https://www.youtube.com/c/..",
        "https://www.youtube.com/user/..",
        "https://www.youtube.com/@foo..bar",
    ):
        norm = normalize_input(bad)
        assert norm.is_valid is False, f"Expected invalid for {bad}"


async def test_scheduler_rechecks_cooldown_inside_semaphore_and_preserves_active_cooldown() -> None:
    tracker = PlatformCooldownTracker(default_cooldown=10.0)
    tracker.record_rate_limit("instagram", retry_after=30.0)
    # Updating default cooldown must NOT wipe active cooldown window (L-02)
    tracker.update_default_cooldown(5.0)
    assert tracker.remaining("instagram") > 20.0

    # Verify queued item re-checks cooldown inside semaphore after earlier item hits 429
    calls: list[str] = []

    class FakeEngine:
        async def extract(self, item: NormalizedInput, progress_cb=None) -> SearchResult:  # noqa: ARG002
            calls.append(item.username)
            await asyncio.sleep(0.03)
            return SearchResult(
                platform=item.platform.value,
                input=item.raw_input,
                username=item.username,
                profile_url=item.profile_url,
                status=ResultStatus.RATE_LIMITED.value,
                error_message="HTTP 429 Too Many Requests (Retry-After: 15s).",
            )

    fresh_tracker = PlatformCooldownTracker(default_cooldown=15.0)
    sched = BatchScheduler(
        engine=FakeEngine(),  # type: ignore[arg-type]
        settings=AppSettings(max_concurrency=1, batch_timeout=10.0),
        cooldown_tracker=fresh_tracker,
    )
    items = [
        normalize_input("https://www.instagram.com/user_a"),
        normalize_input("https://www.instagram.com/user_b"),
    ]
    out = await sched.run_batch(items)
    assert calls == ["user_a"]  # user_b skipped by cooldown without hitting network
    assert out[1].status == ResultStatus.RATE_LIMITED.value
    assert "cooldown active" in out[1].error_message.lower()


def test_user_agent_sanitization_strips_crlf_and_bounds_length() -> None:
    s = AppSettings(user_agent="Mozilla/5.0\r\nX-Injected: evil\x00" + "A" * 1000).clamp()
    assert "\r" not in s.user_agent
    assert "\n" not in s.user_agent
    assert "\x00" not in s.user_agent
    assert len(s.user_agent) <= MAX_USER_AGENT_LENGTH

    empty = AppSettings(user_agent="\r\n\t   ").clamp()
    assert empty.user_agent == USER_AGENT
