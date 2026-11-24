"""Application configuration, defaults, and AppData paths for Social ID Finder."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_RE_UA_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
MAX_USER_AGENT_LENGTH = 512

APP_NAME = "Social ID Finder"
APP_VERSION = "1.1.0"
DB_FILENAME = "social_id_finder.db"

BASE_DIR = Path(__file__).resolve().parent

MAX_BATCH_SIZE = 10
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_ITEM_TIMEOUT = 30.0
DEFAULT_BATCH_TIMEOUT = 60.0
MIN_CONCURRENCY = 1
DEFAULT_MAX_CONCURRENCY = 3
MAX_CONCURRENCY = 10
DEFAULT_RETRY_LIMIT = 1
MAX_RETRY_LIMIT = 1
DEFAULT_RATE_LIMIT_COOLDOWN = 15.0
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def get_resource_path(relative_path: str) -> Path:
    """Resolve bundled read-only resource path in both dev and PyInstaller frozen builds."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / relative_path
    return BASE_DIR / relative_path


def get_app_data_dir() -> Path:
    """Resolve OS user application data directory via QStandardPaths.AppDataLocation.

    All writable runtime files (DB, WAL, logs, backups, default exports) live here,
    never inside the executable installation or project source directory.
    """
    resolved: Path | None = None
    try:
        from PySide6.QtCore import QStandardPaths

        loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if loc:
            candidate = Path(loc)
            if candidate.name.lower() != APP_NAME.lower():
                candidate = candidate / APP_NAME
            resolved = candidate
    except Exception:
        resolved = None

    if resolved is None:
        appdata = os.environ.get("APPDATA")
        if appdata:
            resolved = Path(appdata) / APP_NAME
        else:
            resolved = Path.home() / ".local" / "share" / APP_NAME

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def get_default_db_path() -> Path:
    """Return production SQLite database path inside AppDataLocation."""
    return get_app_data_dir() / DB_FILENAME


def get_logs_dir() -> Path:
    """Return writable logs directory inside AppDataLocation."""
    d = get_app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_exports_dir() -> Path:
    """Return default writable exports directory inside AppDataLocation."""
    d = get_app_data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Backward-compatible constants (all point under AppDataLocation, never source/install dir)
DATA_DIR = get_app_data_dir()
DB_PATH = DATA_DIR / DB_FILENAME
LOGS_DIR = get_logs_dir()
EXPORTS_DIR = get_exports_dir()


@dataclass
class AppSettings:
    """User-configurable non-secret runtime settings."""

    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    item_timeout: float = DEFAULT_ITEM_TIMEOUT
    batch_timeout: float = DEFAULT_BATCH_TIMEOUT
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    retry_limit: int = DEFAULT_RETRY_LIMIT
    rate_limit_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN
    user_agent: str = USER_AGENT
    enable_browser_fallback: bool = True
    debug_mode: bool = False

    def clamp(self) -> "AppSettings":
        """Enforce safe bounds on all settings before persistence or runtime use."""
        self.request_timeout = max(3.0, min(float(self.request_timeout), 60.0))
        self.item_timeout = max(5.0, min(float(self.item_timeout), 120.0))
        self.batch_timeout = max(10.0, min(float(self.batch_timeout), 300.0))
        self.max_concurrency = max(MIN_CONCURRENCY, min(int(self.max_concurrency), MAX_CONCURRENCY))
        self.retry_limit = max(0, min(int(self.retry_limit), MAX_RETRY_LIMIT))
        self.rate_limit_cooldown = max(1.0, min(float(self.rate_limit_cooldown), 300.0))
        ua = _RE_UA_CONTROL.sub(" ", str(self.user_agent or "")).strip()[:MAX_USER_AGENT_LENGTH].strip()
        self.user_agent = ua if ua else USER_AGENT
        self.enable_browser_fallback = bool(self.enable_browser_fallback)
        self.debug_mode = bool(self.debug_mode)
        return self


def ensure_directories() -> None:
    """Create writable runtime directories inside AppDataLocation."""
    get_app_data_dir().mkdir(parents=True, exist_ok=True)
    get_logs_dir().mkdir(parents=True, exist_ok=True)
    get_exports_dir().mkdir(parents=True, exist_ok=True)
