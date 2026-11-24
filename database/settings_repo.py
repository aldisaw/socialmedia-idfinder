"""Persistent SQLite settings repository (strictly non-secret preferences)."""

from __future__ import annotations

import logging
from pathlib import Path

from config import AppSettings
from database.database import DatabaseManager, utc_now_iso

logger = logging.getLogger("social_id_finder.settings_repo")

ALLOWED_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "request_timeout",
        "item_timeout",
        "batch_timeout",
        "max_concurrency",
        "retry_limit",
        "rate_limit_cooldown",
        "user_agent",
        "enable_browser_fallback",
        "debug_mode",
    }
)


class SettingsRepository:
    """Stores and validates non-secret AppSettings in SQLite."""

    def __init__(self, db: DatabaseManager | Path | None = None) -> None:
        if isinstance(db, DatabaseManager):
            self.db = db
        else:
            self.db = DatabaseManager(db_path=db)
            self.db.initialize()

    def load(self) -> AppSettings:
        """Load persisted settings, falling back to config.py defaults for missing or corrupted values."""
        defaults = AppSettings()
        raw: dict[str, str] = {}
        try:
            with self.db.connect() as conn:
                rows = conn.execute("SELECT key, value FROM app_settings;").fetchall()
                for row in rows:
                    key = str(row["key"])
                    if key in ALLOWED_SETTING_KEYS:
                        raw[key] = str(row["value"])
        except Exception as exc:
            logger.warning("Failed to load settings from SQLite; using defaults: %s", exc)
            return defaults

        def _float(key: str, default: float) -> float:
            if key not in raw:
                return default
            try:
                return float(raw[key])
            except (TypeError, ValueError):
                logger.warning("Corrupted float setting '%s'; using default %.1f", key, default)
                return default

        def _int(key: str, default: int) -> int:
            if key not in raw:
                return default
            try:
                return int(float(raw[key]))
            except (TypeError, ValueError):
                logger.warning("Corrupted int setting '%s'; using default %d", key, default)
                return default

        def _bool(key: str, default: bool) -> bool:
            if key not in raw:
                return default
            val = raw[key].strip().lower()
            if val in ("1", "true", "yes", "on"):
                return True
            if val in ("0", "false", "no", "off"):
                return False
            logger.warning("Corrupted bool setting '%s'; using default %s", key, default)
            return default

        def _str(key: str, default: str) -> str:
            val = raw.get(key, "").strip()
            return val if val else default

        return AppSettings(
            request_timeout=_float("request_timeout", defaults.request_timeout),
            item_timeout=_float("item_timeout", defaults.item_timeout),
            batch_timeout=_float("batch_timeout", defaults.batch_timeout),
            max_concurrency=_int("max_concurrency", defaults.max_concurrency),
            retry_limit=_int("retry_limit", defaults.retry_limit),
            rate_limit_cooldown=_float("rate_limit_cooldown", defaults.rate_limit_cooldown),
            user_agent=_str("user_agent", defaults.user_agent),
            enable_browser_fallback=_bool("enable_browser_fallback", defaults.enable_browser_fallback),
            debug_mode=_bool("debug_mode", defaults.debug_mode),
        ).clamp()

    def save(self, settings: AppSettings) -> AppSettings:
        """Validate/clamp and persist AppSettings transactionally."""
        clean = AppSettings(
            request_timeout=settings.request_timeout,
            item_timeout=settings.item_timeout,
            batch_timeout=settings.batch_timeout,
            max_concurrency=settings.max_concurrency,
            retry_limit=settings.retry_limit,
            rate_limit_cooldown=settings.rate_limit_cooldown,
            user_agent=settings.user_agent,
            enable_browser_fallback=settings.enable_browser_fallback,
            debug_mode=settings.debug_mode,
        ).clamp()

        now = utc_now_iso()
        pairs = [
            ("request_timeout", str(clean.request_timeout), now),
            ("item_timeout", str(clean.item_timeout), now),
            ("batch_timeout", str(clean.batch_timeout), now),
            ("max_concurrency", str(clean.max_concurrency), now),
            ("retry_limit", str(clean.retry_limit), now),
            ("rate_limit_cooldown", str(clean.rate_limit_cooldown), now),
            ("user_agent", clean.user_agent, now),
            ("enable_browser_fallback", "1" if clean.enable_browser_fallback else "0", now),
            ("debug_mode", "1" if clean.debug_mode else "0", now),
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at;
                """,
                pairs,
            )
        return clean

    def set_raw(self, key: str, value: str) -> None:
        """Persist a single allowlisted non-secret key (rejects credential-like keys)."""
        if key not in ALLOWED_SETTING_KEYS:
            raise ValueError(f"Setting key '{key}' is not allowed for persistence.")
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at;
                """,
                (key, str(value), utc_now_iso()),
            )

    def reset(self) -> AppSettings:
        """Restore and persist config.py defaults."""
        return self.save(AppSettings())
