"""Phase 7 packaging readiness tests: AppData isolation, icon resource, About UX, zero source writes."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from config import (
    APP_NAME,
    APP_VERSION,
    BASE_DIR,
    get_app_data_dir,
    get_default_db_path,
    get_exports_dir,
    get_logs_dir,
    get_resource_path,
)
from gui.main_window import MainWindow


def test_all_writable_runtime_dirs_outside_source_tree() -> None:
    app_data = get_app_data_dir()
    db_path = get_default_db_path()
    logs_dir = get_logs_dir()
    exports_dir = get_exports_dir()

    for path in (app_data, db_path, logs_dir, exports_dir):
        assert BASE_DIR not in path.parents
        assert path != BASE_DIR


def test_app_icon_and_about_ux(tmp_path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    icon_file = get_resource_path("resources/app_icon.ico")
    assert icon_file.exists()
    icon = QIcon(str(icon_file))
    assert not icon.isNull()

    win = MainWindow(db_path=tmp_path / "p7.db")
    assert APP_NAME in win.windowTitle()
    assert APP_VERSION in win.windowTitle()
    assert not win.windowIcon().isNull()
    assert win._btn_about.text() == "About"
    win.close()
