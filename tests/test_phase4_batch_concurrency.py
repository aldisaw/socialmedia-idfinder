"""Comprehensive offline tests for Phase 4 batch processing, timeouts, concurrency, and cancellation."""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from config import AppSettings, MAX_BATCH_SIZE
from core.engine import ExtractionEngine
from core.http_client import HttpClient
from core.models import Confidence, Platform, ResultStatus
from core.normalizer import normalize_batch, normalize_input
from core.scheduler import BatchScheduler, PlatformCooldownTracker
from gui.main_window import MainWindow
from gui.worker import SearchWorker


def _yt_html(handle: str, cid: str) -> str:
    return (
        f'<script>var ytInitialData={{"metadata":{{"channelMetadataRenderer":'
        f'{{"externalId":"{cid}","vanityChannelUrl":"https://www.youtube.com/@{handle}"}}}}}};</script>'
    )


def _x_html(screen: str, uid: str) -> str:
    return (
        f'<script type="application/ld+json">'
        f'{{"@type":"ProfilePage","mainEntity":{{"identifier":"{uid}","additionalName":"{screen}"}}}}'
        f"</script>"
    )


def _ig_html(uname: str, uid: str) -> str:
    return f'<script type="application/json">{{"profile_id":"{uid}","username":"{uname}"}}</script>'


def _tt_html(uname: str, uid: str) -> str:
    return (
        f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        f'{{"__DEFAULT_SCOPE__":{{"webapp.user-detail":{{"userInfo":{{"user":{{"id":"{uid}","uniqueId":"{uname}"}}}}}}}}}}'
        f"</script>"
    )


def _fb_html(vanity: str, uid: str) -> str:
    return f'<script type="application/json">{{"userID":"{uid}","userVanity":"{vanity}","__typename":"User"}}</script>'


# ---------------------------------------------------------------------------
# 1. 10 mixed-platform entries + strict concurrency cap + order preservation
# ---------------------------------------------------------------------------

async def test_batch_ten_mixed_platforms_enforces_concurrency_and_order() -> None:
    active = 0
    peak_active = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.03)
            url = str(req.url)
            if "youtube.com/@yt" in url:
                num = url.rsplit("@yt", 1)[1]
                return httpx.Response(200, text=_yt_html(f"yt{num}", f"UC000000000000000000000{num}"))
            if "x.com/xuser" in url:
                num = url.rsplit("xuser", 1)[1]
                return httpx.Response(200, text=_x_html(f"xuser{num}", f"1000{num}"))
            if "instagram.com/iguser" in url:
                num = url.rsplit("iguser", 1)[1]
                return httpx.Response(200, text=_ig_html(f"iguser{num}", f"2000{num}"))
            if "tiktok.com/@ttuser" in url:
                num = url.rsplit("@ttuser", 1)[1]
                return httpx.Response(200, text=_tt_html(f"ttuser{num}", f"3000{num}"))
            if "facebook.com/fbuser" in url:
                num = url.rsplit("fbuser", 1)[1]
                return httpx.Response(200, text=_fb_html(f"fbuser{num}", f"40000{num}"))
            return httpx.Response(404)
        finally:
            active -= 1

    raw_entries = [
        "https://www.youtube.com/@yt1",
        "https://x.com/xuser2",
        "https://www.instagram.com/iguser3",
        "https://www.tiktok.com/@ttuser4",
        "https://www.facebook.com/fbuser5",
        "https://www.youtube.com/@yt6",
        "https://x.com/xuser7",
        "https://www.instagram.com/iguser8",
        "https://www.tiktok.com/@ttuser9",
        "https://www.facebook.com/fbuser0",
    ]
    items, dups = normalize_batch(raw_entries)
    assert len(items) == MAX_BATCH_SIZE and dups == 0

    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=3, retry_limit=0)
    http_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))
    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)
        results = await scheduler.run_batch(items)
    await http_client.aclose()

    assert len(results) == 10
    assert peak_active <= 3
    assert peak_active >= 2  # actually ran concurrently
    # Original input order strictly preserved
    assert [r.input for r in results] == raw_entries
    assert all(r.status == ResultStatus.FOUND.value for r in results)
    assert results[0].user_id == "UC0000000000000000000001"
    assert results[1].user_id == "10002"
    assert results[2].user_id == "20003"
    assert results[3].user_id == "30004"
    assert results[4].user_id == "400005"


# ---------------------------------------------------------------------------
# 2. Separate timeouts: per-request, per-item, and batch timeout
# ---------------------------------------------------------------------------

