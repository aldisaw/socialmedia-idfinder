"""Single Search page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.models import InputValidationStatus, Platform, SUPPORTED_PLATFORMS, SearchResult
from core.normalizer import normalize_input
from gui.theme import CONFIDENCE_COLORS, PLATFORM_META, STATUS_COLORS, get_platform_icon


class SingleSearchPage(QWidget):
    """Single profile username or URL lookup interface."""

    search_requested = Signal(str, object)  # raw_input, Platform | None
    stop_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._last_result: SearchResult | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        title = QLabel("Single Search")
        title.setObjectName("PageTitle")
        desc = QLabel("Enter a profile URL (auto-detected) or username with manual platform selection.")
        desc.setObjectName("PageDesc")
        root.addWidget(title)
        root.addWidget(desc)

        # Input Card
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(10)

        self._platform_combo = QComboBox()
        self._platform_combo.setMinimumWidth(175)
        self._platform_combo.addItem("Auto-detect from URL", None)
        for plat in SUPPORTED_PLATFORMS:
            self._platform_combo.addItem(get_platform_icon(plat.value, 16), plat.display_name, plat)
        self._platform_combo.currentIndexChanged.connect(lambda _: self._on_input_changed(self._input_edit.text()))

        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("e.g. https://www.youtube.com/@Google or @username")
        self._input_edit.textChanged.connect(self._on_input_changed)
        self._input_edit.returnPressed.connect(self._emit_search)

        self._btn_search = QPushButton("Search")
        self._btn_search.setMinimumWidth(100)
        self._btn_search.clicked.connect(self._emit_search)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setObjectName("DangerButton")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_requested.emit)

        row.addWidget(self._platform_combo)
        row.addWidget(self._input_edit, 1)
        row.addWidget(self._btn_search)
        row.addWidget(self._btn_stop)
        card_layout.addLayout(row)

        self._detect_hint = QLabel("Platform detection: waiting for input")
        self._detect_hint.setObjectName("PageDesc")
        card_layout.addWidget(self._detect_hint)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setRange(0, 0)
        card_layout.addWidget(self._progress)

        self._strategy_label = QLabel("")
        self._strategy_label.setObjectName("PageDesc")
        card_layout.addWidget(self._strategy_label)

        root.addWidget(card)

        # Latest Result Card
        res_card = QFrame()
        res_card.setObjectName("Card")
        res_layout = QVBoxLayout(res_card)
        res_layout.setContentsMargins(18, 16, 18, 16)
        res_layout.setSpacing(10)

        res_top = QHBoxLayout()
        res_title = QLabel("Latest Result")
        res_title.setObjectName("SectionLabel")
        self._btn_copy_id = QPushButton("Copy User ID")
        self._btn_copy_id.setObjectName("SmallButton")
        self._btn_copy_id.setEnabled(False)
        self._btn_copy_id.clicked.connect(self._copy_id)
        res_top.addWidget(res_title)
        res_top.addStretch()
        res_top.addWidget(self._btn_copy_id)
        res_layout.addLayout(res_top)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        self._fields: dict[str, QLabel] = {}
        for key, label in [
            ("platform", "Platform:"),
            ("username", "Username:"),
            ("user_id", "User ID:"),
            ("id_type", "ID Type:"),
            ("acquisition", "Acquisition:"),
            ("profile_url", "Profile URL:"),
            ("method", "Extraction Method:"),
            ("source", "Extraction Source:"),
            ("confidence", "Confidence:"),
            ("validation", "Validation:"),
            ("status", "Status:"),
            ("time", "Response Time:"),
            ("error", "Details / Error:"),
        ]:
            val = QLabel("—")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setWordWrap(True)
            val.setStyleSheet("background: transparent;")
            self._fields[key] = val
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #94A3B8; background: transparent;")
            form.addRow(lbl, val)

        res_layout.addLayout(form)
        root.addWidget(res_card)
        root.addStretch()

    def _on_input_changed(self, text: str) -> None:
        if not text.strip():
            self._detect_hint.setText("Platform detection: waiting for input")
            self._detect_hint.setStyleSheet("color: #94A3B8; background: transparent;")
            return
        selected = self._platform_combo.currentData()
        norm = normalize_input(text, selected_platform=selected)
        if norm.validation_status == InputValidationStatus.VALID:
            target = norm.profile_url or norm.username
            self._detect_hint.setText(f"[{norm.validation_status.value}] {norm.platform.display_name} -> {target}")
            self._detect_hint.setStyleSheet("color: #22C55E; background: transparent;")
        elif norm.validation_status == InputValidationStatus.AMBIGUOUS_INPUT:
            self._detect_hint.setText(f"[AMBIGUOUS_INPUT] {norm.error_message}")
            self._detect_hint.setStyleSheet("color: #F59E0B; background: transparent;")
        else:
            self._detect_hint.setText(f"[{norm.validation_status.value}] {norm.error_message}")
            self._detect_hint.setStyleSheet("color: #F87171; background: transparent;")

    def _emit_search(self) -> None:
        raw = self._input_edit.text().strip()
        if not raw:
            self._strategy_label.setText("Please enter a username or profile URL.")
            return
        selected = self._platform_combo.currentData()
        self.search_requested.emit(raw, selected)

    def set_running(self, running: bool) -> None:
        self._btn_search.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._input_edit.setEnabled(not running)
        self._platform_combo.setEnabled(not running)
        self._progress.setVisible(running)
        if running:
            self._strategy_label.setText("Starting extraction...")

    def set_progress_text(self, text: str) -> None:
        self._strategy_label.setText(text)

    def show_result(self, result: SearchResult) -> None:
        self._last_result = result
        _, _, plat_name = PLATFORM_META.get(result.platform, ("?", "#64748B", result.platform))
        self._fields["platform"].setText(plat_name)
        self._fields["username"].setText(result.username or "—")
        self._fields["user_id"].setText(result.user_id or "—")
        self._fields["id_type"].setText(result.id_type or "UNKNOWN")
        acq_text = result.acquisition_method or "HTTP"
        if result.response_status:
            acq_text += f" (HTTP {result.response_status})"
        self._fields["acquisition"].setText(acq_text)
        self._fields["profile_url"].setText(result.profile_url or "—")
        self._fields["method"].setText(result.extraction_method or "NONE")
        self._fields["source"].setText(result.extraction_source or "—")

        conf_color = CONFIDENCE_COLORS.get(result.confidence, "#94A3B8")
        self._fields["confidence"].setText(result.confidence)
        self._fields["confidence"].setStyleSheet(f"color: {conf_color}; font-weight: 600; background: transparent;")
        self._fields["validation"].setText(result.validation_status or "UNVERIFIED")

        status_color = STATUS_COLORS.get(result.status, "#94A3B8")
        self._fields["status"].setText(result.status)
        self._fields["status"].setStyleSheet(f"color: {status_color}; font-weight: 600; background: transparent;")

        self._fields["time"].setText(f"{result.response_time:.3f}s")
        self._fields["error"].setText(result.error_message or "—")
        self._btn_copy_id.setEnabled(bool(result.user_id))
        self._strategy_label.setText(f"Completed with status: {result.status}")

    def _copy_id(self) -> None:
        if self._last_result and self._last_result.user_id:
            QGuiApplication.clipboard().setText(self._last_result.user_id)
            self._strategy_label.setText(f"Copied User ID '{self._last_result.user_id}' to clipboard.")
