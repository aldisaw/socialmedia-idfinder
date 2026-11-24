"""SQLite database manager with WAL mode, foreign keys, schema versioning, migrations, and corruption safety."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from config import DEFAULT_SQLITE_BUSY_TIMEOUT_MS, get_default_db_path

logger = logging.getLogger("social_id_finder.database")

CURRENT_SCHEMA_VERSION = 2


class DatabaseCorruptionError(RuntimeError):
    """Raised when the SQLite database fails integrity checks. Original file is never overwritten."""

    def __init__(self, db_path: Path, detail: str) -> None:
        self.db_path = db_path
        self.detail = detail
        super().__init__(
            f"Database integrity error at '{db_path}': {detail}. "
            f"The original file was preserved. Recovery: close Social ID Finder, "
            f"back up or move '{db_path}', then restart to create a fresh database."
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Initial schema: batch_sessions, batch_items, search_history, app_settings + indexes."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS batch_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            completed_items INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            last_updated_at TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            original_input TEXT NOT NULL,
            canonical_key TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            profile_url TEXT NOT NULL DEFAULT '',
            direct_id TEXT,
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            response_time REAL NOT NULL DEFAULT 0.0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batch_sessions(id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_item_id INTEGER,
            input TEXT NOT NULL,
            platform TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            profile_url TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            id_type TEXT NOT NULL DEFAULT 'UNKNOWN',
            extraction_method TEXT NOT NULL DEFAULT 'NONE',
            extraction_source TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
            validation_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            status TEXT NOT NULL DEFAULT 'NOT_FOUND',
            response_time REAL NOT NULL DEFAULT 0.0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_item_id) REFERENCES batch_items(id) ON DELETE SET NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_batch_sessions_status ON batch_sessions(status);",
        "CREATE INDEX IF NOT EXISTS idx_batch_sessions_created ON batch_sessions(created_at DESC);",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_batch_pos ON batch_items(batch_id, position);",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_items(status);",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_canonical ON batch_items(batch_id, canonical_key);",
        "CREATE INDEX IF NOT EXISTS idx_history_created ON search_history(created_at DESC, id DESC);",
        "CREATE INDEX IF NOT EXISTS idx_history_platform ON search_history(platform);",
        "CREATE INDEX IF NOT EXISTS idx_history_status ON search_history(status);",
        "CREATE INDEX IF NOT EXISTS idx_history_confidence ON search_history(confidence);",
        "CREATE INDEX IF NOT EXISTS idx_history_user_id ON search_history(user_id);",
        "CREATE INDEX IF NOT EXISTS idx_history_username ON search_history(username);",
        "CREATE INDEX IF NOT EXISTS idx_history_batch_item ON search_history(batch_item_id);",
    )
    for stmt in statements:
        conn.execute(stmt)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add unified acquisition & diagnostic evidence columns to search_history."""
    existing_cols = {
        str(r["name"])
        for r in conn.execute("PRAGMA table_info(search_history);").fetchall()
    }
    if "acquisition_method" not in existing_cols:
        conn.execute(
            "ALTER TABLE search_history ADD COLUMN acquisition_method TEXT NOT NULL DEFAULT 'HTTP';"
        )
    if "response_status" not in existing_cols:
        conn.execute(
            "ALTER TABLE search_history ADD COLUMN response_status INTEGER NOT NULL DEFAULT 0;"
        )
    if "final_url" not in existing_cols:
        conn.execute(
            "ALTER TABLE search_history ADD COLUMN final_url TEXT NOT NULL DEFAULT '';"
        )


# Ordered list of (version, description, is_destructive, migration_callable)
MIGRATIONS: list[tuple[int, str, bool, Callable[[sqlite3.Connection], None]]] = [
    (1, "Initial schema with batch_sessions, batch_items, search_history, app_settings", False, _migrate_v1),
    (2, "Add acquisition_method, response_status, final_url to search_history", False, _migrate_v2),
]


class DatabaseManager:
    """Thread-safe SQLite manager opening short-lived per-operation connections."""

    def __init__(
        self,
        db_path: Path | None = None,
        busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_default_db_path()
        self.busy_timeout_ms = max(500, int(busy_timeout_ms))

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # Explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms};")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Short-lived read/write connection closed automatically on exit."""
        conn = self._open_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit IMMEDIATE transaction with deterministic commit/rollback."""
        conn = self._open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except BaseException:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def create_backup(self, reason: str = "migration") -> Path | None:
        """Create a WAL-consistent timestamped backup using SQLite's Online Backup API (M-03)."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.db_path.with_name(f"{self.db_path.name}.{reason}.{stamp}.bak")
        src_conn = self._open_connection()
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
            src_conn.close()
        logger.info("Created WAL-consistent SQLite backup at %s", backup_path)
        return backup_path

    def verify_integrity(self) -> None:
        """Verify existing non-empty database file is not corrupted before touching it."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return
        try:
            with self.connect() as conn:
                row = conn.execute("PRAGMA quick_check;").fetchone()
                status = str(row[0]) if row else "unknown"
                if status.lower() != "ok":
                    raise DatabaseCorruptionError(self.db_path, f"PRAGMA quick_check returned '{status}'")
        except DatabaseCorruptionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptionError(self.db_path, str(exc)) from exc

    def get_schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
            ).fetchone()
            if not row:
                return 0
            vrow = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations;").fetchone()
            return int(vrow["v"]) if vrow else 0

    def initialize(
        self,
        extra_migrations: list[tuple[int, str, bool, Callable[[sqlite3.Connection], None]]] | None = None,
    ) -> int:
        """Verify integrity, enable WAL mode, and apply any pending schema migrations safely."""
        self.verify_integrity()

        try:
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL,
                        description TEXT NOT NULL
                    );
                    """
                )
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptionError(self.db_path, str(exc)) from exc

        current = self.get_schema_version()
        all_migrations = list(MIGRATIONS)
        if extra_migrations:
            all_migrations.extend(extra_migrations)
        all_migrations.sort(key=lambda m: m[0])

        for version, description, is_destructive, fn in all_migrations:
            if version <= current:
                continue
            if is_destructive and current > 0:
                self.create_backup(reason=f"pre_v{version}")
            with self.transaction() as conn:
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                    (version, utc_now_iso(), description),
                )
            current = version
            logger.info("Applied SQLite schema migration v%d: %s", version, description)

        return current