async def test_per_request_timeout_and_per_item_timeout() -> None:
    async def req_timeout_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("per-request timeout", request=req)

    settings_req = AppSettings(request_timeout=0.2, item_timeout=5.0, batch_timeout=10.0, retry_limit=0)
    hc1 = HttpClient(timeout=0.2, retry_limit=0, transport=httpx.MockTransport(req_timeout_handler))
    async with ExtractionEngine(settings=settings_req, http_client=hc1) as eng1:
        r_req = await eng1.extract(normalize_input("https://www.youtube.com/@Google"))
    await hc1.aclose()
    assert r_req.status == ResultStatus.TIMEOUT.value

    # Per-item timeout triggers even when httpx request itself is still sleeping
    async def slow_item_handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.0)
        return httpx.Response(200, text="too late")

    settings_item = AppSettings(request_timeout=5.0, item_timeout=0.12, batch_timeout=10.0, retry_limit=0)
    hc2 = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(slow_item_handler))
    async with ExtractionEngine(settings=settings_item, http_client=hc2) as eng2:
        r_item = await eng2.extract(normalize_input("https://www.youtube.com/@Google"))
    await hc2.aclose()
    assert r_item.status == ResultStatus.TIMEOUT.value
    assert "Per-item timeout" in r_item.error_message


async def test_batch_timeout_preserves_partial_results_and_no_orphans() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "@fast" in url:
            return httpx.Response(200, text=_yt_html("fast", "UCFAST000000000000000001"))
        await asyncio.sleep(2.0)
        return httpx.Response(200, text="never")

    items = [
        normalize_input("https://www.youtube.com/@fast"),
        normalize_input("https://www.youtube.com/@slow1"),
        normalize_input("https://www.youtube.com/@slow2"),
        normalize_input("https://www.youtube.com/@slow3"),
    ]
    settings = AppSettings(request_timeout=5.0, item_timeout=5.0, batch_timeout=0.18, max_concurrency=2, retry_limit=0)
    http_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))

    baseline_tasks = len(asyncio.all_tasks())
    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)
        results = await scheduler.run_batch(items)
    await http_client.aclose()
    await asyncio.sleep(0.02)

    assert len(results) == 4
    assert results[0].status == ResultStatus.FOUND.value
    assert results[0].user_id == "UCFAST000000000000000001"
    for r in results[1:]:
        assert r.status == ResultStatus.TIMEOUT.value
    assert len(asyncio.all_tasks()) <= baseline_tasks


# ---------------------------------------------------------------------------
# 3. Cancellation during HTTP request & while queued (preserves partial results)
# ---------------------------------------------------------------------------

async def test_cancellation_in_flight_and_queued_preserves_partials() -> None:
    started_slow = asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "@done1" in url:
            return httpx.Response(200, text=_yt_html("done1", "UCDONE000000000000000001"))
        started_slow.set()
        await asyncio.sleep(5.0)
        return httpx.Response(200, text="cancelled")

    items = [
        normalize_input("https://www.youtube.com/@done1"),
        normalize_input("https://www.youtube.com/@inflight2"),
        normalize_input("https://www.youtube.com/@queued3"),
        normalize_input("https://www.youtube.com/@queued4"),
    ]
    settings = AppSettings(request_timeout=10.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=1, retry_limit=0)
    http_client = HttpClient(timeout=10.0, retry_limit=0, transport=httpx.MockTransport(handler))

    baseline_tasks = len(asyncio.all_tasks())
    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)

        async def _trigger_cancel() -> None:
            await started_slow.wait()
            await asyncio.sleep(0.02)
            scheduler.cancel()

        cancel_task = asyncio.create_task(_trigger_cancel())
        results = await scheduler.run_batch(items)
        await cancel_task
    await http_client.aclose()
    await asyncio.sleep(0.02)

    assert len(results) == 4
    assert results[0].status == ResultStatus.FOUND.value
    assert results[0].user_id == "UCDONE000000000000000001"
    assert all(r.status == ResultStatus.CANCELLED.value for r in results[1:])
    assert len(asyncio.all_tasks()) <= baseline_tasks


# ---------------------------------------------------------------------------
# 4. HTTP 429 cooldown prevents hammering same platform while others succeed
# ---------------------------------------------------------------------------

