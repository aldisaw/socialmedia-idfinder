"""Results table page with status/confidence filtering, sorting, Copy Selected IDs, and CSV/JSON/TXT export."""

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
from core.models import Confidence, SearchResult
from core.validator import find_duplicate_user_ids
from exports.exporter import export_results
from gui.theme import CONFIDENCE_COLORS, PLATFORM_META, STATUS_COLORS, get_platform_icon

_CONFIDENCE_RANK: dict[str, int] = {
    Confidence.HIGH.value: 0,
    Confidence.MEDIUM.value: 1,
    Confidence.LOW.value: 2,
    Confidence.NOT_VERIFIED.value: 3,
}

FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("All", "all"),
    ("Found", "status:FOUND"),
    ("Not Found", "status:NOT_FOUND"),
    ("Blocked", "status:BLOCKED"),
    ("Login Required", "status:LOGIN_REQUIRED"),
    ("CAPTCHA Required", "status:CAPTCHA_REQUIRED"),
    ("Error", "status:ERROR"),
    ("Timeout", "status:TIMEOUT"),
    ("Cancelled", "status:CANCELLED"),
    ("High Confidence", "conf:HIGH"),
    ("Medium Confidence", "conf:MEDIUM"),
    ("Low Confidence", "conf:LOW"),
)

SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Sort: Default", "default"),
    ("Sort: Platform (A-Z)", "platform"),
    ("Sort: Confidence (High -> Low)", "confidence"),
    ("Sort: Response Time (Fast -> Slow)", "time_asc"),
    ("Sort: Response Time (Slow -> Fast)", "time_desc"),
)

EXPORT_SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Export: Filtered", "filtered"),
    ("Export: Selected", "selected"),
    ("Export: All", "all"),
)


