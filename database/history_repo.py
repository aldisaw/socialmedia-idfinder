"""SQLite search history repository with filtering, sorting, selected deletion, and sanitization."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from config import AppSettings
from core.logger import sanitize_log_text
from core.models import SearchResult
from database.database import DatabaseManager, utc_now_iso
from database.settings_repo import SettingsRepository


@dataclass
class HistoryRecord:
    """Persisted search_history row wrapping a SearchResult."""

    id: int
    batch_item_id: int | None
    result: SearchResult


def _row_to_history_record(row: sqlite3.Row) -> HistoryRecord:
    keys = row.keys()
    res = SearchResult(
        platform=str(row["platform"]),
        input=str(row["input"]),
        username=str(row["username"]),
        profile_url=str(row["profile_url"]),
        user_id=str(row["user_id"]),
        id_type=str(row["id_type"]),
        acquisition_method=str(row["acquisition_method"]) if "acquisition_method" in keys else "HTTP",
        extraction_method=str(row["extraction_method"]),
        extraction_source=str(row["extraction_source"]),
        response_status=int(row["response_status"]) if "response_status" in keys else 0,
        final_url=str(row["final_url"]) if "final_url" in keys else "",
        confidence=str(row["confidence"]),
        validation_status=str(row["validation_status"]),
        status=str(row["status"]),
        response_time=float(row["response_time"]),
        error_message=str(row["error_message"]),
        created_at=str(row["created_at"]),
    )
    return HistoryRecord(
        id=int(row["id"]),
        batch_item_id=int(row["batch_item_id"]) if row["batch_item_id"] is not None else None,
        result=res,
    )


def _insert_history_row(
    conn: sqlite3.Connection,
    result: SearchResult,
    batch_item_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO search_history (
            batch_item_id, input, platform, username, profile_url,
            user_id, id_type, acquisition_method, extraction_method,
            extraction_source, response_status, final_url,
            confidence, validation_status, status, response_time,
            error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            batch_item_id,
            sanitize_log_text(result.input or ""),
            str(result.platform or ""),
            sanitize_log_text(result.username or ""),
            sanitize_log_text(result.profile_url or ""),
            str(result.user_id or ""),
            str(result.id_type or "UNKNOWN"),
            str(result.acquisition_method or "HTTP"),
            str(result.extraction_method or "NONE"),
            sanitize_log_text(result.extraction_source or ""),
            int(result.response_status or 0),
            sanitize_log_text(result.final_url or ""),
            str(result.confidence or "NOT_VERIFIED"),
            str(result.validation_status or "UNVERIFIED"),
            str(result.status or "NOT_FOUND"),
            float(result.response_time or 0.0),
            sanitize_log_text(result.error_message or ""),
            str(result.created_at or utc_now_iso()),
        ),
    )
    return int(cur.lastrowid)


class HistoryRepository:
    """SQLite-backed search history repository (never merges distinct accounts)."""

    def __init__(self, db: DatabaseManager | Path | None = None) -> None:
        if isinstance(db, DatabaseManager):
            self.db = db
        else:
            self.db = DatabaseManager(db_path=db)
            self.db.initialize()
        self.db_path = self.db.db_path
        self._settings_repo = SettingsRepository(self.db)

    def save_result(self, result: SearchResult, batch_item_id: int | None = None) -> int:
        with self.db.transaction() as conn:
            return _insert_history_row(conn, result, batch_item_id=batch_item_id)

    def save_results(self, results: list[SearchResult]) -> list[int]:
        ids: list[int] = []
        with self.db.transaction() as conn:
            for res in results:
                ids.append(_insert_history_row(conn, res))
        return ids

    def list_records(
        self,
        *,
        platform: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        query: str = "",
        sort_desc: bool = True,
        limit: int | None = None,
    ) -> list[HistoryRecord]:
        clauses: list[str] = []
        params: list[object] = []

        if platform and platform.lower() != "all":
            clauses.append("platform = ?")
            params.append(platform)
        if status and status.lower() != "all":
            clauses.append("status = ?")
            params.append(status)
        if confidence and confidence.lower() != "all":
            clauses.append("confidence = ?")
            params.append(confidence)

        q = (query or "").strip().lower()
        if q:
            like = f"%{q}%"
            clauses.append(
                "(LOWER(username) LIKE ? OR LOWER(user_id) LIKE ? OR LOWER(input) LIKE ? OR LOWER(profile_url) LIKE ?)"
            )
            params.extend([like, like, like, like])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = "ORDER BY created_at DESC, id DESC" if sort_desc else "ORDER BY created_at ASC, id ASC"
        limit_sql = ""
        if limit is not None and limit > 0:
            limit_sql = "LIMIT ?"
            params.append(int(limit))

        sql = f"SELECT * FROM search_history {where_sql} {order_sql} {limit_sql};"
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_history_record(r) for r in rows]

    def list_results(
        self,
        platform: str | None = None,
        query: str = "",
        *,
        status: str | None = None,
        confidence: str | None = None,
        sort_desc: bool = True,
        limit: int | None = None,
    ) -> list[SearchResult]:
        records = self.list_records(
            platform=platform,
            status=status,
            confidence=confidence,
            query=query,
            sort_desc=sort_desc,
            limit=limit,
        )
        return [r.result for r in records]

    def get_latest_for_batch_items(self, batch_item_ids: list[int]) -> dict[int, SearchResult]:
        """Return latest SearchResult per batch_item_id, preferring FOUND if present."""
        if not batch_item_ids:
            return {}
        placeholders = ",".join("?" for _ in batch_item_ids)
        sql = (
            f"SELECT * FROM search_history WHERE batch_item_id IN ({placeholders}) "
            f"ORDER BY id ASC;"
        )
        out: dict[int, SearchResult] = {}
        with self.db.connect() as conn:
            rows = conn.execute(sql, [int(i) for i in batch_item_ids]).fetchall()
        for row in rows:
            rec = _row_to_history_record(row)
            if rec.batch_item_id is None:
                continue
            prev = out.get(rec.batch_item_id)
            # Never replace a persisted FOUND record with a subsequent non-FOUND record
            if prev is not None and prev.status == "FOUND" and rec.result.status != "FOUND":
                continue
            out[rec.batch_item_id] = rec.result
        return out

    def delete_by_ids(self, record_ids: list[int]) -> int:
        if not record_ids:
            return 0
        placeholders = ",".join("?" for _ in record_ids)
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"DELETE FROM search_history WHERE id IN ({placeholders});",
                [int(i) for i in record_ids],
            )
            return int(cur.rowcount)

    def clear_history(self) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM search_history;")
            return int(cur.rowcount)

    def count(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM search_history;").fetchone()
            return int(row["c"]) if row else 0

    # Backward-compatible settings delegation
    def load_settings(self) -> AppSettings:
        return self._settings_repo.load()

    def save_settings(self, settings: AppSettings) -> AppSettings:
        return self._settings_repo.save(settings)
