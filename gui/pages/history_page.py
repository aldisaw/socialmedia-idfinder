"""Persistent SQLite Search History page with filters, date sort, selected delete, and export."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import get_exports_dir
from core.models import Confidence, ResultStatus, SUPPORTED_PLATFORMS
from database.history_repo import HistoryRecord, HistoryRepository
from exports.exporter import export_results
from gui.theme import CONFIDENCE_COLORS, PLATFORM_META, STATUS_COLORS, get_platform_icon


class HistoryPage(QWidget):
    """Searchable, filterable SQLite search history with export and row deletion."""

    status_message = Signal(str)
    history_changed = Signal()

    def __init__(self, repository: HistoryRepository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repository
        self._records: list[HistoryRecord] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        title = QLabel("History")
        title.setObjectName("PageTitle")
        desc = QLabel(
            "Persistent SQLite search history (no passwords, cookies, or auth tokens are ever stored)."
        )
        desc.setObjectName("PageDesc")
        root.addWidget(title)
        root.addWidget(desc)

        # Row 1: Filters & search
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)

        self._platform_filter = QComboBox()
        self._platform_filter.setMinimumWidth(135)
        self._platform_filter.addItem("All Platforms", "all")
        for plat in SUPPORTED_PLATFORMS:
            self._platform_filter.addItem(get_platform_icon(plat.value, 16), plat.display_name, plat.value)
        self._platform_filter.currentIndexChanged.connect(self.refresh)

        self._status_filter = QComboBox()
        self._status_filter.setMinimumWidth(130)
        self._status_filter.addItem("All Statuses", "all")
        for st in (
            ResultStatus.FOUND,
            ResultStatus.NOT_FOUND,
            ResultStatus.BLOCKED,
            ResultStatus.LOGIN_REQUIRED,
            ResultStatus.CAPTCHA_REQUIRED,
            ResultStatus.RATE_LIMITED,
            ResultStatus.ERROR,
            ResultStatus.TIMEOUT,
            ResultStatus.CANCELLED,
            ResultStatus.INVALID_INPUT,
        ):
            self._status_filter.addItem(st.value, st.value)
        self._status_filter.currentIndexChanged.connect(self.refresh)

        self._conf_filter = QComboBox()
        self._conf_filter.setMinimumWidth(140)
        self._conf_filter.addItem("All Confidence", "all")
        for conf in (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW, Confidence.NOT_VERIFIED):
            self._conf_filter.addItem(conf.value, conf.value)
        self._conf_filter.currentIndexChanged.connect(self.refresh)

        self._sort_combo = QComboBox()
        self._sort_combo.setMinimumWidth(135)
        self._sort_combo.addItem("Date: Newest", True)
        self._sort_combo.addItem("Date: Oldest", False)
        self._sort_combo.currentIndexChanged.connect(self.refresh)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search username, user ID, input, or URL...")
        self._search_edit.textChanged.connect(self.refresh)

        filter_bar.addWidget(self._platform_filter)
        filter_bar.addWidget(self._status_filter)
        filter_bar.addWidget(self._conf_filter)
        filter_bar.addWidget(self._sort_combo)
        filter_bar.addWidget(self._search_edit, 1)
        root.addLayout(filter_bar)

        # Row 2: Count + Export + Delete/Clear
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self._count_label = QLabel("0 records")
        self._count_label.setObjectName("PageDesc")

        btn_refresh = QPushButton("Refresh")
        btn_refresh.setObjectName("SecondaryButton")
        btn_refresh.clicked.connect(self.refresh)

        btn_csv = QPushButton("Export CSV")
        btn_csv.setObjectName("SecondaryButton")
        btn_csv.clicked.connect(lambda: self._export("csv"))

        btn_json = QPushButton("Export JSON")
        btn_json.setObjectName("SecondaryButton")
        btn_json.clicked.connect(lambda: self._export("json"))

        btn_txt = QPushButton("Export TXT")
        btn_txt.setObjectName("SecondaryButton")
        btn_txt.clicked.connect(lambda: self._export("txt"))

        self._btn_delete_selected = QPushButton("Delete Selected")
        self._btn_delete_selected.setObjectName("SecondaryButton")
        self._btn_delete_selected.clicked.connect(self.delete_selected)

        btn_clear = QPushButton("Clear History")
        btn_clear.setObjectName("DangerButton")
        btn_clear.clicked.connect(self._clear_all)

        action_bar.addWidget(self._count_label)
        action_bar.addStretch()
        action_bar.addWidget(btn_refresh)
        action_bar.addWidget(btn_csv)
        action_bar.addWidget(btn_json)
        action_bar.addWidget(btn_txt)
        action_bar.addWidget(self._btn_delete_selected)
        action_bar.addWidget(btn_clear)
        root.addLayout(action_bar)

        self._table = QTableWidget(0, 9)
        self._table.setHorizontalHeaderLabels(
            ["Timestamp", "Platform", "Username", "User ID", "ID Type", "Source", "Confidence", "Status", "Action"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._table, 1)

    def current_records(self) -> list[HistoryRecord]:
        return list(self._records)

    def refresh(self) -> None:
        plat = str(self._platform_filter.currentData() or "all")
        status = str(self._status_filter.currentData() or "all")
        conf = str(self._conf_filter.currentData() or "all")
        sort_desc = bool(self._sort_combo.currentData())
        query = self._search_edit.text()

        self._records = self._repo.list_records(
            platform=plat,
            status=status,
            confidence=conf,
            query=query,
            sort_desc=sort_desc,
        )
        self._count_label.setText(f"{len(self._records)} record(s)")
        self._table.setRowCount(len(self._records))

        for row, hrec in enumerate(self._records):
            rec = hrec.result
            self._table.setItem(row, 0, QTableWidgetItem(rec.created_at))
            _, _, disp = PLATFORM_META.get(rec.platform, ("?", "#64748B", rec.platform))
            p_item = QTableWidgetItem(disp)
            p_item.setIcon(get_platform_icon(rec.platform, 16))
            self._table.setItem(row, 1, p_item)
            self._table.setItem(row, 2, QTableWidgetItem(rec.username or rec.input))
            self._table.setItem(row, 3, QTableWidgetItem(rec.user_id or "—"))
            self._table.setItem(row, 4, QTableWidgetItem(rec.id_type or "UNKNOWN"))

            src_item = QTableWidgetItem(rec.extraction_source or rec.extraction_method or "NONE")
            src_item.setToolTip(f"Input: {rec.input}\nValidation: {rec.validation_status}")
            self._table.setItem(row, 5, src_item)

            c_item = QTableWidgetItem(rec.confidence)
            c_item.setForeground(QColor(CONFIDENCE_COLORS.get(rec.confidence, "#94A3B8")))
            self._table.setItem(row, 6, c_item)

            s_item = QTableWidgetItem(rec.status)
            s_item.setForeground(QColor(STATUS_COLORS.get(rec.status, "#94A3B8")))
            if rec.error_message:
                s_item.setToolTip(rec.error_message)
            self._table.setItem(row, 7, s_item)

            btn = QPushButton("Copy ID")
            btn.setObjectName("SmallButton")
            btn.setEnabled(bool(rec.user_id))
            btn.clicked.connect(lambda _=False, uid=rec.user_id: self._copy_id(uid))
            self._table.setCellWidget(row, 8, btn)
            self._table.setRowHeight(row, 32)

    def _copy_id(self, uid: str) -> None:
        if uid:
            QGuiApplication.clipboard().setText(uid)
            self.status_message.emit(f"Copied User ID '{uid}' from history.")

    def delete_selected(self) -> int:
        model = self._table.selectionModel()
        if model is None:
            return 0
        rows = sorted({idx.row() for idx in model.selectedRows()})
        ids = [self._records[r].id for r in rows if 0 <= r < len(self._records)]
        if not ids:
            self.status_message.emit("No history rows selected.")
            return 0
        deleted = self._repo.delete_by_ids(ids)
        self.refresh()
        self.history_changed.emit()
        self.status_message.emit(f"Deleted {deleted} history record(s).")
        return deleted

    def _clear_all(self) -> None:
        if self._repo.count() == 0:
            self.status_message.emit("History is already empty.")
            return
        reply = QMessageBox.question(
            self,
            "Clear Search History",
            "Delete all persisted search history records? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._repo.clear_history()
        self.refresh()
        self.history_changed.emit()
        self.status_message.emit("Search history cleared.")

    def _export(self, fmt: str) -> None:
        results = [h.result for h in self._records]
        if not results:
            QMessageBox.information(self, "Export History", "No historical records match current filters.")
            return
        ext_map = {"csv": "CSV Files (*.csv)", "json": "JSON Files (*.json)", "txt": "Text Files (*.txt)"}
        default_file = str(get_exports_dir() / f"social_id_history.{fmt}")
        path_str, _ = QFileDialog.getSaveFileName(self, f"Export History ({fmt.upper()})", default_file, ext_map[fmt])
        if not path_str:
            return
        try:
            out = export_results(results, Path(path_str), fmt)
            self.status_message.emit(f"Exported {len(results)} history record(s) to {out}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))
