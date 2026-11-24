"""Local SQLite persistence package."""

from database.batch_repo import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_INTERRUPTED,
    BATCH_STATUS_RUNNING,
    BATCH_STATUS_TIMEOUT,
    BatchItemRecord,
    BatchRepository,
    BatchSessionRecord,
)
from database.database import CURRENT_SCHEMA_VERSION, DatabaseCorruptionError, DatabaseManager
from database.history_repo import HistoryRecord, HistoryRepository
from database.settings_repo import ALLOWED_SETTING_KEYS, SettingsRepository

__all__ = [
    "ALLOWED_SETTING_KEYS",
    "BATCH_STATUS_CANCELLED",
    "BATCH_STATUS_COMPLETED",
    "BATCH_STATUS_INTERRUPTED",
    "BATCH_STATUS_RUNNING",
    "BATCH_STATUS_TIMEOUT",
    "BatchItemRecord",
    "BatchRepository",
    "BatchSessionRecord",
    "CURRENT_SCHEMA_VERSION",
    "DatabaseCorruptionError",
    "DatabaseManager",
    "HistoryRecord",
    "HistoryRepository",
    "SettingsRepository",
]
