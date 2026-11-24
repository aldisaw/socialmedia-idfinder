"""Comprehensive offline tests for Phase 6 SQLite persistence, migrations, recovery, and GUI integration."""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

from config import BASE_DIR, AppSettings, get_default_db_path
from core.engine import ExtractionEngine
from core.http_client import HttpClient
from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from core.normalizer import normalize_input
from core.scheduler import BatchScheduler
from database import (
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_INTERRUPTED,
    BATCH_STATUS_RUNNING,
    CURRENT_SCHEMA_VERSION,
    BatchRepository,
    DatabaseCorruptionError,
    DatabaseManager,
    HistoryRepository,
    SettingsRepository,
)
from gui.main_window import MainWindow


def _yt_html(handle: str, cid: str) -> str:
    return (
        f'<script>var ytInitialData={{"metadata":{{"channelMetadataRenderer":'
        f'{{"externalId":"{cid}","vanityChannelUrl":"https://www.youtube.com/@{handle}"}}}}}};</script>'
    )


# ---------------------------------------------------------------------------
# 1. Database location, WAL mode, Foreign Keys, Schema & Indexes
# ---------------------------------------------------------------------------

def test_default_db_path_outside_project_source_tree() -> None:
    db_path = get_default_db_path()
    assert db_path.name == "social_id_finder.db"
    assert BASE_DIR not in db_path.parents


def test_db_creation_wal_foreign_keys_and_indexes(tmp_path: Path) -> None:
    db_file = tmp_path / "social_id_finder.db"
    db = DatabaseManager(db_path=db_file)
    version = db.initialize()

    assert db_file.exists()
    assert version == CURRENT_SCHEMA_VERSION
    assert db.get_schema_version() == CURRENT_SCHEMA_VERSION

    with db.connect() as conn:
        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
        }

    assert str(jm).lower() == "wal"
    assert int(fk) == 1
    assert {"batch_sessions", "batch_items", "search_history", "app_settings", "schema_migrations"} <= tables
    assert "idx_history_created" in indexes
    assert "idx_batch_items_batch_pos" in indexes


# ---------------------------------------------------------------------------
# 2. Schema migration with automatic backup & data preservation
# ---------------------------------------------------------------------------

