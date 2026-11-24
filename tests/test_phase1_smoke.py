"""Phase 1 smoke tests: models, sanitization, GUI construction, batch validation."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from config import MAX_BATCH_SIZE, AppSettings
from core.logger import sanitize_log_text
from core.models import Confidence, Platform, ResultStatus, SearchResult
from core.validator import validate_and_score
from core.normalizer import normalize_input
from gui.main_window import MainWindow


def _get_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_settings_clamp() -> None:
    s = AppSettings(
        request_timeout=1.0,
        item_timeout=1.0,
        batch_timeout=5.0,
        max_concurrency=99,
        retry_limit=5,
    ).clamp()
    assert s.request_timeout == 3.0
    assert s.item_timeout == 5.0
    assert s.batch_timeout == 10.0
    assert s.max_concurrency == 10
    assert s.retry_limit == 1


def test_log_sanitization_redacts_secrets() -> None:
    raw = "Header Cookie=sessionid=abc123 Authorization: Bearer secretToken123"
    cleaned = sanitize_log_text(raw)
    assert "abc123" not in cleaned
    assert "secretToken123" not in cleaned
    assert "[REDACTED]" in cleaned


def test_unverified_result_never_high_confidence() -> None:
    norm = normalize_input("https://www.youtube.com/@Google")
    res = SearchResult(
        platform=Platform.YOUTUBE.value,
        input=norm.raw_input,
        user_id="",
        confidence=Confidence.HIGH.value,
        status=ResultStatus.NOT_FOUND.value,
    )
    checked = validate_and_score(res, norm)
    assert checked.confidence == Confidence.NOT_VERIFIED.value


def test_mainwindow_instantiation_and_batch_dedup(tmp_path) -> None:
    _get_app()
    win = MainWindow(db_path=tmp_path / "smoke.db")
    assert win._stack.count() == 6

    # Verify batch deduplication and 10-entry enforcement
    lines = [f"https://www.youtube.com/@user{i}" for i in range(12)]
    lines.append("https://www.youtube.com/@user0")  # duplicate
    win._batch_page._input_area.setPlainText("\n".join(lines))
    unique, dups = win._batch_page.parse_entries()
    assert len(unique) == 12
    assert dups == 1
    assert not win._batch_page._btn_start.isEnabled()

    # Valid <= MAX_BATCH_SIZE enables Start
    win._batch_page._input_area.setPlainText("\n".join(lines[:MAX_BATCH_SIZE]))
    unique2, dups2 = win._batch_page.parse_entries()
    assert len(unique2) == MAX_BATCH_SIZE
    assert dups2 == 0
    assert win._batch_page._btn_start.isEnabled()

    win.close()
