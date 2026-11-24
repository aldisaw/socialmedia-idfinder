"""Offline tests for HttpClient, ExtractionEngine fault isolation, and validator scoring."""

from __future__ import annotations

import httpx

from config import AppSettings
from core.engine import ExtractionEngine
from core.http_client import HttpClient, parse_retry_after
from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from core.normalizer import normalize_input
from core.scheduler import BatchScheduler
from core.validator import validate_and_score


async def test_http_client_handles_403_404_5xx_and_max_one_retry() -> None:
    calls = {"500": 0, "403": 0, "404": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/500":
            calls["500"] += 1
            return httpx.Response(503, text="Service Unavailable")
        if path == "/403":
            calls["403"] += 1
            return httpx.Response(403, text="Forbidden")
        if path == "/404":
            calls["404"] += 1
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, text="ok")

    async with HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler)) as client:
        r500 = await client.get("https://example.test/500")
        r403 = await client.get("https://example.test/403")
        r404 = await client.get("https://example.test/404")

    # 5xx retried at most once (2 total calls); 403 and 404 never retried
    assert calls["500"] == 2
    assert r500.error_type == "SERVER_ERROR"
    assert calls["403"] == 1
    assert r403.error_type == "FORBIDDEN"
    assert calls["404"] == 1
    assert r404.error_type == "NOT_FOUND"


async def test_http_client_respects_retry_after_and_caps_retries() -> None:
    attempts = {"short": 0, "long": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/short429":
            attempts["short"] += 1
            if attempts["short"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.05"})
            return httpx.Response(200, text="recovered")
        if req.url.path == "/long429":
            attempts["long"] += 1
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(404)

    async with HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler)) as client:
        r_short = await client.get("https://example.test/short429")
        r_long = await client.get("https://example.test/long429")

    assert attempts["short"] == 2
    assert r_short.ok and r_short.text == "recovered"
    assert attempts["long"] == 1
    assert r_long.error_type == "RATE_LIMITED"
    assert r_long.retry_after == 120.0
    assert parse_retry_after("30") == 30.0


async def test_http_client_timeout_and_no_cookie_persistence() -> None:
    seen_cookies: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/timeout":
            raise httpx.ReadTimeout("timed out", request=req)
        seen_cookies.append(req.headers.get("cookie", ""))
        return httpx.Response(200, text="ok", headers={"Set-Cookie": "sessionid=secret123; Path=/"})

    async with HttpClient(timeout=2.0, retry_limit=1, transport=httpx.MockTransport(handler)) as client:
        rt = await client.get("https://example.test/timeout")
        r1 = await client.get("https://example.test/c1")
        r2 = await client.get("https://example.test/c2")

    assert rt.error_type == "TIMEOUT"
    assert "set-cookie" not in r1.headers
    assert seen_cookies == ["", ""]  # never forwarded session cookie on subsequent requests
    assert r2.ok


async def test_engine_isolates_failures_across_mixed_batch() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "youtube.com/@Google" in url:
            return httpx.Response(
                200,
                text='<script>var ytInitialData={"metadata":{"channelMetadataRenderer":{"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw","vanityChannelUrl":"https://www.youtube.com/@Google"}}};</script>',
            )
        if "x.com/ratelimited" in url:
            return httpx.Response(429, headers={"Retry-After": "60"})
        if "instagram.com/emptyhtml" in url:
            return httpx.Response(200, text="   ")
        if "tiktok.com/@timeoutuser" in url:
            raise httpx.ReadTimeout("timeout", request=req)
        return httpx.Response(404)

    settings = AppSettings(request_timeout=5.0, batch_timeout=30.0, max_concurrency=3, retry_limit=0)
    http_client = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))
    engine = ExtractionEngine(settings=settings, http_client=http_client)
    scheduler = BatchScheduler(engine=engine, settings=settings)

    items = [
        normalize_input("https://www.youtube.com/@Google"),
        normalize_input("https://x.com/ratelimited"),
        normalize_input("https://www.instagram.com/emptyhtml"),
        normalize_input("https://www.tiktok.com/@timeoutuser"),
        normalize_input("bare_ambiguous_user"),
    ]
    results = await scheduler.run_batch(items)
    await http_client.aclose()

    assert len(results) == 5
    assert results[0].status == ResultStatus.FOUND.value
    assert results[0].user_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert results[0].confidence == Confidence.HIGH.value

    assert results[1].status == ResultStatus.RATE_LIMITED.value
    assert results[2].status == ResultStatus.NOT_FOUND.value
    assert "Empty HTML" in results[2].error_message
    assert results[3].status == ResultStatus.TIMEOUT.value
    assert results[4].status == ResultStatus.INVALID_INPUT.value
    assert "AMBIGUOUS_INPUT" in results[4].error_message


def test_validator_rejects_mismatched_id_type_for_platform() -> None:
    # TikTok secUid mislabeled as NUMERIC_USER_ID must be rejected by validator
    norm = normalize_input("https://www.tiktok.com/@tiktok")
    bad = SearchResult(
        platform=Platform.TIKTOK.value,
        input=norm.raw_input,
        user_id="MS4wLjABAAAA_not_numeric_at_all_123456",
        id_type=IdType.NUMERIC_USER_ID.value,
        extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
        validation_status=ResultValidationStatus.VERIFIED.value,
        status=ResultStatus.FOUND.value,
    )
    checked = validate_and_score(bad, norm)
    assert checked.status == ResultStatus.NOT_FOUND.value
    assert checked.user_id == ""
    assert checked.confidence == Confidence.NOT_VERIFIED.value
