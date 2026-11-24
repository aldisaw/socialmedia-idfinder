"""Entry point for Social ID Finder native Windows desktop application."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from config import APP_NAME, APP_VERSION, ensure_directories, get_resource_path
from core.logger import setup_logging, shutdown_logging
from gui.main_window import MainWindow
from gui.theme import DARK_STYLESHEET


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    ensure_directories()
    setup_logging(debug=False)

    icon_path = get_resource_path("resources/app_icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    app.setStyleSheet(DARK_STYLESHEET)

    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
