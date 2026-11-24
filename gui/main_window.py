"""Main desktop window coordinating sidebar navigation, SQLite persistence, batch recovery, and async worker."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME, APP_VERSION, AppSettings, get_app_data_dir, get_resource_path
from core.logger import attach_ui_log_callback, setup_logging, shutdown_logging
from core.models import NormalizedInput, Platform, ResultStatus, SearchResult
from core.normalizer import normalize_batch, normalize_input
from core.scheduler import BatchScheduler, PlatformCooldownTracker
from database import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_TIMEOUT,
    BatchItemRecord,
    BatchRepository,
    DatabaseCorruptionError,
    DatabaseManager,
    HistoryRepository,
    SettingsRepository,
)
from gui.pages.batch_search_page import BatchSearchPage
from gui.pages.dashboard_page import DashboardPage
from gui.pages.history_page import HistoryPage
from gui.pages.results_page import ResultsPage
from gui.pages.settings_page import SettingsPage
from gui.pages.single_search_page import SingleSearchPage
from gui.worker import SearchWorker

logger = logging.getLogger("social_id_finder.gui")


class MainWindow(QMainWindow):
    """Primary Social ID Finder application window."""

    _log_signal = Signal(str, str)

    def __init__(self, db_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1240, 780)
        self.setMinimumSize(960, 640)
        icon_path = get_resource_path("resources/app_icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Configure sanitized AppData logging before DB initialization so startup/corruption errors are logged (L-04)
        setup_logging(debug=False)

        self._db = DatabaseManager(db_path=db_path)
        self._db_error: str = ""
        try:
            self._db.initialize()
        except DatabaseCorruptionError as exc:
            self._db_error = str(exc)
            logger.error("%s", self._db_error)

        self._settings_repo = SettingsRepository(self._db)
        self._repo = HistoryRepository(self._db)
        self._batch_repo = BatchRepository(self._db)

        self._settings: AppSettings = self._settings_repo.load() if not self._db_error else AppSettings()
        self._cooldown_tracker = PlatformCooldownTracker(
            default_cooldown=self._settings.rate_limit_cooldown
        )

        # Detect batches left in RUNNING state on previous crash/exit (never auto-start network work)
        self._interrupted_batch_ids: list[int] = []
        if not self._db_error:
            try:
                self._interrupted_batch_ids = self._batch_repo.mark_running_batches_interrupted()
            except Exception as exc:
                logger.warning("Interrupted batch check failed: %s", exc)

        self._worker: SearchWorker | None = None
        self._is_shutting_down: bool = False
        self._cancel_requested_for_batch: bool = False
        self._active_mode: str = ""
        self._active_is_retry: bool = False
        self._active_canonical_keys: set[str] = set()
        self._session_results: list[SearchResult] = []

        # Retained batch context for persistence, Resume, and Retry Failed
        self._active_batch_id: int | None = None
        self._batch_items: list[NormalizedInput] = []
        self._batch_item_records: list[BatchItemRecord] = []
        self._batch_results: list[SearchResult | None] = []

        setup_logging(debug=self._settings.debug_mode)
        self._log_signal.connect(self._append_log_line)
        self._log_handler = attach_ui_log_callback(lambda lvl, msg: self._log_signal.emit(lvl, msg))

        self._nav_buttons: dict[str, QPushButton] = {}
        self._page_indices: dict[str, int] = {}

        self._setup_ui()
        self._load_persisted_state()
        self._navigate("dashboard")
        logger.info("%s v%s initialized (db=%s).", APP_NAME, APP_VERSION, self._db.db_path)

        if self._db_error:
            QMessageBox.critical(self, "Database Recovery Required", self._db_error)

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(14, 18, 14, 16)
        sb_layout.setSpacing(6)

        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        subtitle = QLabel(f"Native Desktop  •  v{APP_VERSION}")
        subtitle.setObjectName("AppSubtitle")
        sb_layout.addWidget(title)
        sb_layout.addWidget(subtitle)
        sb_layout.addSpacing(14)

        nav_items = [
            ("dashboard", "Dashboard"),
            ("single", "Single Search"),
            ("batch", "Batch Search"),
            ("results", "Results"),
            ("history", "History"),
            ("settings", "Settings"),
        ]
        for key, label in nav_items:
            btn = QPushButton(label)
            btn.setObjectName("NavButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._navigate(k))
            self._nav_buttons[key] = btn
            sb_layout.addWidget(btn)

        sb_layout.addStretch()

        self._btn_global_stop = QPushButton("Stop All Tasks")
        self._btn_global_stop.setObjectName("DangerButton")
        self._btn_global_stop.setEnabled(False)
        self._btn_global_stop.clicked.connect(self._stop_all)
        sb_layout.addWidget(self._btn_global_stop)

        self._btn_toggle_log = QPushButton("Hide Activity Log")
        self._btn_toggle_log.setObjectName("SecondaryButton")
        self._btn_toggle_log.clicked.connect(self._toggle_log_panel)
        sb_layout.addWidget(self._btn_toggle_log)

        self._btn_about = QPushButton("About")
        self._btn_about.setObjectName("SecondaryButton")
        self._btn_about.clicked.connect(self._show_about_dialog)
        sb_layout.addWidget(self._btn_about)

        outer.addWidget(sidebar)

        # Right column: Stacked pages + Live Activity Log drawer
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)

        self._stack = QStackedWidget()
        self._dashboard_page = DashboardPage()
        self._single_page = SingleSearchPage()
        self._batch_page = BatchSearchPage()
        self._results_page = ResultsPage()
        self._history_page = HistoryPage(self._repo)
        self._settings_page = SettingsPage(self._settings)

        pages = [
            ("dashboard", self._dashboard_page),
            ("single", self._single_page),
            ("batch", self._batch_page),
            ("results", self._results_page),
            ("history", self._history_page),
            ("settings", self._settings_page),
        ]
        for idx, (key, page) in enumerate(pages):
            self._stack.addWidget(page)
            self._page_indices[key] = idx

        right_col.addWidget(self._stack, 1)

        # Collapsible Activity Log panel
        self._log_frame = QFrame()
        self._log_frame.setStyleSheet("background-color: #0B1120; border-top: 1px solid #1E293B;")
        log_layout = QVBoxLayout(self._log_frame)
        log_layout.setContentsMargins(14, 8, 14, 10)
        log_layout.setSpacing(6)

        log_hdr = QHBoxLayout()
        lbl_log = QLabel("Live Activity & Strategy Log")
        lbl_log.setObjectName("SectionLabel")
        btn_clear_log = QPushButton("Clear")
        btn_clear_log.setObjectName("SmallButton")
        btn_clear_log.clicked.connect(lambda: self._log_console.clear())
        log_hdr.addWidget(lbl_log)
        log_hdr.addStretch()
        log_hdr.addWidget(btn_clear_log)
        log_layout.addLayout(log_hdr)

        self._log_console = QPlainTextEdit()
        self._log_console.setObjectName("LogConsole")
        self._log_console.setReadOnly(True)
        self._log_console.setMaximumHeight(105)
        log_layout.addWidget(self._log_console)
        right_col.addWidget(self._log_frame)

        outer.addLayout(right_col, 1)

        # Wire signals
        self._dashboard_page.navigate_requested.connect(self._navigate)
        self._single_page.search_requested.connect(self._start_single_search)
        self._single_page.stop_requested.connect(self._stop_all)
        self._batch_page.batch_requested.connect(self._start_batch_search)
        self._batch_page.retry_failed_requested.connect(self._start_retry_failed)
        self._batch_page.load_session_requested.connect(self._load_saved_batch)
        self._batch_page.resume_requested.connect(self._resume_saved_batch)
        self._batch_page.stop_requested.connect(self._stop_all)
        self._results_page.status_message.connect(self._show_status)
        self._history_page.status_message.connect(self._show_status)
        self._history_page.history_changed.connect(self._refresh_dashboard_from_db)
        self._settings_page.settings_saved.connect(self._on_settings_saved)

        self.statusBar().showMessage("Ready")

    def _load_persisted_state(self) -> None:
        if self._db_error:
            return
        self._refresh_dashboard_from_db()
        self._history_page.refresh()
        self._refresh_saved_batches()

        if self._interrupted_batch_ids:
            latest_id = self._interrupted_batch_ids[0]
            self._load_saved_batch(latest_id)
            logger.warning(
                "Detected %d interrupted batch session(s): %s. Loaded Batch #%d for manual resume.",
                len(self._interrupted_batch_ids),
                self._interrupted_batch_ids,
                latest_id,
            )

    def _refresh_dashboard_from_db(self) -> None:
        if self._db_error:
            return
        try:
            all_hist = self._repo.list_results()
            self._dashboard_page.update_stats(all_hist)
        except Exception as exc:
            logger.warning("Failed to refresh dashboard stats: %s", exc)

    def _refresh_saved_batches(self) -> None:
        if self._db_error:
            return
        try:
            sessions = self._batch_repo.list_sessions(limit=25)
            self._batch_page.update_saved_sessions(sessions)
        except Exception as exc:
            logger.warning("Failed to refresh saved batches: %s", exc)

    def _navigate(self, key: str) -> None:
        if key not in self._page_indices:
            return
        self._stack.setCurrentIndex(self._page_indices[key])
        for btn_key, btn in self._nav_buttons.items():
            btn.setProperty("active", btn_key == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if key == "history":
            self._history_page.refresh()
        elif key == "batch":
            self._refresh_saved_batches()
        elif key == "dashboard":
            self._refresh_dashboard_from_db()

    def _toggle_log_panel(self) -> None:
        visible = not self._log_frame.isVisible()
        self._log_frame.setVisible(visible)
        self._btn_toggle_log.setText("Hide Activity Log" if visible else "Show Activity Log")

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            (
                f"<h3>{APP_NAME} v{APP_VERSION}</h3>"
                "<p>Native Windows desktop application for extracting publicly available "
                "user IDs from social media profiles without official APIs.</p>"
                "<p><b>Supported Platforms:</b> Instagram, TikTok, X (Twitter), Facebook, YouTube</p>"
                "<p><b>Privacy & Policy:</b> Public profile metadata only. No login bypass, "
                "no CAPTCHA bypass, no cookie harvesting, and no stored credentials.</p>"
                f"<p><b>User Data Directory:</b><br><code>{get_app_data_dir()}</code></p>"
            ),
        )

    def _append_log_line(self, _level: str, msg: str) -> None:
        self._log_console.appendPlainText(msg)

    def _show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)
        logger.info("%s", message)

    def _on_settings_saved(self, settings: AppSettings) -> None:
        saved = self._settings_repo.save(settings) if not self._db_error else settings.clamp()
        self._settings = saved
        # Preserve active HTTP 429 cooldown windows when updating default cooldown (L-02)
        self._cooldown_tracker.update_default_cooldown(saved.rate_limit_cooldown)
        logging.getLogger("social_id_finder").setLevel(
            logging.DEBUG if saved.debug_mode else logging.INFO
        )
        self._show_status(
            f"Settings saved: req={saved.request_timeout}s, item={saved.item_timeout}s, "
            f"batch={saved.batch_timeout}s, concurrency={saved.max_concurrency}, "
            f"retries={saved.retry_limit}, cooldown={saved.rate_limit_cooldown}s, debug={saved.debug_mode}"
        )

    def _is_busy(self) -> bool:
        return self._is_shutting_down or (self._worker is not None and self._worker.isRunning())

    def _start_single_search(self, raw_input: str, selected_platform: Platform | None) -> None:
        if self._is_busy():
            self._show_status("Search or shutdown is in progress. Please wait.")
            return

        normalized = normalize_input(raw_input, selected_platform=selected_platform)
        if normalized.canonical_key and normalized.canonical_key in self._active_canonical_keys:
            self._show_status("This profile is already being searched.")
            return

        if normalized.canonical_key:
            self._active_canonical_keys.add(normalized.canonical_key)

        self._active_mode = "single"
        self._active_is_retry = False
        self._single_page.set_running(True)
        self._btn_global_stop.setEnabled(True)
        self.statusBar().showMessage(f"Searching {raw_input}...")

        self._launch_worker([normalized], is_batch=False)

    def _start_batch_search(self, raw_entries: list[str], default_platform: Platform | None) -> None:
        if self._is_busy():
            self._show_status("Search or shutdown is in progress. Please wait.")
            return

        normalized_items, _ = normalize_batch(raw_entries, selected_platform=default_platform)
        session, item_records = self._batch_repo.create_batch(normalized_items)

        self._active_batch_id = session.id
        self._batch_items = normalized_items
        self._batch_item_records = item_records
        self._batch_results = [None] * len(normalized_items)
        self._cancel_requested_for_batch = False
        self._active_is_retry = False

        for itm in normalized_items:
            if itm.canonical_key:
                self._active_canonical_keys.add(itm.canonical_key)

        self._active_mode = "batch"
        self._batch_page.prepare_table(normalized_items, batch_id=session.id)
        self._batch_page.set_running(True)
        self._btn_global_stop.setEnabled(True)
        self._refresh_saved_batches()
        self.statusBar().showMessage(f"Running Batch #{session.id} ({len(normalized_items)} entries)...")

        self._launch_worker(normalized_items, is_batch=True)

    def _load_saved_batch(self, batch_id: int) -> None:
        if self._is_busy():
            self._show_status("Cannot load a batch while a search is running.")
            return
        loaded = self._batch_repo.load_batch_state(batch_id)
        if loaded is None:
            self._show_status(f"Batch #{batch_id} not found.")
            return
        session, items, item_records, results = loaded
        self._active_batch_id = session.id
        self._batch_items = items
        self._batch_item_records = item_records
        self._batch_results = results
        self._batch_page.restore_session_view(session, items, results)
        self._show_status(
            f"Loaded Batch #{session.id} [{session.status}] ({session.completed_items}/{session.total_items})."
        )

    def _resume_saved_batch(self, batch_id: int) -> None:
        if self._is_busy():
            self._show_status("Search or shutdown is in progress. Please wait.")
            return
        if self._active_batch_id != batch_id:
            self._load_saved_batch(batch_id)
        if not self._batch_items or self._active_batch_id != batch_id:
            return

        indices = BatchRepository.select_resumable_indices(
            self._batch_items, self._batch_item_records, self._batch_results
        )
        if not indices:
            self._show_status(f"Batch #{batch_id} has no pending or retryable entries left.")
            return

        self._batch_repo.mark_batch_running(batch_id)
        self._cancel_requested_for_batch = False
        self._active_is_retry = True

        for idx in indices:
            ck = self._batch_items[idx].canonical_key
            if ck:
                self._active_canonical_keys.add(ck)

        self._active_mode = "batch"
        self._batch_page.prepare_retry(indices)
        self._batch_page.set_running(True)
        self._btn_global_stop.setEnabled(True)
        self._refresh_saved_batches()
        self.statusBar().showMessage(f"Resuming Batch #{batch_id} ({len(indices)} entries)...")

        self._launch_worker(
            self._batch_items,
            is_batch=True,
            indices_to_run=indices,
            existing_results=list(self._batch_results),
        )

    def _start_retry_failed(self) -> None:
        if self._is_busy():
            self._show_status("Search or shutdown is in progress. Please wait.")
            return
        if not self._batch_items:
            return

        indices = BatchScheduler.select_retryable_indices(self._batch_items, self._batch_results)
        if not indices:
            self._show_status("No eligible failed entries to retry.")
            return

        if self._active_batch_id is not None:
            self._batch_repo.mark_batch_running(self._active_batch_id)

        self._cancel_requested_for_batch = False
        self._active_is_retry = True

        for idx in indices:
            ck = self._batch_items[idx].canonical_key
            if ck:
                self._active_canonical_keys.add(ck)

        self._active_mode = "batch"
        self._batch_page.prepare_retry(indices)
        self._batch_page.set_running(True)
        self._btn_global_stop.setEnabled(True)
        self.statusBar().showMessage(f"Retrying {len(indices)} failed batch entries...")

        self._launch_worker(
            self._batch_items,
            is_batch=True,
            indices_to_run=indices,
            existing_results=list(self._batch_results),
        )

    def _launch_worker(
        self,
        items: list[NormalizedInput],
        is_batch: bool,
        indices_to_run: list[int] | None = None,
        existing_results: list[SearchResult | None] | None = None,
    ) -> None:
        worker = SearchWorker(
            items=items,
            settings=self._settings,
            is_batch=is_batch,
            indices_to_run=indices_to_run,
            existing_results=existing_results,
            cooldown_tracker=self._cooldown_tracker,
            parent=self,
        )
        worker.progress_signal.connect(self._on_worker_progress)
        worker.item_started_signal.connect(self._on_item_started)
        worker.item_progress_signal.connect(self._on_item_progress)
        worker.item_finished_signal.connect(self._on_item_finished)
        worker.all_finished_signal.connect(self._on_all_finished)
        worker.shutdown_complete_signal.connect(self._on_worker_shutdown_complete)
        worker.error_signal.connect(lambda err: logger.error("Worker error: %s", err))
        self._worker = worker
        worker.start()

    def _on_worker_progress(self, message: str) -> None:
        logger.info("%s", message)
        if self._active_mode == "single":
            self._single_page.set_progress_text(message)

    def _on_item_started(self, idx: int, _item) -> None:
        if self._active_mode == "batch":
            self._batch_page.mark_item_running(idx)
            if (
                not self._db_error
                and self._active_batch_id is not None
                and 0 <= idx < len(self._batch_item_records)
            ):
                try:
                    self._batch_repo.mark_item_running(
                        self._active_batch_id, self._batch_item_records[idx].id
                    )
                except Exception as exc:
                    logger.warning("Failed to persist RUNNING state for batch item: %s", exc)

    def _on_item_progress(self, idx: int, message: str) -> None:
        if self._active_mode == "batch":
            self._batch_page.mark_item_progress(idx, message)

    def _on_item_finished(self, idx: int, result: SearchResult) -> None:
        if self._active_mode == "batch" and 0 <= idx < len(self._batch_results):
            prev = self._batch_results[idx]
            if prev is not None and prev.status == ResultStatus.FOUND.value:
                return
            self._batch_results[idx] = result

            if (
                not self._db_error
                and self._active_batch_id is not None
                and idx < len(self._batch_item_records)
            ):
                try:
                    self._batch_repo.record_item_result(
                        self._active_batch_id,
                        self._batch_item_records[idx].id,
                        result,
                        increment_retry=self._active_is_retry,
                    )
                except Exception as exc:
                    logger.warning("Failed to persist batch item result: %s", exc)
        elif not self._db_error:
            try:
                self._repo.save_result(result)
            except Exception as exc:
                logger.warning("Failed to persist single search result: %s", exc)

        self._session_results.insert(0, result)
        self._results_page.add_result(result)
        self._refresh_dashboard_from_db()

        if self._active_mode == "single":
            self._single_page.show_result(result)
        elif self._active_mode == "batch":
            self._batch_page.mark_item_done(idx, result)

    def _on_all_finished(self, results: list) -> None:
        if self._active_mode == "batch" and self._active_batch_id is not None and not self._db_error:
            if self._cancel_requested_for_batch:
                final_status = BATCH_STATUS_CANCELLED
            elif any(r.status == ResultStatus.TIMEOUT.value for r in results):
                final_status = BATCH_STATUS_TIMEOUT
            else:
                final_status = BATCH_STATUS_COMPLETED
            try:
                self._batch_repo.finalize_batch(self._active_batch_id, final_status)
                self._batch_item_records = self._batch_repo.get_batch_items(self._active_batch_id)
            except Exception as exc:
                logger.warning("Failed to finalize batch #%s: %s", self._active_batch_id, exc)
            self._refresh_saved_batches()
        self._history_page.refresh()

    def _on_worker_shutdown_complete(self) -> None:
        self._is_shutting_down = False
        self._active_canonical_keys.clear()
        self._single_page.set_running(False)
        self._batch_page.set_shutting_down(False)
        self._batch_page.set_running(False)
        self._btn_global_stop.setEnabled(False)
        self.statusBar().showMessage("Ready")
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _stop_all(self) -> None:
        if self._worker is not None and self._worker.isRunning() and not self._is_shutting_down:
            self._is_shutting_down = True
            self._cancel_requested_for_batch = True
            self._btn_global_stop.setEnabled(False)
            self._batch_page.set_shutting_down(True)
            logger.warning("Stop All requested — cancelling active tasks.")
            self._worker.request_cancel()
            self.statusBar().showMessage("Cancelling tasks...")

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._cancel_requested_for_batch = True
            self._worker.request_cancel()
            self._worker.wait(8000)
        logging.getLogger("social_id_finder").removeHandler(self._log_handler)
        shutdown_logging()
        super().closeEvent(event)