def test_schema_migration_preserves_data_and_creates_backup(tmp_path: Path) -> None:
    db_file = tmp_path / "migrate.db"
    db = DatabaseManager(db_path=db_file)
    db.initialize()

    hist = HistoryRepository(db)
    hist.save_result(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input="https://www.youtube.com/@Google",
            username="@Google",
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="ytInitialData",
            confidence=Confidence.HIGH.value,
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        )
    )

    def _migrate_next(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE batch_sessions ADD COLUMN note TEXT NOT NULL DEFAULT '';")

    next_v = CURRENT_SCHEMA_VERSION + 1
    new_ver = db.initialize(extra_migrations=[(next_v, "Add batch_sessions.note column", True, _migrate_next)])
    assert new_ver == next_v
    assert db.get_schema_version() == next_v

    # Backup created before destructive migration
    backups = list(tmp_path.glob(f"migrate.db.pre_v{next_v}.*.bak"))
    assert len(backups) == 1
    # Existing data preserved
    assert len(hist.list_results()) == 1
    assert hist.list_results()[0].user_id == "UCK8sQmJBp8GCxrOtXWBpyEA"


# ---------------------------------------------------------------------------
# 3. Foreign key integrity & transaction rollback
# ---------------------------------------------------------------------------

def test_foreign_key_integrity_and_transaction_rollback(tmp_path: Path) -> None:
    db = DatabaseManager(db_path=tmp_path / "fk.db")
    db.initialize()

    # Invalid foreign key must raise IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO batch_items (
                    batch_id, position, original_input, platform, status, created_at, updated_at
                ) VALUES (99999, 0, 'x', 'youtube', 'PENDING', '2026-01-01', '2026-01-01');
                """
            )

    # Transaction rollback must leave zero committed rows
    with pytest.raises(RuntimeError, match="forced failure"):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?);",
                ("debug_mode", "1", "2026-01-01"),
            )
            raise RuntimeError("forced failure")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM app_settings WHERE key = 'debug_mode';").fetchone()
    assert row is None

    # ON DELETE SET NULL on search_history.batch_item_id when batch session deleted
    brepo = BatchRepository(db)
    hrepo = HistoryRepository(db)
    session, items = brepo.create_batch([normalize_input("https://www.youtube.com/@Google")])
    brepo.record_item_result(
        session.id,
        items[0].id,
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input="https://www.youtube.com/@Google",
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            status=ResultStatus.FOUND.value,
        ),
    )
    brepo.delete_session(session.id)
    assert brepo.get_batch_items(session.id) == []
    # History entry survives with batch_item_id=NULL
    records = hrepo.list_records()
    assert len(records) == 1
    assert records[0].batch_item_id is None


# ---------------------------------------------------------------------------
# 4. Settings persistence, validation, corrupted values & secret rejection
# ---------------------------------------------------------------------------

def test_settings_persistence_corrupted_fallback_and_secret_rejection(tmp_path: Path) -> None:
    db_file = tmp_path / "settings.db"
    s1 = SettingsRepository(db_file)

    custom = AppSettings(
        request_timeout=22.5,
        item_timeout=45.0,
        batch_timeout=90.0,
        max_concurrency=6,
        retry_limit=0,
        rate_limit_cooldown=25.0,
        user_agent="CustomAgent/1.0",
        debug_mode=True,
    )
    s1.save(custom)

    # Reload via fresh repository instance (simulates restart)
    s2 = SettingsRepository(db_file)
    loaded = s2.load()
    assert loaded.request_timeout == 22.5
    assert loaded.item_timeout == 45.0
    assert loaded.batch_timeout == 90.0
    assert loaded.max_concurrency == 6
    assert loaded.retry_limit == 0
    assert loaded.rate_limit_cooldown == 25.0
    assert loaded.user_agent == "CustomAgent/1.0"
    assert loaded.debug_mode is True

    # Secret keys strictly rejected
    for forbidden in ("cookie", "password", "auth_token", "session_secret"):
        with pytest.raises(ValueError):
            s2.set_raw(forbidden, "secret")

    # Corrupted setting value falls back cleanly to default without crashing
    with s2.db.transaction() as conn:
        conn.execute("UPDATE app_settings SET value = 'NOT_A_NUMBER' WHERE key = 'request_timeout';")
        conn.execute("UPDATE app_settings SET value = 'not_a_bool' WHERE key = 'debug_mode';")
    recovered = s2.load()
    assert recovered.request_timeout == AppSettings().request_timeout
    assert recovered.debug_mode is False
    assert recovered.max_concurrency == 6  # unaffected valid settings preserved


# ---------------------------------------------------------------------------
# 5. History persistence, filtering, sorting, deletion & sanitization
# ---------------------------------------------------------------------------

def test_history_persistence_filters_delete_and_no_credentials(tmp_path: Path) -> None:
    db_file = tmp_path / "hist.db"
    h1 = HistoryRepository(db_file)

    id1 = h1.save_result(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input="https://www.youtube.com/@Google",
            username="@Google",
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="ytInitialData",
            confidence=Confidence.HIGH.value,
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
            created_at="2026-11-20T10:00:00+00:00",
        )
    )
    id2 = h1.save_result(
        SearchResult(
            platform=Platform.X.value,
            input="https://x.com/jack",
            username="jack",
            user_id="",
            status=ResultStatus.BLOCKED.value,
            confidence=Confidence.NOT_VERIFIED.value,
            error_message="Cookie=sess=leak123 Authorization: Bearer secretTok99",
            created_at="2026-11-21T10:00:00+00:00",
        )
    )

    # Fresh repo instance simulates app restart
    h2 = HistoryRepository(db_file)
    assert h2.count() == 2

    # Date sorting
    desc = h2.list_records(sort_desc=True)
    asc = h2.list_records(sort_desc=False)
    assert [r.id for r in desc] == [id2, id1]
    assert [r.id for r in asc] == [id1, id2]

    # Sanitization verified in persisted row
    assert "leak123" not in desc[0].result.error_message
    assert "secretTok99" not in desc[0].result.error_message

    # Platform, status, confidence, query filters
    assert len(h2.list_records(platform=Platform.YOUTUBE.value)) == 1
    assert len(h2.list_records(status=ResultStatus.BLOCKED.value)) == 1
    assert len(h2.list_records(confidence=Confidence.HIGH.value)) == 1
    assert len(h2.list_records(query="UCK8sQm")) == 1

    # Delete selected record
    assert h2.delete_by_ids([id2]) == 1
    assert h2.count() == 1
    assert h2.list_records()[0].id == id1


# ---------------------------------------------------------------------------
# 6. Batch persistence, interrupted recovery, resume, FOUND protection
# ---------------------------------------------------------------------------

async def test_batch_persistence_interrupted_recovery_resume_and_found_protection(tmp_path: Path) -> None:
    db_file = tmp_path / "batch_recovery.db"
    db1 = DatabaseManager(db_path=db_file)
    db1.initialize()
    brepo1 = BatchRepository(db1)

    items = [
        normalize_input("https://www.youtube.com/@first"),
        normalize_input("https://www.youtube.com/@second"),
        normalize_input("https://www.youtube.com/@third"),
        normalize_input("bare_invalid"),
    ]
    session, item_recs = brepo1.create_batch(items)
    assert session.status == BATCH_STATUS_RUNNING
    assert [r.position for r in item_recs] == [0, 1, 2, 3]

    # Simulate partial progress before unexpected crash:
    # item 0 FOUND, item 1 RUNNING (mid-flight), item 2 PENDING, item 3 INVALID_INPUT
    found_res = SearchResult(
        platform=Platform.YOUTUBE.value,
        input=items[0].raw_input,
        username="@first",
        profile_url=items[0].profile_url,
        user_id="UCFIRST00000000000000001",
        id_type=IdType.CHANNEL_ID.value,
        extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
        extraction_source="ytInitialData",
        confidence=Confidence.HIGH.value,
        validation_status=ResultValidationStatus.VERIFIED.value,
        status=ResultStatus.FOUND.value,
    )
    assert brepo1.record_item_result(session.id, item_recs[0].id, found_res) is True
    brepo1.mark_item_running(session.id, item_recs[1].id)

    # Attempt to overwrite FOUND item with a failed retry MUST be blocked!
    failed_overwrite = SearchResult(
        platform=Platform.YOUTUBE.value,
        input=items[0].raw_input,
        status=ResultStatus.ERROR.value,
        error_message="Should not overwrite FOUND",
    )
    assert brepo1.record_item_result(session.id, item_recs[0].id, failed_overwrite, increment_retry=True) is False

    # --- Simulate application restart with fresh DB manager & repositories ---
    db2 = DatabaseManager(db_path=db_file)
    db2.initialize()
    brepo2 = BatchRepository(db2)

    interrupted = brepo2.mark_running_batches_interrupted()
    assert interrupted == [session.id]

    loaded = brepo2.load_batch_state(session.id)
    assert loaded is not None
    restored_session, restored_items, restored_recs, restored_results = loaded
    assert restored_session.status == BATCH_STATUS_INTERRUPTED
    assert restored_results[0] is not None and restored_results[0].user_id == "UCFIRST00000000000000001"
    assert restored_recs[1].status == ResultStatus.PENDING.value  # reset from RUNNING
    assert restored_recs[2].status == ResultStatus.PENDING.value

    resumable = BatchRepository.select_resumable_indices(restored_items, restored_recs, restored_results)
    assert resumable == [1, 2]  # excludes FOUND (0) and INVALID_INPUT (3)

    # Resume ONLY resumable items with mocked HTTP; ensure @first is NEVER re-requested
    requested_urls: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        requested_urls.append(url)
        if "@second" in url:
            return httpx.Response(200, text=_yt_html("second", "UCSECOND0000000000000002"))
        if "@third" in url:
            return httpx.Response(200, text=_yt_html("third", "UCTHIRD00000000000000003"))
        return httpx.Response(500)

    settings = AppSettings(request_timeout=5.0, item_timeout=10.0, batch_timeout=30.0, max_concurrency=2, retry_limit=0)
    hc = HttpClient(timeout=5.0, retry_limit=0, transport=httpx.MockTransport(handler))
    async with ExtractionEngine(settings=settings, http_client=hc) as engine:
        scheduler = BatchScheduler(engine=engine, settings=settings)

        def _on_done(idx: int, res: SearchResult) -> None:
            brepo2.record_item_result(session.id, restored_recs[idx].id, res, increment_retry=True)

        final_results = await scheduler.run_batch(
            restored_items,
            on_item_done=_on_done,
            indices_to_run=resumable,
            existing_results=restored_results,
        )
    await hc.aclose()
    brepo2.finalize_batch(session.id, BATCH_STATUS_COMPLETED)

    assert all("@first" not in u for u in requested_urls)
    assert len(requested_urls) == 2
    assert final_results[0].user_id == "UCFIRST00000000000000001"
    assert final_results[1].user_id == "UCSECOND0000000000000002"
    assert final_results[2].user_id == "UCTHIRD00000000000000003"
    assert final_results[3].status == ResultStatus.INVALID_INPUT.value
    assert brepo2.get_session(session.id).status == BATCH_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# 7. Concurrent multi-thread repository access (WAL + per-op connections)
# ---------------------------------------------------------------------------

def test_concurrent_repository_access_from_multiple_threads(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent.db"
    db = DatabaseManager(db_path=db_file)
    db.initialize()
    repo = HistoryRepository(db)

    def _writer(worker_id: int) -> int:
        for i in range(10):
            repo.save_result(
                SearchResult(
                    platform=Platform.YOUTUBE.value,
                    input=f"https://www.youtube.com/@w{worker_id}_{i}",
                    username=f"@w{worker_id}_{i}",
                    user_id=f"UCW{worker_id:02d}{i:02d}00000000000000000",
                    status=ResultStatus.FOUND.value,
                )
            )
        return len(repo.list_results())

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(_writer, w) for w in range(5)]
        counts = [f.result() for f in futs]

    assert all(c > 0 for c in counts)
    assert repo.count() == 50


# ---------------------------------------------------------------------------
# 8. Database corruption handling (preserves original file untouched)
# ---------------------------------------------------------------------------

def test_database_corruption_raises_and_preserves_original_file(tmp_path: Path) -> None:
    corrupt_file = tmp_path / "corrupted.db"
    original_bytes = b"THIS IS NOT A VALID SQLITE DATABASE HEADER !@#$%"
    corrupt_file.write_bytes(original_bytes)

    db = DatabaseManager(db_path=corrupt_file)
    with pytest.raises(DatabaseCorruptionError) as exc_info:
        db.initialize()

    assert "Recovery:" in str(exc_info.value)
    assert str(corrupt_file) in str(exc_info.value)
    # Original corrupted file MUST NOT be overwritten or deleted
    assert corrupt_file.exists()
    assert corrupt_file.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# 9. GUI startup, history restoration, settings reload, interrupted batch view
# ---------------------------------------------------------------------------

def test_gui_restores_history_settings_and_interrupted_batch_on_restart(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    db_file = tmp_path / "gui_restart.db"
    db = DatabaseManager(db_path=db_file)
    db.initialize()

    # Seed settings, history, and an interrupted RUNNING batch before launching GUI
    SettingsRepository(db).save(AppSettings(request_timeout=21.0, max_concurrency=4, rate_limit_cooldown=20.0))
    HistoryRepository(db).save_result(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input="https://www.youtube.com/@Google",
            username="@Google",
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="ytInitialData",
            confidence=Confidence.HIGH.value,
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        )
    )
    brepo = BatchRepository(db)
    session, recs = brepo.create_batch(
        [
            normalize_input("https://www.youtube.com/@Google"),
            normalize_input("https://www.youtube.com/@PendingOne"),
        ]
    )
    brepo.mark_item_running(session.id, recs[1].id)

    # Launch MainWindow against seeded DB (simulates app restart)
    win = MainWindow(db_path=db_file)

    # Verify settings loaded into SettingsPage
    assert win._settings.request_timeout == 21.0
    assert win._settings.max_concurrency == 4
    assert win._settings_page.current_settings().rate_limit_cooldown == 20.0

    # Verify history restored into HistoryPage & selected delete works
    assert len(win._history_page.current_records()) == 1
    assert win._history_page.current_records()[0].result.user_id == "UCK8sQmJBp8GCxrOtXWBpyEA"

    # Verify interrupted batch detected, NOT auto-started, loaded & Resume enabled
    assert win._worker is None  # NEVER auto-starts network work on startup
    assert session.id in win._interrupted_batch_ids
    assert not win._batch_page._interrupted_banner.isHidden()
    assert "interrupted" in win._batch_page._interrupted_banner.text().lower()
    assert win._batch_page._btn_resume.isEnabled()

    # Verify selected deletion on HistoryPage
    sel = win._history_page._table.selectionModel()
    assert sel is not None
    sel.select(
        win._history_page._table.model().index(0, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert win._history_page.delete_selected() == 1
    assert len(win._history_page.current_records()) == 0

    win.close()
