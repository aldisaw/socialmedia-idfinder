"""SQLite repository for batch_sessions and batch_items with crash recovery and FOUND protection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.logger import sanitize_log_text
from core.models import (
    InputValidationStatus,
    NormalizedInput,
    Platform,
    ResultStatus,
    SearchResult,
)
from core.normalizer import normalize_input
from core.scheduler import RETRYABLE_STATUSES
from database.database import DatabaseManager, utc_now_iso
from database.history_repo import HistoryRepository, _insert_history_row

BATCH_STATUS_RUNNING = "RUNNING"
BATCH_STATUS_COMPLETED = "COMPLETED"
BATCH_STATUS_INTERRUPTED = "INTERRUPTED"
BATCH_STATUS_CANCELLED = "CANCELLED"
BATCH_STATUS_TIMEOUT = "TIMEOUT"

_TERMINAL_ITEM_STATUSES: frozenset[str] = frozenset(
    {
        ResultStatus.FOUND.value,
        ResultStatus.NOT_FOUND.value,
        ResultStatus.BLOCKED.value,
        ResultStatus.LOGIN_REQUIRED.value,
        ResultStatus.CAPTCHA_REQUIRED.value,
        ResultStatus.RATE_LIMITED.value,
        ResultStatus.ERROR.value,
        ResultStatus.TIMEOUT.value,
        ResultStatus.CANCELLED.value,
        ResultStatus.INVALID_INPUT.value,
    }
)


@dataclass
class BatchSessionRecord:
    id: int
    status: str
    total_items: int
    completed_items: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    last_updated_at: str


@dataclass
class BatchItemRecord:
    id: int
    batch_id: int
    position: int
    original_input: str
    canonical_key: str
    platform: str
    username: str
    profile_url: str
    direct_id: str | None
    status: str
    retry_count: int
    response_time: float
    error_message: str
    created_at: str
    updated_at: str


def _row_to_session(row: sqlite3.Row) -> BatchSessionRecord:
    return BatchSessionRecord(
        id=int(row["id"]),
        status=str(row["status"]),
        total_items=int(row["total_items"]),
        completed_items=int(row["completed_items"]),
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
        last_updated_at=str(row["last_updated_at"]),
    )


def _row_to_item(row: sqlite3.Row) -> BatchItemRecord:
    return BatchItemRecord(
        id=int(row["id"]),
        batch_id=int(row["batch_id"]),
        position=int(row["position"]),
        original_input=str(row["original_input"]),
        canonical_key=str(row["canonical_key"]),
        platform=str(row["platform"]),
        username=str(row["username"]),
        profile_url=str(row["profile_url"]),
        direct_id=str(row["direct_id"]) if row["direct_id"] is not None else None,
        status=str(row["status"]),
        retry_count=int(row["retry_count"]),
        response_time=float(row["response_time"]),
        error_message=str(row["error_message"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class BatchRepository:
    """Persists batch sessions and ordered items with crash recovery and FOUND protection."""

    def __init__(self, db: DatabaseManager | Path | None = None) -> None:
        if isinstance(db, DatabaseManager):
            self.db = db
        else:
            self.db = DatabaseManager(db_path=db)
            self.db.initialize()
        self._history_repo = HistoryRepository(self.db)

    def create_batch(
        self,
        items: list[NormalizedInput],
    ) -> tuple[BatchSessionRecord, list[BatchItemRecord]]:
        """Persist a new batch session and all ordered batch items atomically."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO batch_sessions (
                    status, total_items, completed_items,
                    created_at, started_at, finished_at, last_updated_at
                ) VALUES (?, ?, 0, ?, ?, NULL, ?);
                """,
                (BATCH_STATUS_RUNNING, len(items), now, now, now),
            )
            batch_id = int(cur.lastrowid)

            item_records: list[BatchItemRecord] = []
            for pos, item in enumerate(items):
                init_status = ResultStatus.PENDING.value if item.is_valid else ResultStatus.INVALID_INPUT.value
                init_err = "" if item.is_valid else f"[{item.validation_status.value}] {item.error_message}"
                icur = conn.execute(
                    """
                    INSERT INTO batch_items (
                        batch_id, position, original_input, canonical_key,
                        platform, username, profile_url, direct_id,
                        status, retry_count, response_time, error_message,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0.0, ?, ?, ?);
                    """,
                    (
                        batch_id,
                        pos,
                        sanitize_log_text(item.raw_input),
                        str(item.canonical_key or ""),
                        item.platform.value,
                        sanitize_log_text(item.username or ""),
                        sanitize_log_text(item.profile_url or ""),
                        item.direct_id,
                        init_status,
                        sanitize_log_text(init_err),
                        now,
                        now,
                    ),
                )
                item_records.append(
                    BatchItemRecord(
                        id=int(icur.lastrowid),
                        batch_id=batch_id,
                        position=pos,
                        original_input=item.raw_input,
                        canonical_key=item.canonical_key,
                        platform=item.platform.value,
                        username=item.username,
                        profile_url=item.profile_url,
                        direct_id=item.direct_id,
                        status=init_status,
                        retry_count=0,
                        response_time=0.0,
                        error_message=init_err,
                        created_at=now,
                        updated_at=now,
                    )
                )

            session_row = conn.execute(
                "SELECT * FROM batch_sessions WHERE id = ?;", (batch_id,)
            ).fetchone()
            assert session_row is not None
            return _row_to_session(session_row), item_records

    def mark_batch_running(self, batch_id: int) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE batch_sessions
                SET status = ?, finished_at = NULL, last_updated_at = ?
                WHERE id = ?;
                """,
                (BATCH_STATUS_RUNNING, now, batch_id),
            )

    def mark_item_running(self, batch_id: int, item_id: int) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM batch_items WHERE id = ?;", (item_id,)).fetchone()
            if row and str(row["status"]) == ResultStatus.FOUND.value:
                return
            conn.execute(
                "UPDATE batch_items SET status = ?, updated_at = ? WHERE id = ?;",
                (ResultStatus.RUNNING.value, now, item_id),
            )
            conn.execute(
                "UPDATE batch_sessions SET last_updated_at = ? WHERE id = ?;",
                (now, batch_id),
            )

    def record_item_result(
        self,
        batch_id: int,
        item_id: int,
        result: SearchResult,
        *,
        increment_retry: bool = False,
    ) -> bool:
        """Persist item result and linked search_history row atomically.

        Never overwrites a previously FOUND batch item with a failed retry.
        Returns True if updated, False if protected as already FOUND.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cur_row = conn.execute(
                "SELECT status, retry_count FROM batch_items WHERE id = ? AND batch_id = ?;",
                (item_id, batch_id),
            ).fetchone()
            if cur_row is None:
                return False
            if str(cur_row["status"]) == ResultStatus.FOUND.value:
                # Strict protection: never overwrite a previously successful FOUND item
                return False

            new_retry = int(cur_row["retry_count"]) + (1 if increment_retry else 0)
            conn.execute(
                """
                UPDATE batch_items
                SET status = ?,
                    retry_count = ?,
                    response_time = ?,
                    error_message = ?,
                    username = CASE WHEN ? != '' THEN ? ELSE username END,
                    profile_url = CASE WHEN ? != '' THEN ? ELSE profile_url END,
                    updated_at = ?
                WHERE id = ?;
                """,
                (
                    result.status,
                    new_retry,
                    float(result.response_time or 0.0),
                    sanitize_log_text(result.error_message or ""),
                    sanitize_log_text(result.username or ""),
                    sanitize_log_text(result.username or ""),
                    sanitize_log_text(result.profile_url or ""),
                    sanitize_log_text(result.profile_url or ""),
                    now,
                    item_id,
                ),
            )

            _insert_history_row(conn, result, batch_item_id=item_id)

            placeholders = ",".join("?" for _ in _TERMINAL_ITEM_STATUSES)
            comp_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM batch_items WHERE batch_id = ? AND status IN ({placeholders});",
                (batch_id, *tuple(_TERMINAL_ITEM_STATUSES)),
            ).fetchone()
            completed = int(comp_row["c"]) if comp_row else 0
            conn.execute(
                "UPDATE batch_sessions SET completed_items = ?, last_updated_at = ? WHERE id = ?;",
                (completed, now, batch_id),
            )
            return True

    def finalize_batch(self, batch_id: int, status: str) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            placeholders = ",".join("?" for _ in _TERMINAL_ITEM_STATUSES)
            comp_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM batch_items WHERE batch_id = ? AND status IN ({placeholders});",
                (batch_id, *tuple(_TERMINAL_ITEM_STATUSES)),
            ).fetchone()
            completed = int(comp_row["c"]) if comp_row else 0
            conn.execute(
                """
                UPDATE batch_sessions
                SET status = ?, completed_items = ?, finished_at = ?, last_updated_at = ?
                WHERE id = ?;
                """,
                (status, completed, now, now, batch_id),
            )

    def mark_running_batches_interrupted(self) -> list[int]:
        """On startup, detect batches left in RUNNING state and mark them INTERRUPTED.

        Resets in-flight RUNNING items back to PENDING so user can explicitly resume them.
        Never starts any network work automatically.
        """
        now = utc_now_iso()
        interrupted_ids: list[int] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM batch_sessions WHERE status = ?;",
                (BATCH_STATUS_RUNNING,),
            ).fetchall()
            interrupted_ids = [int(r["id"]) for r in rows]
            if not interrupted_ids:
                return []

            placeholders = ",".join("?" for _ in interrupted_ids)
            conn.execute(
                f"""
                UPDATE batch_sessions
                SET status = ?, last_updated_at = ?
                WHERE id IN ({placeholders});
                """,
                (BATCH_STATUS_INTERRUPTED, now, *interrupted_ids),
            )
            conn.execute(
                f"""
                UPDATE batch_items
                SET status = ?, error_message = ?, updated_at = ?
                WHERE batch_id IN ({placeholders}) AND status = ?;
                """,
                (
                    ResultStatus.PENDING.value,
                    "Interrupted before completion; ready to resume.",
                    now,
                    *interrupted_ids,
                    ResultStatus.RUNNING.value,
                ),
            )
        return interrupted_ids

    def list_sessions(self, limit: int = 25) -> list[BatchSessionRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_sessions ORDER BY id DESC LIMIT ?;",
                (int(limit),),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def list_interrupted_sessions(self) -> list[BatchSessionRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_sessions WHERE status = ? ORDER BY id DESC;",
                (BATCH_STATUS_INTERRUPTED,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_session(self, batch_id: int) -> BatchSessionRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM batch_sessions WHERE id = ?;", (batch_id,)).fetchone()
        return _row_to_session(row) if row else None

    def get_batch_items(self, batch_id: int) -> list[BatchItemRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position ASC;",
                (batch_id,),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def load_batch_state(
        self,
        batch_id: int,
    ) -> tuple[BatchSessionRecord, list[NormalizedInput], list[BatchItemRecord], list[SearchResult | None]] | None:
        """Restore complete batch session, normalized inputs, item rows, and persisted SearchResults in order."""
        session = self.get_session(batch_id)
        if session is None:
            return None
        item_records = self.get_batch_items(batch_id)
        history_map = self._history_repo.get_latest_for_batch_items([it.id for it in item_records])

        normalized_items: list[NormalizedInput] = []
        results: list[SearchResult | None] = []

        for rec in item_records:
            try:
                plat = Platform(rec.platform)
            except ValueError:
                plat = Platform.UNKNOWN
            norm = normalize_input(rec.original_input, selected_platform=plat if plat != Platform.UNKNOWN else None)
            if not norm.is_valid and rec.status == ResultStatus.FOUND.value:
                # Reconstruct valid normalized item if previously normalized
                norm = NormalizedInput(
                    raw_input=rec.original_input,
                    platform=plat,
                    username=rec.username,
                    profile_url=rec.profile_url,
                    direct_id=rec.direct_id,
                    validation_status=InputValidationStatus.VALID,
                    is_valid=True,
                    canonical_key=rec.canonical_key,
                )
            normalized_items.append(norm)

            hist = history_map.get(rec.id)
            if hist is not None:
                results.append(hist)
            elif rec.status in (ResultStatus.PENDING.value, ResultStatus.RUNNING.value):
                results.append(None)
            else:
                results.append(
                    SearchResult(
                        platform=rec.platform,
                        input=rec.original_input,
                        username=rec.username,
                        profile_url=rec.profile_url,
                        status=rec.status,
                        response_time=rec.response_time,
                        error_message=rec.error_message,
                        created_at=rec.updated_at,
                    )
                )

        return session, normalized_items, item_records, results

    @staticmethod
    def select_resumable_indices(
        items: list[NormalizedInput],
        item_records: list[BatchItemRecord],
        results: list[SearchResult | None],
    ) -> list[int]:
        """Return indices that are PENDING, interrupted, or retryable — strictly excluding FOUND."""
        indices: list[int] = []
        for idx, item in enumerate(items):
            if not item.is_valid:
                continue
            rec_status = item_records[idx].status if idx < len(item_records) else ResultStatus.PENDING.value
            res = results[idx] if idx < len(results) else None
            if rec_status == ResultStatus.FOUND.value or (res is not None and res.status == ResultStatus.FOUND.value):
                continue
            if rec_status in (ResultStatus.PENDING.value, ResultStatus.RUNNING.value) or rec_status in RETRYABLE_STATUSES:
                indices.append(idx)
        return indices

    def delete_session(self, batch_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM batch_sessions WHERE id = ?;", (batch_id,))
