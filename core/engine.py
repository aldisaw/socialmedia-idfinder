"""Extraction engine coordinator with Method A (HTTP) and optional Method B (Playwright Browser) fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from config import AppSettings, MAX_RETRY_LIMIT
from core.browser_client import BrowserClient
from core.http_client import HttpClient
from core.models import (
    AcquisitionMethod,
    Confidence,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from extractors.base import BaseExtractor
from extractors.facebook import FacebookExtractor
from extractors.instagram import InstagramExtractor
from extractors.tiktok import TikTokExtractor
from extractors.x import XExtractor
from extractors.youtube import YouTubeExtractor

logger = logging.getLogger("social_id_finder.engine")

ProgressCallback = Callable[[str], None]

# Method B (Playwright Chromium) fallback for JS-rendered / browser-gated public profiles
BROWSER_FALLBACK_PLATFORMS: frozenset[Platform] = frozenset(
    {Platform.INSTAGRAM, Platform.TIKTOK, Platform.FACEBOOK}
)

_BROWSER_WAIT_SELECTORS: dict[Platform, str | None] = {
    Platform.TIKTOK: "#__UNIVERSAL_DATA_FOR_REHYDRATION__",
    Platform.INSTAGRAM: None,
    Platform.FACEBOOK: None,
}

# Never escalate to browser when Method A already hit an explicit access restriction
_NO_BROWSER_RETRY_STATUSES: frozenset[str] = frozenset(
    {
        ResultStatus.FOUND.value,
        ResultStatus.LOGIN_REQUIRED.value,
        ResultStatus.CAPTCHA_REQUIRED.value,
        ResultStatus.BLOCKED.value,
        ResultStatus.RATE_LIMITED.value,
        ResultStatus.INVALID_INPUT.value,
    }
)


class ExtractionEngine:
    """Routes normalized inputs to platform-specific extractors with Method A + optional Method B."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        http_client: HttpClient | None = None,
        browser_client: BrowserClient | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self._retry_limit = max(0, min(int(self.settings.retry_limit), MAX_RETRY_LIMIT))
        self._request_timeout = max(0.05, float(self.settings.request_timeout))
        self._item_timeout = max(0.05, float(self.settings.item_timeout))
        self._external_client = http_client is not None
        self._http_client = http_client or HttpClient(
            timeout=self._request_timeout,
            retry_limit=self._retry_limit,
            user_agent=self.settings.user_agent,
        )
        self._browser_client = browser_client
        self.adapters: dict[Platform, BaseExtractor] = {
            Platform.INSTAGRAM: InstagramExtractor(),
            Platform.TIKTOK: TikTokExtractor(),
            Platform.X: XExtractor(),
            Platform.FACEBOOK: FacebookExtractor(),
            Platform.YOUTUBE: YouTubeExtractor(),
        }

    def get_adapter(self, platform: Platform) -> BaseExtractor | None:
        return self.adapters.get(platform)

    def _get_browser_client(self) -> BrowserClient:
        if self._browser_client is None:
            self._browser_client = BrowserClient(
                timeout=self._request_timeout,
                user_agent=self.settings.user_agent,
            )
        return self._browser_client

    async def aclose(self) -> None:
        """Close owned HTTP client pool."""
        if not self._external_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> "ExtractionEngine":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    def _should_try_browser_fallback(self, item: NormalizedInput, http_result: SearchResult) -> bool:
        if not self.settings.enable_browser_fallback:
            return False
        # When an isolated mock HttpClient is injected without a BrowserClient, never launch a live browser
        if self._external_client and self._browser_client is None:
            return False
        if item.platform not in BROWSER_FALLBACK_PLATFORMS:
            return False
        # For Facebook vanity URLs, try Playwright once if HTTPX returned NOT_FOUND,
        # non-browser /login/ redirect, or direct HTTP 400 (Facebook often rejects raw HTTPX clients with 400)
        if item.platform == Platform.FACEBOOK and not item.direct_id:
            if http_result.response_status == 404:
                return False
            if http_result.validation_status in (
                ResultValidationStatus.IDENTITY_MISMATCH.value,
                ResultValidationStatus.CONFLICTING_IDS.value,
            ):
                return False
            return (
                http_result.status in (ResultStatus.NOT_FOUND.value, ResultStatus.LOGIN_REQUIRED.value)
                or (http_result.status == ResultStatus.ERROR.value and http_result.response_status == 400)
            )
        if http_result.status in _NO_BROWSER_RETRY_STATUSES:
            return False
        # Only fall back on 200 JS-only / CSR shell where HTTP yielded NOT_FOUND (not 404 nonexistent profile)
        if http_result.response_status == 404:
            return False
        if http_result.validation_status in (
            ResultValidationStatus.IDENTITY_MISMATCH.value,
            ResultValidationStatus.CONFLICTING_IDS.value,
        ):
            return False
        return http_result.status == ResultStatus.NOT_FOUND.value

    async def _run_browser_fallback(
        self,
        adapter: BaseExtractor,
        item: NormalizedInput,
        http_result: SearchResult,
        progress_cb: ProgressCallback | None = None,
    ) -> SearchResult:
        browser = self._get_browser_client()
        if self._browser_client is None:
            cap = browser.get_capability()
            if not cap.available:
                logger.info(
                    "[%s] Browser fallback disabled: %s",
                    item.platform.value,
                    cap.reason,
                )
                return http_result

        if progress_cb:
            progress_cb(
                f"[{item.platform.display_name}] (BROWSER) Method A returned no public ID; trying Playwright Chromium..."
            )
        logger.info("[%s] Invoking optional Method B (Playwright) for '%s'", item.platform.value, item.raw_input)

        fetch = await browser.fetch_rendered(
            item.profile_url,
            timeout=self._request_timeout,
            wait_selector=_BROWSER_WAIT_SELECTORS.get(item.platform),
        )
        if fetch.error_type == "BROWSER_UNAVAILABLE":
            return http_result

        return adapter.extract_from_response(
            item,
            fetch,
            acquisition_method=AcquisitionMethod.BROWSER,
            progress_cb=progress_cb,
        )

    async def extract(
        self,
        item: NormalizedInput,
        progress_cb: ProgressCallback | None = None,
    ) -> SearchResult:
        """Execute extraction pipeline (Method A HTTP + optional Method B Browser fallback)."""
        start = time.perf_counter()
        if not item.is_valid:
            reason = item.error_message or "Invalid input."
            status_tag = item.validation_status.value
            return SearchResult(
                platform=item.platform.value,
                input=item.raw_input,
                username=item.username,
                profile_url=item.profile_url,
                user_id="",
                id_type=IdType.UNKNOWN.value,
                acquisition_method=AcquisitionMethod.NONE.value,
                status=ResultStatus.INVALID_INPUT.value,
                confidence=Confidence.NOT_VERIFIED.value,
                validation_status=ResultValidationStatus.INVALID_INPUT.value,
                extraction_method=ExtractionMethod.NONE.value,
                error_message=f"[{status_tag}] {reason}",
                response_time=round(time.perf_counter() - start, 3),
            )

        adapter = self.get_adapter(item.platform)
        if adapter is None:
            return SearchResult(
                platform=item.platform.value,
                input=item.raw_input,
                acquisition_method=AcquisitionMethod.NONE.value,
                status=ResultStatus.ERROR.value,
                validation_status=ResultValidationStatus.INVALID_INPUT.value,
                error_message=f"Unsupported platform: {item.platform.value}",
                response_time=round(time.perf_counter() - start, 3),
            )

        if progress_cb:
            progress_cb(f"[{item.platform.display_name}] Dispatching to {adapter.__class__.__name__}")
        logger.info("Running %s for input '%s'", adapter.__class__.__name__, item.raw_input)

        async def _pipeline() -> SearchResult:
            http_res = await adapter.extract_user_id(
                item,
                timeout=self._request_timeout,
                retry_limit=self._retry_limit,
                progress_cb=progress_cb,
                http_client=self._http_client,
            )
            http_res = adapter.validate_result(http_res, item)
            if self._should_try_browser_fallback(item, http_res):
                browser_res = await self._run_browser_fallback(adapter, item, http_res, progress_cb=progress_cb)
                return adapter.validate_result(browser_res, item)
            return http_res

        try:
            result = await asyncio.wait_for(_pipeline(), timeout=self._item_timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] Per-item timeout (%.2fs) exceeded for '%s'",
                item.platform.value,
                self._item_timeout,
                item.raw_input,
            )
            return SearchResult(
                platform=item.platform.value,
                input=item.raw_input,
                username=item.username,
                profile_url=item.profile_url,
                acquisition_method=AcquisitionMethod.HTTP.value,
                status=ResultStatus.TIMEOUT.value,
                confidence=Confidence.NOT_VERIFIED.value,
                validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
                error_message=f"Per-item timeout ({self._item_timeout:.1f}s) exceeded.",
                response_time=round(time.perf_counter() - start, 3),
            )
        except Exception as exc:
            logger.exception("[%s] Unexpected extraction error for '%s': %s", item.platform.value, item.raw_input, exc)
            return SearchResult(
                platform=item.platform.value,
                input=item.raw_input,
                username=item.username,
                profile_url=item.profile_url,
                acquisition_method=AcquisitionMethod.HTTP.value,
                status=ResultStatus.ERROR.value,
                confidence=Confidence.NOT_VERIFIED.value,
                validation_status=ResultValidationStatus.UNVERIFIED.value,
                error_message=f"Extraction failed: {type(exc).__name__}",
                response_time=round(time.perf_counter() - start, 3),
            )

        result.response_time = round(time.perf_counter() - start, 3)
        return result
