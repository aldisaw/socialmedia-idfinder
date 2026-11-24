"""Modern dark-mode theme and crisp vector platform icons for Social ID Finder."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from core.models import Confidence, Platform, ResultStatus

PLATFORM_META: dict[str, tuple[str, str, str]] = {
    Platform.INSTAGRAM.value: ("IG", "#E1306C", "Instagram"),
    Platform.TIKTOK.value: ("TT", "#25F4EE", "TikTok"),
    Platform.X.value: ("X", "#E7E9EA", "X (Twitter)"),
    Platform.FACEBOOK.value: ("FB", "#1877F2", "Facebook"),
    Platform.YOUTUBE.value: ("YT", "#FF0033", "YouTube"),
    Platform.UNKNOWN.value: ("?", "#64748B", "Unknown"),
}

CONFIDENCE_COLORS: dict[str, str] = {
    Confidence.HIGH.value: "#22C55E",
    Confidence.MEDIUM.value: "#F59E0B",
    Confidence.LOW.value: "#F97316",
    Confidence.NOT_VERIFIED.value: "#94A3B8",
}

STATUS_COLORS: dict[str, str] = {
    ResultStatus.FOUND.value: "#22C55E",
    ResultStatus.RUNNING.value: "#38BDF8",
    ResultStatus.PENDING.value: "#94A3B8",
    ResultStatus.NOT_FOUND.value: "#94A3B8",
    ResultStatus.RATE_LIMITED.value: "#F59E0B",
    ResultStatus.BLOCKED.value: "#EF4444",
    ResultStatus.LOGIN_REQUIRED.value: "#F59E0B",
    ResultStatus.CAPTCHA_REQUIRED.value: "#F97316",
    ResultStatus.TIMEOUT.value: "#F97316",
    ResultStatus.CANCELLED.value: "#A855F7",
    ResultStatus.INVALID_INPUT.value: "#EF4444",
    ResultStatus.ERROR.value: "#EF4444",
}

_ICON_CACHE: dict[str, QIcon] = {}


def get_platform_icon(platform_key: str, size: int = 20) -> QIcon:
    """Generate a crisp circular colored badge QIcon for a platform."""
    cache_key = f"{platform_key}:{size}"
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    short, color_hex, _ = PLATFORM_META.get(platform_key, PLATFORM_META[Platform.UNKNOWN.value])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    bg = QColor(color_hex)
    painter.setBrush(bg)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, size, size, 5, 5)

    text_color = QColor("#0B0F19") if platform_key in (Platform.TIKTOK.value, Platform.X.value) else QColor("#FFFFFF")
    painter.setPen(text_color)
    font = QFont("Segoe UI", max(7, int(size * 0.38)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, short)
    painter.end()

    icon = QIcon(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


DARK_STYLESHEET = """
QWidget {
    background-color: #0F172A;
    color: #E2E8F0;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}

QMainWindow {
    background-color: #0B1120;
}

#Sidebar {
    background-color: #0B1120;
    border-right: 1px solid #1E293B;
}

#AppTitle {
    color: #F8FAFC;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}

#AppSubtitle {
    color: #64748B;
    font-size: 11px;
    background: transparent;
}

QPushButton#NavButton {
    text-align: left;
    padding: 10px 14px;
    border-radius: 8px;
    border: none;
    background-color: transparent;
    color: #94A3B8;
    font-weight: 500;
}

QPushButton#NavButton:hover {
    background-color: #1E293B;
    color: #F1F5F9;
}

QPushButton#NavButton[active="true"] {
    background-color: #1D4ED8;
    color: #FFFFFF;
    font-weight: 600;
}

QFrame#Card {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 10px;
}

QLabel#PageTitle {
    font-size: 20px;
    font-weight: 700;
    color: #F8FAFC;
    background: transparent;
}

QLabel#PageDesc {
    color: #94A3B8;
    font-size: 12px;
    background: transparent;
}

QLabel#StatValue {
    font-size: 26px;
    font-weight: 700;
    color: #F8FAFC;
    background: transparent;
}

QLabel#StatLabel {
    font-size: 12px;
    color: #94A3B8;
    background: transparent;
}

QLabel#SectionLabel {
    font-size: 13px;
    font-weight: 600;
    color: #CBD5E1;
    background: transparent;
}

QLabel#WarningLabel {
    color: #F59E0B;
    font-size: 12px;
    background: transparent;
}

QLabel#ErrorLabel {
    color: #F87171;
    font-size: 12px;
    background: transparent;
}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #0F172A;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 7px 10px;
    color: #F1F5F9;
    selection-background-color: #2563EB;
}

QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #3B82F6;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox QAbstractItemView {
    background-color: #1E293B;
    border: 1px solid #334155;
    selection-background-color: #2563EB;
    color: #F1F5F9;
}

QPushButton {
    background-color: #2563EB;
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #1D4ED8;
}

QPushButton:disabled {
    background-color: #334155;
    color: #64748B;
}

QPushButton#SecondaryButton {
    background-color: #1E293B;
    border: 1px solid #334155;
    color: #E2E8F0;
}

QPushButton#SecondaryButton:hover {
    background-color: #334155;
}

QPushButton#DangerButton {
    background-color: #DC2626;
    color: #FFFFFF;
}

QPushButton#DangerButton:hover {
    background-color: #B91C1C;
}

QPushButton#DangerButton:disabled {
    background-color: #334155;
    color: #64748B;
}

QPushButton#SmallButton {
    background-color: #334155;
    color: #E2E8F0;
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
}

QPushButton#SmallButton:hover {
    background-color: #475569;
}

QTableWidget {
    background-color: #0F172A;
    alternate-background-color: #162033;
    border: 1px solid #334155;
    border-radius: 8px;
    gridline-color: #1E293B;
    color: #E2E8F0;
    selection-background-color: #1E3A8A;
}

QHeaderView::section {
    background-color: #1E293B;
    color: #94A3B8;
    font-weight: 600;
    font-size: 12px;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #334155;
}

QProgressBar {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 6px;
    text-align: center;
    color: #F8FAFC;
    height: 18px;
    font-size: 11px;
}

QProgressBar::chunk {
    background-color: #2563EB;
    border-radius: 5px;
}

QCheckBox {
    spacing: 8px;
    background: transparent;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #475569;
    background-color: #0F172A;
}

QCheckBox::indicator:checked {
    background-color: #2563EB;
    border-color: #2563EB;
}

QPlainTextEdit#LogConsole {
    background-color: #090E1A;
    border: 1px solid #1E293B;
    border-radius: 6px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 11px;
    color: #CBD5E1;
}

QScrollBar:vertical {
    background: #0F172A;
    width: 10px;
    border-radius: 5px;
}

QScrollBar::handle:vertical {
    background: #334155;
    border-radius: 5px;
    min-height: 24px;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""
