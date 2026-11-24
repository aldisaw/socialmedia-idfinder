"""Non-blocking QThread bridge for running asyncio single and batch extractions."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QThread, Signal

from config import AppSettings
from core.engine import ExtractionEngine
from core.http_client import HttpClient
from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from core.scheduler import BatchScheduler, PlatformCooldownTracker

logger = logging.getLogger("social_id_finder.worker")


class SearchWorker(QThread):
    """Runs single or batch async extractions off the Qt GUI thread with clean cancellation."""

    progress_signal = Signal(str)
    item_started_signal = Signal(int, object)
    item_progress_signal = Signal(int, str)
    item_finished_signal = Signal(int, object)
    all_finished_signal = Signal(list)
    shutdown_complete_signal = Signal()
    error_signal = Signal(str)

    def __init__(
        self,
        items: list[NormalizedInput],
        settings: AppSettings,
        is_batch: bool = False,
        indices_to_run: list[int] | None = None,
        existing_results: list[SearchResult | None] | None = None,
        cooldown_tracker: PlatformCooldownTracker | None = None,
        http_client: HttpClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items = items
        self._settings = settings
        self._is_batch = is_batch
        self._indices_to_run = indices_to_run
        self._existing_results = existing_results
        self._cooldown_tracker = cooldown_tracker
        self._http_client = http_client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler: BatchScheduler | None = None
        self._single_task: asyncio.Task | None = None
        self._cancel_requested = False

    def request_cancel(self) -> None:
        """Non-blocking cancellation signal dispatched thread-safely onto the worker event loop."""
        self._cancel_requested = True
        loop = self._loop
        if loop and loop.is_running():
            if self._scheduler is not None:
                loop.call_soon_threadsafe(self._scheduler.cancel)
            if self._single_task is not None and not self._single_task.done():
                loop.call_soon_threadsafe(self._single_task.cancel)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(self._execute())
            self.all_finished_signal.emit(results)
        except asyncio.CancelledError:
            self.progress_signal.emit("Search cancelled.")
            self.all_finished_signal.emit([])
        except Exception as exc:
            logger.exception("Worker error: %s", exc)
            self.error_signal.emit(str(exc))
            self.all_finished_signal.emit([])
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:
                logger.debug("Loop teardown note: %s", exc)
            finally:
                loop.close()
                self._loop = None
                self._scheduler = None
                self._single_task = None
                self.shutdown_complete_signal.emit()

    async def _execute(self) -> list[SearchResult]:
        async with ExtractionEngine(settings=self._settings, http_client=self._http_client) as engine:
            if not self._is_batch and len(self._items) == 1:
                item = self._items[0]
                if self._cancel_requested:
                    res = SearchResult(
                        platform=item.platform.value,
                        input=item.raw_input,
                        username=item.username,
                        profile_url=item.profile_url,
                        id_type=IdType.UNKNOWN.value,
                        extraction_method=ExtractionMethod.NONE.value,
                        confidence=Confidence.NOT_VERIFIED.value,
                        validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
                        status=ResultStatus.CANCELLED.value,
                        error_message="Cancelled by user.",
                    )
                    self.item_finished_signal.emit(0, res)
                    return [res]

                self.item_started_signal.emit(0, item)

                def _single_cb(msg: str) -> None:
                    self.item_progress_signal.emit(0, msg)
                    self.progress_signal.emit(msg)

                task = asyncio.create_task(engine.extract(item, progress_cb=_single_cb))
                self._single_task = task
                try:
                    result = await task
                except asyncio.CancelledError:
                    res = SearchResult(
                        platform=item.platform.value,
                        input=item.raw_input,
                        username=item.username,
                        profile_url=item.profile_url,
                        id_type=IdType.UNKNOWN.value,
                        extraction_method=ExtractionMethod.NONE.value,
                        confidence=Confidence.NOT_VERIFIED.value,
                        validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
                        status=ResultStatus.CANCELLED.value,
                        error_message="Cancelled by user.",
                    )
                    self.item_finished_signal.emit(0, res)
                    return [res]
                finally:
                    self._single_task = None

                self.item_finished_signal.emit(0, result)
                return [result]

            scheduler = BatchScheduler(
                engine=engine,
                settings=self._settings,
                cooldown_tracker=self._cooldown_tracker,
            )
            self._scheduler = scheduler

            if self._cancel_requested:
                # Immediately produce cancelled entries for target indices while preserving existing
                target = (
                    list(self._indices_to_run)
                    if self._indices_to_run is not None
                    else list(range(len(self._items)))
                )
                out: list[SearchResult | None] = (
                    list(self._existing_results) if self._existing_results else [None] * len(self._items)
                )
                for idx in target:
                    prev = out[idx]
                    if prev is not None and prev.status == ResultStatus.FOUND.value:
                        continue
                    itm = self._items[idx]
                    res = SearchResult(
                        platform=itm.platform.value,
                        input=itm.raw_input,
                        username=itm.username,
                        profile_url=itm.profile_url,
                        status=ResultStatus.CANCELLED.value,
                        error_message="Cancelled by user.",
                    )
                    out[idx] = res
                    self.item_finished_signal.emit(idx, res)
                return [r for r in out if r is not None]

            return await scheduler.run_batch(
                self._items,
                on_item_start=lambda idx, itm: self.item_started_signal.emit(idx, itm),
                on_item_done=lambda idx, res: self.item_finished_signal.emit(idx, res),
                on_item_progress=lambda idx, msg: self.item_progress_signal.emit(idx, msg),
                on_progress=self.progress_signal.emit,
                indices_to_run=self._indices_to_run,
                existing_results=self._existing_results,
            )
