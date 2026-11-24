"""Dashboard overview page."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import ResultStatus, SUPPORTED_PLATFORMS, SearchResult
from gui.theme import PLATFORM_META, get_platform_icon


class DashboardPage(QWidget):
    """High-level stats, quick actions, and supported platforms status."""

    navigate_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(18)

        header = QVBoxLayout()
        header.setSpacing(4)
        title = QLabel("Dashboard")
        title.setObjectName("PageTitle")
        desc = QLabel("Public profile ID extraction overview across 5 supported platforms.")
        desc.setObjectName("PageDesc")
        header.addWidget(title)
        header.addWidget(desc)
        root.addLayout(header)

        # Stat cards
        cards_grid = QGridLayout()
        cards_grid.setSpacing(14)

        self._stat_total = self._make_stat_card("Total Searches", "0")
        self._stat_found = self._make_stat_card("IDs Found", "0")
        self._stat_rate = self._make_stat_card("Found Rate", "0%")
        self._stat_platforms = self._make_stat_card("Supported Platforms", str(len(SUPPORTED_PLATFORMS)))

        cards_grid.addWidget(self._stat_total[0], 0, 0)
        cards_grid.addWidget(self._stat_found[0], 0, 1)
        cards_grid.addWidget(self._stat_rate[0], 0, 2)
        cards_grid.addWidget(self._stat_platforms[0], 0, 3)
        root.addLayout(cards_grid)

        # Quick actions
        actions_card = QFrame()
        actions_card.setObjectName("Card")
        actions_layout = QHBoxLayout(actions_card)
        actions_layout.setContentsMargins(16, 14, 16, 14)
        actions_layout.setSpacing(12)

        qa_label = QLabel("Quick Actions")
        qa_label.setObjectName("SectionLabel")
        actions_layout.addWidget(qa_label)
        actions_layout.addStretch()

        btn_single = QPushButton("New Single Search")
        btn_single.clicked.connect(lambda: self.navigate_requested.emit("single"))
        btn_batch = QPushButton("Open Batch Search (Max 10)")
        btn_batch.setObjectName("SecondaryButton")
        btn_batch.clicked.connect(lambda: self.navigate_requested.emit("batch"))
        btn_results = QPushButton("View Results")
        btn_results.setObjectName("SecondaryButton")
        btn_results.clicked.connect(lambda: self.navigate_requested.emit("results"))

        actions_layout.addWidget(btn_single)
        actions_layout.addWidget(btn_batch)
        actions_layout.addWidget(btn_results)
        root.addWidget(actions_card)

        # Supported platforms table
        plat_label = QLabel("Supported Platforms & Strategy Pipeline")
        plat_label.setObjectName("SectionLabel")
        root.addWidget(plat_label)

        table = QTableWidget(len(SUPPORTED_PLATFORMS), 3)
        table.setHorizontalHeaderLabels(["Platform", "Input Formats", "Extraction Strategy Order"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        formats = {
            "instagram": "username, @username, instagram.com/<user>",
            "tiktok": "username, @username, tiktok.com/@<user>",
            "x": "username, @username, x.com/<user>, twitter.com/<user>",
            "facebook": "username, facebook.com/<user>, profile.php?id=<id>",
            "youtube": "@handle, youtube.com/@<handle>, youtube.com/channel/UC...",
        }
        pipeline = "1) Embedded JSON -> 2) Structured Metadata -> 3) HTML Attributes -> 4) Regex Fallback"

        for row, platform in enumerate(SUPPORTED_PLATFORMS):
            _, _, display = PLATFORM_META[platform.value]
            item_plat = QTableWidgetItem(display)
            item_plat.setIcon(get_platform_icon(platform.value, 18))
            table.setItem(row, 0, item_plat)
            table.setItem(row, 1, QTableWidgetItem(formats[platform.value]))
            table.setItem(row, 2, QTableWidgetItem(pipeline))
            table.setRowHeight(row, 34)

        table.setMaximumHeight(220)
        root.addWidget(table)
        root.addStretch()

    def _make_stat_card(self, label_text: str, initial_value: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        val_lbl = QLabel(initial_value)
        val_lbl.setObjectName("StatValue")
        name_lbl = QLabel(label_text)
        name_lbl.setObjectName("StatLabel")
        layout.addWidget(val_lbl)
        layout.addWidget(name_lbl)
        return card, val_lbl

    def update_stats(self, results: list[SearchResult]) -> None:
        total = len(results)
        found = sum(1 for r in results if r.status == ResultStatus.FOUND.value)
        rate = int((found / total) * 100) if total else 0
        self._stat_total[1].setText(str(total))
        self._stat_found[1].setText(str(found))
        self._stat_rate[1].setText(f"{rate}%")
