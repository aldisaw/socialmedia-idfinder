"""Concurrent batch scheduler with bounded worker pool, timeouts, rate-limit cooldowns, and clean cancellation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable

from config import (
    AppSettings,
    DEFAULT_RATE_LIMIT_COOLDOWN,
    MAX_BATCH_SIZE,
    MAX_CONCURRENCY,
    MIN_CONCURRENCY,
)
from core.engine import ExtractionEngine
from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)

logger = logging.getLogger("social_id_finder.scheduler")

RETRYABLE_STATUSES: frozenset[str] = frozenset(
    {
        ResultStatus.ERROR.value,
        ResultStatus.TIMEOUT.value,
        ResultStatus.CANCELLED.value,
        ResultStatus.RATE_LIMITED.value,
    }
)

_RE_RETRY_AFTER_MSG = re.compile(r"Retry-After:\s*(\d+(?:\.\d+)?)s", re.I)


class PlatformCooldownTracker:
    """Tracks per-platform rate-limit cooldown windows to avoid hammering HTTP 429 platforms."""

    def __init__(self, default_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN) -> None:
        self._default_cooldown = max(0.0, float(default_cooldown))
        self._until: dict[str, float] = {}

    def record_rate_limit(self, platform: str, retry_after: float | None = None) -> float:
        duration = float(retry_after) if (retry_after is not None and retry_after > 0) else self._default_cooldown
        until = time.monotonic() + duration
        prev = self._until.get(platform, 0.0)
        if until > prev:
            self._until[platform] = until
        return duration

    def remaining(self, platform: str) -> float:
        until = self._until.get(platform, 0.0)
        rem = until - time.monotonic()
        return rem if rem > 0 else 0.0

    def update_default_cooldown(self, default_cooldown: float) -> None:
        """Update default cooldown duration without discarding active platform cooldowns (L-02)."""
        self._default_cooldown = max(0.0, float(default_cooldown))

    def clear(self) -> None:
        self._until.clear()


def _parse_cooldown_from_result(result: SearchResult) -> float | None:
    if not result.error_message:
        return None
    m = _RE_RETRY_AFTER_MSG.search(result.error_message)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


class BatchScheduler:
    """Bounded-concurrency async batch scheduler preserving order, partial results, and clean shutdown."""

    def __init__(
        self,
        engine: ExtractionEngine,
        settings: AppSettings | None = None,
        cooldown_tracker: PlatformCooldownTracker | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings or AppSettings()
        self.cooldown_tracker = cooldown_tracker or PlatformCooldownTracker()
        self._cancel_event = asyncio.Event()
        self._active_workers: set[asyncio.Task] = set()
        self._active_canonical_keys: set[str] = set()

    def cancel(self) -> None:
        """Stop scheduling new items and cancel in-flight worker tasks cleanly."""
        self._cancel_event.set()
        for task in list(self._active_workers):
            if not task.done():
                task.cancel()

    def reset(self) -> None:
        self._cancel_event.clear()
        self._active_workers.clear()
        self._active_canonical_keys.clear()

    @staticmethod
    def select_retryable_indices(
        items: list[NormalizedInput],
        previous_results: list[SearchResult | None],
    ) -> list[int]:
        """Return indices eligible for Retry Failed (excludes FOUND, NOT_FOUND, BLOCKED, INVALID_INPUT)."""
        eligible: list[int] = []
        for idx, item in enumerate(items):
            if not item.is_valid:
                continue
            prev = previous_results[idx] if idx < len(previous_results) else None
            if prev is None:
                eligible.append(idx)
            elif prev.status in RETRYABLE_STATUSES:
                eligible.append(idx)
        return eligible

    def _make_terminal_result(
        self,
        item: NormalizedInput,
        status: ResultStatus,
        message: str,
        val_status: ResultValidationStatus = ResultValidationStatus.NOT_AVAILABLE,
    ) -> SearchResult:
        return SearchResult(
            platform=item.platform.value,
            input=item.raw_input,
            username=item.username,
            profile_url=item.profile_url,
            user_id="",
            id_type=IdType.UNKNOWN.value,
            extraction_method=ExtractionMethod.NONE.value,
            extraction_source="",
            confidence=Confidence.NOT_VERIFIED.value,
            validation_status=val_status.value,
            status=status.value,
            response_time=0.0,
            error_message=message,
        )

    async def run_batch(
        self,
        items: list[NormalizedInput],
        on_item_start: Callable[[int, NormalizedInput], None] | None = None,
        on_item_done: Callable[[int, SearchResult], None] | None = None,
        on_item_progress: Callable[[int, str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        indices_to_run: list[int] | None = None,
        existing_results: list[SearchResult | None] | None = None,
    ) -> list[SearchResult]:
        if len(items) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch exceeds maximum allowed size of {MAX_BATCH_SIZE}.")

        self.reset()
        concurrency = max(MIN_CONCURRENCY, min(int(self.settings.max_concurrency), MAX_CONCURRENCY))
        batch_timeout = max(0.05, float(self.settings.batch_timeout))
        semaphore = asyncio.Semaphore(concurrency)

        results: list[SearchResult | None] = [None] * len(items)
        if existing_results:
            for i in range(min(len(items), len(existing_results))):
                if existing_results[i] is not None:
                    results[i] = existing_results[i]

        target_indices = (
            list(indices_to_run)
            if indices_to_run is not None
            else list(range(len(items)))
        )

        # Preserve order; reject invalid inputs and duplicate canonical keys before queuing network work.
        queue: asyncio.Queue[tuple[int, NormalizedInput]] = asyncio.Queue()
        seen_canonical: set[str] = set()
        in_flight_indices: set[int] = set()

        for idx in target_indices:
            item = items[idx]
            prev = results[idx]
            # Never overwrite an already-successful FOUND result during a retry
            if prev is not None and prev.status == ResultStatus.FOUND.value:
                continue

            if not item.is_valid:
                status_tag = item.validation_status.value
                res = self._make_terminal_result(
                    item,
                    ResultStatus.INVALID_INPUT,
                    f"[{status_tag}] {item.error_message or 'Invalid input.'}",
                    val_status=ResultValidationStatus.INVALID_INPUT,
                )
                results[idx] = res
                if on_item_done:
                    on_item_done(idx, res)
                continue

            ckey = item.canonical_key
            if ckey and ckey in seen_canonical:
                res = self._make_terminal_result(
                    item,
                    ResultStatus.CANCELLED,
                    "Skipped duplicate canonical entry in batch.",
                    val_status=ResultValidationStatus.INVALID_INPUT,
                )
                results[idx] = res
                if on_item_done:
                    on_item_done(idx, res)
                continue

            if ckey:
                seen_canonical.add(ckey)
            queue.put_nowait((idx, item))

        timed_out = False

        async def _worker() -> None:
            while not self._cancel_event.is_set():
                try:
                    idx, item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                ckey = item.canonical_key
                if ckey and ckey in self._active_canonical_keys:
                    res = self._make_terminal_result(
                        item,
                        ResultStatus.CANCELLED,
                        "Skipped concurrent duplicate canonical entry.",
                    )
                    results[idx] = res
                    if on_item_done:
                        on_item_done(idx, res)
                    queue.task_done()
                    continue

                # Check per-platform HTTP 429 cooldown before acquiring slot
                rem = self.cooldown_tracker.remaining(item.platform.value)
                if rem > 0:
                    msg = f"Skipped: {item.platform.display_name} rate-limit cooldown active ({rem:.1f}s remaining)."
                    res = self._make_terminal_result(item, ResultStatus.RATE_LIMITED, msg)
                    results[idx] = res
                    if on_item_progress:
                        on_item_progress(idx, msg)
                    if on_item_done:
                        on_item_done(idx, res)
                    queue.task_done()
                    continue

                if ckey:
                    self._active_canonical_keys.add(ckey)
                in_flight_indices.add(idx)

                try:
                    async with semaphore:
                        if self._cancel_event.is_set():
                            return
                        # Re-check per-platform HTTP 429 cooldown after acquiring slot (L-02)
                        rem_inner = self.cooldown_tracker.remaining(item.platform.value)
                        if rem_inner > 0:
                            msg = f"Skipped: {item.platform.display_name} rate-limit cooldown active ({rem_inner:.1f}s remaining)."
                            res = self._make_terminal_result(item, ResultStatus.RATE_LIMITED, msg)
                            results[idx] = res
                            if on_item_progress:
                                on_item_progress(idx, msg)
                            if on_item_done:
                                on_item_done(idx, res)
                            continue
                        if on_item_start:
                            on_item_start(idx, item)

                        def _emit_progress(msg: str, i: int = idx) -> None:
                            if on_item_progress:
                                on_item_progress(i, msg)
                            if on_progress:
                                on_progress(msg)

                        res = await self.engine.extract(item, progress_cb=_emit_progress)

                        if res.status == ResultStatus.RATE_LIMITED.value:
                            ra = _parse_cooldown_from_result(res)
                            self.cooldown_tracker.record_rate_limit(item.platform.value, retry_after=ra)

                        # Protect previously successful result if ever present
                        prev = results[idx]
                        if prev is None or prev.status != ResultStatus.FOUND.value:
                            results[idx] = res
                            if on_item_done:
                                on_item_done(idx, res)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Isolated batch worker error on index %d: %s", idx, exc)
                    res = self._make_terminal_result(
                        item,
                        ResultStatus.ERROR,
                        f"Batch worker error: {type(exc).__name__}",
                        val_status=ResultValidationStatus.UNVERIFIED,
                    )
                    results[idx] = res
                    if on_item_done:
                        on_item_done(idx, res)
                finally:
                    in_flight_indices.discard(idx)
                    if ckey:
                        self._active_canonical_keys.discard(ckey)
                    queue.task_done()

        num_workers = min(concurrency, queue.qsize())
        if num_workers > 0:
            workers = [asyncio.create_task(_worker()) for _ in range(num_workers)]
            self._active_workers.update(workers)

            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True),
                    timeout=batch_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning("Batch timeout (%.2fs) exceeded — stopping scheduling and cancelling pending tasks.", batch_timeout)
                self._cancel_event.set()
                for w in workers:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            except asyncio.CancelledError:
                self._cancel_event.set()
                for w in workers:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
            finally:
                # Ensure zero orphaned worker tasks
                for w in workers:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                self._active_workers.clear()

        # Drain any remaining queued entries and mark unfinished entries deterministically
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

        for idx in target_indices:
            if results[idx] is not None and (
                existing_results is None
                or results[idx] is not existing_results[idx]
                or results[idx].status == ResultStatus.FOUND.value
            ):
                continue
            item = items[idx]
            if timed_out:
                res = self._make_terminal_result(
                    item,
                    ResultStatus.TIMEOUT,
                    f"Batch timeout ({batch_timeout:.1f}s) exceeded.",
                )
            else:
                res = self._make_terminal_result(
                    item,
                    ResultStatus.CANCELLED,
                    "Cancelled by user.",
                )
            results[idx] = res
            if on_item_done:
                on_item_done(idx, res)

        return [r for r in results if r is not None]