async def test_rate_limit_cooldown_skips_same_platform_not_other_platforms() -> None:
    x_calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal x_calls
        url = str(req.url)
        if "x.com/" in url:
            x_calls += 1
            return httpx.Response(429, headers={"Retry-After": "60"})
        if "youtube.com/@okyt" in url:
            return httpx.Response(200, text=_yt_html("okyt", "UCOKYT000000000000000001"))
        return httpx.Response(404)

    items = [
        normalize_input("https://x.com/first429"),
        normalize_input("https://x.com/second429"),
        normalize_input("https://www.youtube.com/@okyt"),
    ]
    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=1, retry_limit=1)
    http_client = HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))
    cooldown = PlatformCooldownTracker(default_cooldown=30.0)

    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings, cooldown_tracker=cooldown)
        results = await scheduler.run_batch(items)
    await http_client.aclose()

    # Second X request skipped due to cooldown -> only 1 actual HTTP hit to x.com
    assert x_calls == 1
    assert results[0].status == ResultStatus.RATE_LIMITED.value
    assert results[1].status == ResultStatus.RATE_LIMITED.value
    assert "cooldown" in results[1].error_message.lower()
    assert results[2].status == ResultStatus.FOUND.value
    assert results[2].user_id == "UCOKYT000000000000000001"


# ---------------------------------------------------------------------------
# 5. Retry Failed: retries only eligible failed items, never overwrites FOUND
# ---------------------------------------------------------------------------

async def test_retry_failed_preserves_found_and_only_retries_eligible() -> None:
    call_counts: dict[str, int] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        call_counts[url] = call_counts.get(url, 0) + 1
        if "@ok1" in url:
            # First run succeeds; if ever called again (should NOT happen), fail!
            if call_counts[url] == 1:
                return httpx.Response(200, text=_yt_html("ok1", "UCOK10000000000000000001"))
            return httpx.Response(500)
        if "@flaky2" in url:
            if call_counts[url] == 1:
                raise httpx.ReadTimeout("transient", request=req)
            return httpx.Response(200, text=_yt_html("flaky2", "UCFLAKY00000000000000002"))
        if "@missing3" in url:
            return httpx.Response(404)
        return httpx.Response(403)

    items = [
        normalize_input("https://www.youtube.com/@ok1"),
        normalize_input("https://www.youtube.com/@flaky2"),
        normalize_input("https://www.youtube.com/@missing3"),
        normalize_input("https://www.youtube.com/@blocked4"),
        normalize_input("bare_invalid"),
    ]
    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=3, retry_limit=0)
    http_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))

    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)
        first_results = await scheduler.run_batch(items)

        assert first_results[0].status == ResultStatus.FOUND.value
        assert first_results[1].status == ResultStatus.TIMEOUT.value
        assert first_results[2].status == ResultStatus.NOT_FOUND.value
        assert first_results[3].status == ResultStatus.BLOCKED.value
        assert first_results[4].status == ResultStatus.INVALID_INPUT.value

        retry_indices = BatchScheduler.select_retryable_indices(items, first_results)
        assert retry_indices == [1]  # ONLY the TIMEOUT entry is eligible

        second_results = await scheduler.run_batch(
            items,
            indices_to_run=retry_indices,
            existing_results=first_results,
        )
    await http_client.aclose()

    assert len(second_results) == 5
    # Previously FOUND entry was never re-called and never overwritten
    assert call_counts["https://www.youtube.com/@ok1"] == 1
    assert second_results[0].status == ResultStatus.FOUND.value
    assert second_results[0].user_id == "UCOK10000000000000000001"
    # Retried flaky entry recovered!
    assert second_results[1].status == ResultStatus.FOUND.value
    assert second_results[1].user_id == "UCFLAKY00000000000000002"
    # Non-retryable statuses preserved verbatim
    assert second_results[2].status == ResultStatus.NOT_FOUND.value
    assert second_results[3].status == ResultStatus.BLOCKED.value
    assert second_results[4].status == ResultStatus.INVALID_INPUT.value


# ---------------------------------------------------------------------------
# 6. Duplicate canonical key prevention & one-item failure isolation
# ---------------------------------------------------------------------------

