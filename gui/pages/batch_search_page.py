"""Batch Search page (max 10 entries, live status counters, strategy progress, persistent batch recovery & Resume)."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import MAX_BATCH_SIZE
from core.models import NormalizedInput, Platform, ResultStatus, SUPPORTED_PLATFORMS, SearchResult
from core.normalizer import normalize_batch
from core.scheduler import RETRYABLE_STATUSES
from database.batch_repo import BATCH_STATUS_INTERRUPTED, BatchSessionRecord
from gui.theme import CONFIDENCE_COLORS, PLATFORM_META, STATUS_COLORS, get_platform_icon


class BatchSearchPage(QWidget):
    """Multiline batch extraction interface with strict 10-entry cap, live stats, Resume, and Retry Failed."""

    batch_requested = Signal(list, object)  # list[str], default Platform | None
    resume_requested = Signal(int)  # batch_id
    load_session_requested = Signal(int)  # batch_id
    retry_failed_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[NormalizedInput] = []
        self._row_statuses: list[str] = []
        self._active_batch_id: int | None = None
        self._active_batch_status: str = ""
        self._total_items = 0
        self._completed_items = 0
        self._run_started_at: float = 0.0
        self._is_running = False
        self._is_shutting_down = False

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(100)
        self._elapsed_timer.timeout.connect(self._update_elapsed_label)

        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(10)

        title = QLabel("Batch Search")
        title.setObjectName("PageTitle")
        desc = QLabel(
            f"Enter up to {MAX_BATCH_SIZE} profile URLs or usernames (one per line). "
            "Batch sessions persist in SQLite and interrupted runs can be resumed safely."
        )
        desc.setObjectName("PageDesc")
        root.addWidget(title)
        root.addWidget(desc)

        # Interrupted / Previous sessions recovery bar
        recovery_card = QFrame()
        recovery_card.setObjectName("Card")
        rec_layout = QHBoxLayout(recovery_card)
        rec_layout.setContentsMargins(14, 8, 14, 8)
        rec_layout.setSpacing(10)

        rec_lbl = QLabel("Saved Sessions:")
        rec_lbl.setObjectName("SectionLabel")

        self._session_combo = QComboBox()
        self._session_combo.setMinimumWidth(310)
        self._session_combo.addItem("No saved batches yet", None)

        self._btn_load_session = QPushButton("Load")
        self._btn_load_session.setObjectName("SecondaryButton")
        self._btn_load_session.setEnabled(False)
        self._btn_load_session.clicked.connect(self._on_load_session_clicked)

        self._btn_resume = QPushButton("Resume Batch")
        self._btn_resume.setEnabled(False)
        self._btn_resume.clicked.connect(self._on_resume_clicked)

        self._interrupted_banner = QLabel("")
        self._interrupted_banner.setObjectName("WarningLabel")
        self._interrupted_banner.setVisible(False)

        rec_layout.addWidget(rec_lbl)
        rec_layout.addWidget(self._session_combo)
        rec_layout.addWidget(self._btn_load_session)
        rec_layout.addWidget(self._btn_resume)
        rec_layout.addWidget(self._interrupted_banner, 1)
        root.addWidget(recovery_card)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        top_bar = QHBoxLayout()
        fallback_lbl = QLabel("Default platform for plain usernames:")
        fallback_lbl.setStyleSheet("color: #94A3B8; background: transparent;")
        self._default_platform = QComboBox()
        self._default_platform.setMinimumWidth(160)
        self._default_platform.addItem("URL Auto-detect Only", None)
        for plat in SUPPORTED_PLATFORMS:
            self._default_platform.addItem(get_platform_icon(plat.value, 16), plat.display_name, plat)
        self._default_platform.currentIndexChanged.connect(lambda _: self._validate_live())

        self._count_label = QLabel(f"0 / {MAX_BATCH_SIZE} unique entries (Valid: 0 | Invalid: 0 | Dup: 0)")
        self._count_label.setObjectName("SectionLabel")

        top_bar.addWidget(fallback_lbl)
        top_bar.addWidget(self._default_platform)
        top_bar.addStretch()
        top_bar.addWidget(self._count_label)
        card_layout.addLayout(top_bar)

        self._input_area = QPlainTextEdit()
        self._input_area.setPlaceholderText(
            "https://www.youtube.com/@Google\n"
            "https://www.instagram.com/instagram/\n"
            "https://www.tiktok.com/@tiktok\n"
            "https://x.com/X"
        )
        self._input_area.setMinimumHeight(86)
        self._input_area.setMaximumHeight(115)
        self._input_area.textChanged.connect(self._validate_live)
        card_layout.addWidget(self._input_area)

        self._warning_label = QLabel("")
        self._warning_label.setObjectName("WarningLabel")
        self._warning_label.setVisible(False)
        card_layout.addWidget(self._warning_label)

        controls = QHBoxLayout()
        self._btn_start = QPushButton("Start Batch")
        self._btn_start.setEnabled(False)
        self._btn_start.clicked.connect(self._on_start_clicked)

        self._btn_stop = QPushButton("Stop All")
        self._btn_stop.setObjectName("DangerButton")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_requested.emit)

        self._btn_retry_failed = QPushButton("Retry Failed")
        self._btn_retry_failed.setObjectName("SecondaryButton")
        self._btn_retry_failed.setEnabled(False)
        self._btn_retry_failed.clicked.connect(self.retry_failed_requested.emit)

        self._btn_clear = QPushButton("Clear Input")
        self._btn_clear.setObjectName("SecondaryButton")
        self._btn_clear.clicked.connect(self._input_area.clear)

        self._progress = QProgressBar()
        self._progress.setValue(0)
        self._progress.setFormat("Idle")

        controls.addWidget(self._btn_start)
        controls.addWidget(self._btn_stop)
        controls.addWidget(self._btn_retry_failed)
        controls.addWidget(self._btn_clear)
        controls.addWidget(self._progress, 1)
        card_layout.addLayout(controls)

        live_strip = QHBoxLayout()
        live_strip.setSpacing(16)
        self._lbl_current_platform = QLabel("Platform: —")
        self._lbl_current_platform.setObjectName("PageDesc")
        self._lbl_current_strategy = QLabel("Strategy: Idle")
        self._lbl_current_strategy.setObjectName("PageDesc")
        self._lbl_elapsed = QLabel("Elapsed: 0.0s")
        self._lbl_elapsed.setObjectName("SectionLabel")
        live_strip.addWidget(self._lbl_current_platform)
        live_strip.addWidget(self._lbl_current_strategy, 1)
        live_strip.addWidget(self._lbl_elapsed)
        card_layout.addLayout(live_strip)
        root.addWidget(card)

        counters_card = QFrame()
        counters_card.setObjectName("Card")
        counters_layout = QHBoxLayout(counters_card)
        counters_layout.setContentsMargins(12, 8, 12, 8)
        counters_layout.setSpacing(14)

        self._counter_labels: dict[str, QLabel] = {}
        counter_specs = [
            ("total", "Total", "#F8FAFC"),
            ("queued", "Queued", "#94A3B8"),
            ("running", "Running", "#38BDF8"),
            ("found", "Found", "#22C55E"),
            ("not_found", "Not Found", "#94A3B8"),
            ("blocked", "Blocked", "#EF4444"),
            ("error", "Error", "#EF4444"),
            ("timeout", "Timeout", "#F97316"),
            ("cancelled", "Cancelled", "#A855F7"),
        ]
        for key, label_text, color in counter_specs:
            lbl = QLabel(f"{label_text}: 0")
            lbl.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600; background: transparent;")
            self._counter_labels[key] = lbl
            counters_layout.addWidget(lbl)
        counters_layout.addStretch()
        root.addWidget(counters_card)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["#", "Input", "Platform", "Status", "Progress / Strategy", "User ID", "Confidence", "Time"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._table, 1)

    def update_saved_sessions(self, sessions: list[BatchSessionRecord]) -> None:
        """Populate saved & recoverable sessions dropdown and interrupted alert banner."""
        prev_id = self._session_combo.currentData()
        self._session_combo.blockSignals(True)
        self._session_combo.clear()

        interrupted = [s for s in sessions if s.status == BATCH_STATUS_INTERRUPTED]
        if not sessions:
            self._session_combo.addItem("No saved batches yet", None)
        else:
            for s in sessions:
                label = f"Batch #{s.id} [{s.status}] {s.completed_items}/{s.total_items} — {s.created_at}"
                self._session_combo.addItem(label, s.id)
            if prev_id is not None:
                idx = self._session_combo.findData(prev_id)
                if idx >= 0:
                    self._session_combo.setCurrentIndex(idx)
        self._session_combo.blockSignals(False)

        if interrupted:
            self._interrupted_banner.setText(
                f"{len(interrupted)} interrupted batch(es) ready to recover (click Load / Resume Batch)."
            )
            self._interrupted_banner.setVisible(True)
        else:
            self._interrupted_banner.setVisible(False)

        self._update_session_buttons()

    def _update_session_buttons(self) -> None:
        has_sel = self._session_combo.currentData() is not None
        idle = (not self._is_running) and (not self._is_shutting_down)
        self._btn_load_session.setEnabled(idle and has_sel)
        can_resume = idle and self._active_batch_id is not None and self.has_resumable_entries()
        self._btn_resume.setEnabled(can_resume)

    def _on_load_session_clicked(self) -> None:
        bid = self._session_combo.currentData()
        if bid is not None:
            self.load_session_requested.emit(int(bid))

    def _on_resume_clicked(self) -> None:
        if self._active_batch_id is not None:
            self.resume_requested.emit(int(self._active_batch_id))

    def restore_session_view(
        self,
        session: BatchSessionRecord,
        items: list[NormalizedInput],
        results: list[SearchResult | None],
    ) -> None:
        """Restore persisted batch rows, progress bar, and counters without triggering network work."""
        self._active_batch_id = session.id
        self._active_batch_status = session.status
        self.prepare_table(items)
        for idx, res in enumerate(results):
            if res is not None:
                self.mark_item_done(idx, res)

        sel_idx = self._session_combo.findData(session.id)
        if sel_idx >= 0:
            self._session_combo.setCurrentIndex(sel_idx)
        self._btn_retry_failed.setEnabled(
            (not self._is_running) and (not self._is_shutting_down) and self.has_retryable_entries()
        )
        self._update_session_buttons()

    def parse_entries(self) -> tuple[list[str], int]:
        """Return canonically deduplicated non-empty inputs and removed duplicate count."""
        raw_lines = [line.strip() for line in self._input_area.toPlainText().splitlines() if line.strip()]
        normalized, dups = normalize_batch(raw_lines, selected_platform=self._default_platform.currentData())
        return [item.raw_input for item in normalized], dups

    def _validate_live(self) -> None:
        raw_lines = [line.strip() for line in self._input_area.toPlainText().splitlines() if line.strip()]
        normalized, dups = normalize_batch(raw_lines, selected_platform=self._default_platform.currentData())
        count = len(normalized)
        valid_count = sum(1 for item in normalized if item.is_valid)
        invalid_count = count - valid_count
        self._count_label.setText(
            f"{count} / {MAX_BATCH_SIZE} unique entries (Valid: {valid_count} | Invalid: {invalid_count} | Dup: {dups})"
        )

        warnings: list[str] = []
        if count > MAX_BATCH_SIZE:
            warnings.append(f"Exceeds maximum {MAX_BATCH_SIZE} entries ({count} entered). Reduce input before starting.")
            self._warning_label.setObjectName("ErrorLabel")
            can_start = False
        else:
            self._warning_label.setObjectName("WarningLabel")
            can_start = count > 0 and not self._is_running and not self._is_shutting_down

        if dups > 0:
            warnings.append(f"{dups} normalized duplicate entr{'y' if dups == 1 else 'ies'} ignored.")
        if invalid_count > 0:
            warnings.append(f"{invalid_count} invalid/ambiguous entr{'y' if invalid_count == 1 else 'ies'} will be rejected pre-run.")

        self._btn_start.setEnabled(can_start)
        if warnings:
            self._warning_label.setText("  |  ".join(warnings))
            self._warning_label.setVisible(True)
        else:
            self._warning_label.setVisible(False)
        self._warning_label.style().unpolish(self._warning_label)
        self._warning_label.style().polish(self._warning_label)

    def _on_start_clicked(self) -> None:
        unique, _ = self.parse_entries()
        if not unique or len(unique) > MAX_BATCH_SIZE:
            return
        self.batch_requested.emit(unique, self._default_platform.currentData())

    def prepare_table(self, items: list[NormalizedInput], batch_id: int | None = None) -> None:
        if batch_id is not None:
            self._active_batch_id = batch_id
        self._items = list(items)
        self._total_items = len(items)
        self._completed_items = 0
        self._row_statuses = [ResultStatus.PENDING.value] * len(items)
        self._table.setRowCount(len(items))
        self._progress.setRange(0, max(1, len(items)))
        self._progress.setValue(0)
        self._progress.setFormat(f"0 / {len(items)} completed")

        for idx, item in enumerate(items):
            self._table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self._table.setItem(idx, 1, QTableWidgetItem(item.raw_input))

            _, _, disp = PLATFORM_META.get(item.platform.value, ("?", "#64748B", item.platform.value))
            plat_item = QTableWidgetItem(disp)
            plat_item.setIcon(get_platform_icon(item.platform.value, 16))
            self._table.setItem(idx, 2, plat_item)

            st_item = QTableWidgetItem(ResultStatus.PENDING.value)
            st_item.setForeground(QColor(STATUS_COLORS[ResultStatus.PENDING.value]))
            self._table.setItem(idx, 3, st_item)
            self._table.setItem(idx, 4, QTableWidgetItem("Queued"))
            self._table.setItem(idx, 5, QTableWidgetItem("—"))
            self._table.setItem(idx, 6, QTableWidgetItem("—"))
            self._table.setItem(idx, 7, QTableWidgetItem("—"))
            self._table.setRowHeight(idx, 32)

        self._refresh_counters()

    def prepare_retry(self, indices_to_retry: list[int]) -> None:
        for idx in indices_to_retry:
            if 0 <= idx < len(self._row_statuses):
                self._row_statuses[idx] = ResultStatus.PENDING.value
                st_item = QTableWidgetItem(ResultStatus.PENDING.value)
                st_item.setForeground(QColor(STATUS_COLORS[ResultStatus.PENDING.value]))
                self._table.setItem(idx, 3, st_item)
                self._table.setItem(idx, 4, QTableWidgetItem("Queued (Resume/Retry)"))
                self._table.setItem(idx, 7, QTableWidgetItem("—"))

        self._completed_items = sum(
            1 for s in self._row_statuses if s not in (ResultStatus.PENDING.value, ResultStatus.RUNNING.value)
        )
        self._progress.setRange(0, max(1, self._total_items))
        self._progress.setValue(self._completed_items)
        self._progress.setFormat(f"{self._completed_items} / {self._total_items} completed")
        self._refresh_counters()

    def mark_item_running(self, idx: int) -> None:
        if 0 <= idx < self._table.rowCount():
            self._row_statuses[idx] = ResultStatus.RUNNING.value
            item = QTableWidgetItem(ResultStatus.RUNNING.value)
            item.setForeground(QColor(STATUS_COLORS[ResultStatus.RUNNING.value]))
            self._table.setItem(idx, 3, item)
            self._table.setItem(idx, 4, QTableWidgetItem("Starting..."))
            if idx < len(self._items):
                self._lbl_current_platform.setText(f"Platform: {self._items[idx].platform.display_name}")
            self._refresh_counters()

    def mark_item_progress(self, idx: int, message: str) -> None:
        if 0 <= idx < self._table.rowCount():
            self._table.setItem(idx, 4, QTableWidgetItem(message))
        self._lbl_current_strategy.setText(f"Strategy: {message}")

    def mark_item_done(self, idx: int, result: SearchResult) -> None:
        if 0 <= idx < self._table.rowCount():
            self._row_statuses[idx] = result.status
            st_item = QTableWidgetItem(result.status)
            st_item.setForeground(QColor(STATUS_COLORS.get(result.status, "#94A3B8")))
            if result.error_message:
                st_item.setToolTip(result.error_message)
            self._table.setItem(idx, 3, st_item)

            detail = result.extraction_source or result.error_message or result.status
            self._table.setItem(idx, 4, QTableWidgetItem(detail))
            self._table.setItem(idx, 5, QTableWidgetItem(result.user_id or "—"))

            conf_item = QTableWidgetItem(result.confidence)
            conf_item.setForeground(QColor(CONFIDENCE_COLORS.get(result.confidence, "#94A3B8")))
            self._table.setItem(idx, 6, conf_item)
            self._table.setItem(idx, 7, QTableWidgetItem(f"{result.response_time:.2f}s"))

        self._completed_items = sum(
            1 for s in self._row_statuses if s not in (ResultStatus.PENDING.value, ResultStatus.RUNNING.value)
        )
        self._progress.setValue(self._completed_items)
        self._progress.setFormat(f"{self._completed_items} / {self._total_items} completed")
        self._refresh_counters()

    def _refresh_counters(self) -> None:
        counts = {
            "total": len(self._row_statuses),
            "queued": 0,
            "running": 0,
            "found": 0,
            "not_found": 0,
            "blocked": 0,
            "error": 0,
            "timeout": 0,
            "cancelled": 0,
        }
        for st in self._row_statuses:
            if st == ResultStatus.PENDING.value:
                counts["queued"] += 1
            elif st == ResultStatus.RUNNING.value:
                counts["running"] += 1
            elif st == ResultStatus.FOUND.value:
                counts["found"] += 1
            elif st == ResultStatus.NOT_FOUND.value:
                counts["not_found"] += 1
            elif st in (
                ResultStatus.BLOCKED.value,
                ResultStatus.RATE_LIMITED.value,
                ResultStatus.LOGIN_REQUIRED.value,
                ResultStatus.CAPTCHA_REQUIRED.value,
            ):
                counts["blocked"] += 1
            elif st in (ResultStatus.ERROR.value, ResultStatus.INVALID_INPUT.value):
                counts["error"] += 1
            elif st == ResultStatus.TIMEOUT.value:
                counts["timeout"] += 1
            elif st == ResultStatus.CANCELLED.value:
                counts["cancelled"] += 1

        titles = {
            "total": "Total",
            "queued": "Queued",
            "running": "Running",
            "found": "Found",
            "not_found": "Not Found",
            "blocked": "Blocked",
            "error": "Error",
            "timeout": "Timeout",
            "cancelled": "Cancelled",
        }
        for k, lbl in self._counter_labels.items():
            lbl.setText(f"{titles[k]}: {counts[k]}")

    def has_retryable_entries(self) -> bool:
        return any(
            st in RETRYABLE_STATUSES and idx < len(self._items) and self._items[idx].is_valid
            for idx, st in enumerate(self._row_statuses)
        )

    def has_resumable_entries(self) -> bool:
        return any(
            (st in (ResultStatus.PENDING.value, ResultStatus.RUNNING.value) or st in RETRYABLE_STATUSES)
            and idx < len(self._items)
            and self._items[idx].is_valid
            for idx, st in enumerate(self._row_statuses)
        )

    def _update_elapsed_label(self) -> None:
        if self._run_started_at > 0:
            elapsed = time.perf_counter() - self._run_started_at
            self._lbl_elapsed.setText(f"Elapsed: {elapsed:.1f}s")

    def set_shutting_down(self, shutting_down: bool) -> None:
        self._is_shutting_down = shutting_down
        if shutting_down:
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(False)
            self._btn_retry_failed.setEnabled(False)
            self._btn_resume.setEnabled(False)
            self._btn_load_session.setEnabled(False)
            self._lbl_current_strategy.setText("Strategy: Cancelling & closing HTTP connections...")

    def set_running(self, running: bool) -> None:
        self._is_running = running
        if running:
            self._is_shutting_down = False
            self._run_started_at = time.perf_counter()
            self._elapsed_timer.start()
            self._lbl_elapsed.setText("Elapsed: 0.0s")
        else:
            self._elapsed_timer.stop()
            if self._run_started_at > 0:
                self._update_elapsed_label()
            self._lbl_current_strategy.setText("Strategy: Idle")

        can_start = (not running) and (not self._is_shutting_down) and (0 < len(self.parse_entries()[0]) <= MAX_BATCH_SIZE)
        self._btn_start.setEnabled(can_start)
        self._btn_stop.setEnabled(running and not self._is_shutting_down)
        self._btn_retry_failed.setEnabled((not running) and (not self._is_shutting_down) and self.has_retryable_entries())
        self._input_area.setEnabled(not running and not self._is_shutting_down)
        self._default_platform.setEnabled(not running and not self._is_shutting_down)
        self._btn_clear.setEnabled(not running and not self._is_shutting_down)
        self._update_session_buttons()
