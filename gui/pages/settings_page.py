"""Application Settings page backed by SQLite SettingsRepository."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import AppSettings, MAX_CONCURRENCY, MAX_RETRY_LIMIT, MIN_CONCURRENCY
from core.browser_client import check_browser_capability


class SettingsPage(QWidget):
    """Configures persisted timeouts, concurrency, cooldown, User-Agent, and debug mode."""

    settings_saved = Signal(object)  # AppSettings

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self.load_settings(settings)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        desc = QLabel(
            "Persisted non-secret preferences (timeouts, concurrency, rate-limit cooldown, User-Agent, diagnostics)."
        )
        desc.setObjectName("PageDesc")
        root.addWidget(title)
        root.addWidget(desc)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(12)

        self._req_timeout = QDoubleSpinBox()
        self._req_timeout.setRange(3.0, 60.0)
        self._req_timeout.setSuffix(" s")
        self._req_timeout.setDecimals(1)
        self._req_timeout.setMaximumWidth(140)

        self._item_timeout = QDoubleSpinBox()
        self._item_timeout.setRange(5.0, 120.0)
        self._item_timeout.setSuffix(" s")
        self._item_timeout.setDecimals(1)
        self._item_timeout.setMaximumWidth(140)

        self._batch_timeout = QDoubleSpinBox()
        self._batch_timeout.setRange(10.0, 300.0)
        self._batch_timeout.setSuffix(" s")
        self._batch_timeout.setDecimals(1)
        self._batch_timeout.setMaximumWidth(140)

        self._concurrency = QSpinBox()
        self._concurrency.setRange(MIN_CONCURRENCY, MAX_CONCURRENCY)
        self._concurrency.setMaximumWidth(140)

        self._retry_limit = QSpinBox()
        self._retry_limit.setRange(0, MAX_RETRY_LIMIT)
        self._retry_limit.setMaximumWidth(140)

        self._cooldown = QDoubleSpinBox()
        self._cooldown.setRange(1.0, 300.0)
        self._cooldown.setSuffix(" s")
        self._cooldown.setDecimals(1)
        self._cooldown.setMaximumWidth(140)

        self._user_agent = QLineEdit()
        self._user_agent.setPlaceholderText("HTTP User-Agent string")

        cap = check_browser_capability()
        self._browser_fallback = QCheckBox(
            f"Enable optional Playwright Chromium fallback for JS-rendered profiles [{cap.status}]"
        )
        self._browser_cap_note = QLabel(cap.reason)
        self._browser_cap_note.setObjectName("PageDesc")

        self._debug_mode = QCheckBox("Enable verbose strategy diagnostics (sensitive values always redacted)")

        form.addRow(self._lbl("Per-request timeout:"), self._req_timeout)
        form.addRow(self._lbl("Per-item timeout:"), self._item_timeout)
        form.addRow(self._lbl("Batch timeout:"), self._batch_timeout)
        form.addRow(self._lbl(f"Max concurrent requests ({MIN_CONCURRENCY}-{MAX_CONCURRENCY}):"), self._concurrency)
        form.addRow(self._lbl("Transient retry limit (max 1):"), self._retry_limit)
        form.addRow(self._lbl("Rate-limit cooldown:"), self._cooldown)
        form.addRow(self._lbl("User-Agent:"), self._user_agent)
        form.addRow(self._lbl("Browser fallback:"), self._browser_fallback)
        form.addRow(self._lbl("Browser runtime:"), self._browser_cap_note)
        form.addRow(self._lbl("Debug mode:"), self._debug_mode)
        card_layout.addLayout(form)

        actions = QHBoxLayout()
        btn_save = QPushButton("Save Settings")
        btn_save.clicked.connect(self._on_save)
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.setObjectName("SecondaryButton")
        btn_reset.clicked.connect(self._on_reset)
        self._feedback = QLabel("")
        self._feedback.setObjectName("PageDesc")

        actions.addWidget(btn_save)
        actions.addWidget(btn_reset)
        actions.addSpacing(12)
        actions.addWidget(self._feedback)
        actions.addStretch()
        card_layout.addLayout(actions)

        root.addWidget(card)
        root.addStretch()

    def _lbl(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #CBD5E1; background: transparent;")
        return lbl

    def load_settings(self, settings: AppSettings) -> None:
        self._req_timeout.setValue(settings.request_timeout)
        self._item_timeout.setValue(settings.item_timeout)
        self._batch_timeout.setValue(settings.batch_timeout)
        self._concurrency.setValue(settings.max_concurrency)
        self._retry_limit.setValue(settings.retry_limit)
        self._cooldown.setValue(settings.rate_limit_cooldown)
        self._user_agent.setText(settings.user_agent)
        self._browser_fallback.setChecked(settings.enable_browser_fallback)
        self._debug_mode.setChecked(settings.debug_mode)

    def current_settings(self) -> AppSettings:
        return AppSettings(
            request_timeout=self._req_timeout.value(),
            item_timeout=self._item_timeout.value(),
            batch_timeout=self._batch_timeout.value(),
            max_concurrency=self._concurrency.value(),
            retry_limit=self._retry_limit.value(),
            rate_limit_cooldown=self._cooldown.value(),
            user_agent=self._user_agent.text().strip(),
            enable_browser_fallback=self._browser_fallback.isChecked(),
            debug_mode=self._debug_mode.isChecked(),
        ).clamp()

    def _on_save(self) -> None:
        settings = self.current_settings()
        self.load_settings(settings)
        self.settings_saved.emit(settings)
        self._feedback.setText("Settings saved to SQLite.")

    def _on_reset(self) -> None:
        defaults = AppSettings()
        self.load_settings(defaults)
        self.settings_saved.emit(defaults)
        self._feedback.setText("Restored default settings.")