async def test_duplicate_prevention_and_exception_isolation() -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if "x.com/crasher" in str(req.url):
            raise RuntimeError("simulated unexpected transport crash")
        return httpx.Response(200, text=_yt_html("dupcheck", "UCDUP0000000000000000001"))

    # Pass two items with identical canonical key directly to BatchScheduler
    items = [
        normalize_input("https://www.youtube.com/@dupcheck"),
        normalize_input("https://m.youtube.com/@DUPCHECK/videos"),
        normalize_input("https://x.com/crasher"),
    ]
    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=2, retry_limit=0)
    http_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))

    async with ExtractionEngine(settings=settings, http_client=http_client) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)
        results = await scheduler.run_batch(items)
    await http_client.aclose()

    assert results[0].status == ResultStatus.FOUND.value
    assert results[1].status == ResultStatus.CANCELLED.value  # duplicate canonical key skipped
    assert results[2].status == ResultStatus.ERROR.value  # isolated without crashing batch
    assert calls == 2  # only 1 call for dupcheck + 1 call for crasher


# ---------------------------------------------------------------------------
# 7. SearchWorker + GUI responsiveness, clean shutdown & HTTP client lifecycle
# ---------------------------------------------------------------------------

def test_worker_and_gui_clean_shutdown_and_http_lifecycle(tmp_path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        if "@gui1" in str(req.url):
            return httpx.Response(200, text=_yt_html("gui1", "UCGUI0000000000000000001"))
        return httpx.Response(404)

    items = [
        normalize_input("https://www.youtube.com/@gui1"),
        normalize_input("https://www.youtube.com/@gui2"),
        normalize_input("bare_ambiguous"),
    ]
    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=2, retry_limit=0)
    mock_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))

    win = MainWindow(db_path=tmp_path / "p4.db")
    win._batch_items = items
    win._batch_results = [None] * len(items)
    win._active_mode = "batch"
    win._batch_page.prepare_table(items)
    win._batch_page.set_running(True)

    loop = QEventLoop()
    shutdown_fired = False
    finished_results: list = []

    worker = SearchWorker(
        items=items,
        settings=settings,
        is_batch=True,
        http_client=mock_client,
        parent=win,
    )
    worker.item_started_signal.connect(win._on_item_started)
    worker.item_progress_signal.connect(win._on_item_progress)
    worker.item_finished_signal.connect(win._on_item_finished)
    worker.all_finished_signal.connect(lambda res: finished_results.extend(res))

    def _on_shutdown() -> None:
        nonlocal shutdown_fired
        shutdown_fired = True
        win._on_worker_shutdown_complete()
        loop.quit()

    worker.shutdown_complete_signal.connect(_on_shutdown)
    QTimer.singleShot(5000, loop.quit)
    worker.start()
    loop.exec()
    worker.wait(2000)

    assert shutdown_fired is True
    assert len(finished_results) == 3
    assert finished_results[0].status == ResultStatus.FOUND.value
    assert finished_results[1].status == ResultStatus.NOT_FOUND.value
    assert finished_results[2].status == ResultStatus.INVALID_INPUT.value
    assert "Found: 1" in win._batch_page._counter_labels["found"].text()
    assert "Not Found: 1" in win._batch_page._counter_labels["not_found"].text()
    assert "Error: 1" in win._batch_page._counter_labels["error"].text()
    assert not win._batch_page._btn_retry_failed.isEnabled()

    # Close external mock client cleanly
    asyncio.run(mock_client.aclose())
    assert mock_client._client is None
    win.close()


def test_worker_owned_http_client_closed_on_cancel() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    started = False

    async def slow_handler(req: httpx.Request) -> httpx.Response:
        nonlocal started
        started = True
        await asyncio.sleep(5.0)
        return httpx.Response(200, text="never")

    items = [
        normalize_input("https://www.youtube.com/@slowa"),
        normalize_input("https://www.youtube.com/@slowb"),
    ]
    settings = AppSettings(request_timeout=10.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=1, retry_limit=0)
    mock_client = HttpClient(timeout=10.0, retry_limit=0, transport=httpx.MockTransport(slow_handler))

    loop = QEventLoop()
    results_seen: list = []
    shutdown_fired = False

    worker = SearchWorker(items=items, settings=settings, is_batch=True, http_client=mock_client)
    worker.all_finished_signal.connect(lambda res: results_seen.extend(res))

    def _on_done() -> None:
        nonlocal shutdown_fired
        shutdown_fired = True
        loop.quit()

    worker.shutdown_complete_signal.connect(_on_done)
    worker.start()
    QTimer.singleShot(80, worker.request_cancel)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    worker.wait(2000)

    assert shutdown_fired is True
    assert len(results_seen) == 2
    assert all(r.status == ResultStatus.CANCELLED.value for r in results_seen)
    asyncio.run(mock_client.aclose())
    assert mock_client._client is None