class ResultsPage(QWidget):
    """Displays extracted results with filtering, sorting, selection copy, and multi-format export."""

    status_message = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._results: list[SearchResult] = []
        self._filtered: list[SearchResult] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        title = QLabel("Results")
        title.setObjectName("PageTitle")
        desc = QLabel(
            "Filter, sort, inspect duplicate IDs without merging accounts, copy selected IDs, and export."
        )
        desc.setObjectName("PageDesc")
        root.addWidget(title)
        root.addWidget(desc)

        # Row 1: Search + Filter + Sort
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Search username, user ID, platform, input, or status...")
        self._filter_edit.textChanged.connect(self._render_table)

        self._filter_combo = QComboBox()
        self._filter_combo.setMinimumWidth(155)
        for label, key in FILTER_OPTIONS:
            self._filter_combo.addItem(label, key)
        self._filter_combo.currentIndexChanged.connect(self._render_table)

        self._sort_combo = QComboBox()
        self._sort_combo.setMinimumWidth(200)
        for label, key in SORT_OPTIONS:
            self._sort_combo.addItem(label, key)
        self._sort_combo.currentIndexChanged.connect(self._render_table)

        filter_bar.addWidget(self._filter_edit, 1)
        filter_bar.addWidget(self._filter_combo)
        filter_bar.addWidget(self._sort_combo)
        root.addLayout(filter_bar)

        # Row 2: Actions + Export scope + CSV/JSON/TXT
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self._summary_label = QLabel("Showing 0 / 0 results")
        self._summary_label.setObjectName("PageDesc")

        self._btn_copy_selected = QPushButton("Copy Selected IDs")
        self._btn_copy_selected.setObjectName("SecondaryButton")
        self._btn_copy_selected.clicked.connect(self.copy_selected_ids)

        self._export_scope_combo = QComboBox()
        self._export_scope_combo.setMinimumWidth(135)
        for label, key in EXPORT_SCOPE_OPTIONS:
            self._export_scope_combo.addItem(label, key)

        btn_csv = QPushButton("Export CSV")
        btn_csv.setObjectName("SecondaryButton")
        btn_csv.clicked.connect(lambda: self._export("csv"))

        btn_json = QPushButton("Export JSON")
        btn_json.setObjectName("SecondaryButton")
        btn_json.clicked.connect(lambda: self._export("json"))

        btn_txt = QPushButton("Export TXT")
        btn_txt.setObjectName("SecondaryButton")
        btn_txt.clicked.connect(lambda: self._export("txt"))

        btn_clear = QPushButton("Clear")
        btn_clear.setObjectName("SecondaryButton")
        btn_clear.clicked.connect(self.clear_results)

        action_bar.addWidget(self._summary_label)
        action_bar.addStretch()
        action_bar.addWidget(self._btn_copy_selected)
        action_bar.addWidget(self._export_scope_combo)
        action_bar.addWidget(btn_csv)
        action_bar.addWidget(btn_json)
        action_bar.addWidget(btn_txt)
        action_bar.addWidget(btn_clear)
        root.addLayout(action_bar)

        self._table = QTableWidget(0, 10)
        self._table.setHorizontalHeaderLabels(
            [
                "Platform",
                "Username",
                "User ID",
                "ID Type",
                "Profile URL",
                "Method",
                "Confidence",
                "Status",
                "Time",
                "Action",
            ]
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
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._table, 1)

    def add_result(self, result: SearchResult) -> None:
        self._results.insert(0, result)
        self._render_table()

    def set_results(self, results: list[SearchResult]) -> None:
        self._results = list(results)
        self._render_table()

    def clear_results(self) -> None:
        self._results.clear()
        self._render_table()

    def filtered_results(self) -> list[SearchResult]:
        return list(self._filtered)

    def selected_row_indices(self) -> list[int]:
        model = self._table.selectionModel()
        if model is None:
            return []
        return sorted({idx.row() for idx in model.selectedRows()})

    def get_export_target_results(self, scope: str | None = None) -> list[SearchResult]:
        chosen_scope = scope or str(self._export_scope_combo.currentData() or "filtered")
        if chosen_scope == "all":
            return list(self._results)
        if chosen_scope == "selected":
            rows = self.selected_row_indices()
            return [self._filtered[i] for i in rows if 0 <= i < len(self._filtered)]
        return list(self._filtered)

    def _apply_filter_and_sort(self) -> list[SearchResult]:
        q = self._filter_edit.text().strip().lower()
        filter_key = str(self._filter_combo.currentData() or "all")
        sort_key = str(self._sort_combo.currentData() or "default")

        items = list(self._results)

        if filter_key.startswith("status:"):
            target_status = filter_key.split(":", 1)[1]
            items = [r for r in items if r.status == target_status]
        elif filter_key.startswith("conf:"):
            target_conf = filter_key.split(":", 1)[1]
            items = [r for r in items if r.confidence == target_conf]

        if q:
            items = [
                r
                for r in items
                if q in r.platform.lower()
                or q in r.username.lower()
                or q in r.user_id.lower()
                or q in r.status.lower()
                or q in r.input.lower()
                or q in r.profile_url.lower()
            ]

        if sort_key == "platform":
            items.sort(key=lambda r: (r.platform.lower(), r.username.lower()))
        elif sort_key == "confidence":
            items.sort(key=lambda r: (_CONFIDENCE_RANK.get(r.confidence, 99), r.platform.lower()))
        elif sort_key == "time_asc":
            items.sort(key=lambda r: float(r.response_time))
        elif sort_key == "time_desc":
            items.sort(key=lambda r: float(r.response_time), reverse=True)

        return items

    def _render_table(self) -> None:
        self._filtered = self._apply_filter_and_sort()
        dups = find_duplicate_user_ids(self._filtered)

        dup_note = f"  •  {len(dups)} duplicate ID group(s)" if dups else ""
        self._summary_label.setText(
            f"Showing {len(self._filtered)} / {len(self._results)} results{dup_note}"
        )

        self._table.setRowCount(len(self._filtered))
        for row, res in enumerate(self._filtered):
            _, _, disp = PLATFORM_META.get(res.platform, ("?", "#64748B", res.platform))
            p_item = QTableWidgetItem(disp)
            p_item.setIcon(get_platform_icon(res.platform, 18))
            self._table.setItem(row, 0, p_item)
            self._table.setItem(row, 1, QTableWidgetItem(res.username or "—"))

            is_dup = (res.platform, res.user_id) in dups
            uid_text = f"{res.user_id} [DUP]" if (res.user_id and is_dup) else (res.user_id or "—")
            uid_item = QTableWidgetItem(uid_text)
            if is_dup:
                uid_item.setForeground(QColor("#F59E0B"))
                uid_item.setToolTip("Duplicate User ID detected across multiple entries (rows kept separate).")
            self._table.setItem(row, 2, uid_item)

            self._table.setItem(row, 3, QTableWidgetItem(res.id_type or "UNKNOWN"))
            self._table.setItem(row, 4, QTableWidgetItem(res.profile_url or res.input))
            method_item = QTableWidgetItem(res.extraction_method or "NONE")
            if res.extraction_source:
                method_item.setToolTip(f"Source: {res.extraction_source}")
            self._table.setItem(row, 5, method_item)

            conf_item = QTableWidgetItem(res.confidence)
            conf_item.setForeground(QColor(CONFIDENCE_COLORS.get(res.confidence, "#94A3B8")))
            conf_item.setToolTip(f"Validation: {res.validation_status}")
            self._table.setItem(row, 6, conf_item)

            st_item = QTableWidgetItem(res.status)
            st_item.setForeground(QColor(STATUS_COLORS.get(res.status, "#94A3B8")))
            if res.error_message:
                st_item.setToolTip(res.error_message)
            self._table.setItem(row, 7, st_item)

            self._table.setItem(row, 8, QTableWidgetItem(f"{res.response_time:.2f}s"))

            copy_btn = QPushButton("Copy ID")
            copy_btn.setObjectName("SmallButton")
            copy_btn.setEnabled(bool(res.user_id))
            copy_btn.clicked.connect(lambda _=False, uid=res.user_id: self._copy_user_id(uid))
            self._table.setCellWidget(row, 9, copy_btn)
            self._table.setRowHeight(row, 34)

    def _copy_user_id(self, user_id: str) -> None:
        if user_id:
            QGuiApplication.clipboard().setText(user_id)
            self.status_message.emit(f"Copied User ID '{user_id}' to clipboard.")

    def copy_selected_ids(self) -> list[str]:
        """Copy non-empty user IDs from selected rows to clipboard."""
        rows = self.selected_row_indices()
        ids = [
            self._filtered[i].user_id
            for i in rows
            if 0 <= i < len(self._filtered) and self._filtered[i].user_id
        ]
        if not ids:
            self.status_message.emit("No selected rows with valid User IDs to copy.")
            return []
        QGuiApplication.clipboard().setText("\n".join(ids))
        self.status_message.emit(f"Copied {len(ids)} selected User ID(s) to clipboard.")
        return ids

    def _export(self, fmt: str) -> None:
        target_results = self.get_export_target_results()
        if not target_results:
            scope = str(self._export_scope_combo.currentData() or "filtered")
            msg = (
                "No rows selected for export."
                if scope == "selected"
                else "No results available to export."
            )
            QMessageBox.information(self, "Export", msg)
            return

        ext_map = {"csv": "CSV Files (*.csv)", "json": "JSON Files (*.json)", "txt": "Text Files (*.txt)"}
        default_file = str(get_exports_dir() / f"social_id_results.{fmt}")
        path_str, _ = QFileDialog.getSaveFileName(self, f"Export {fmt.upper()}", default_file, ext_map[fmt])
        if not path_str:
            return
        try:
            out = export_results(target_results, Path(path_str), fmt)
            self.status_message.emit(f"Exported {len(target_results)} results to {out}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))
