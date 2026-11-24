"""Comprehensive offline unit tests for Phase 2 input normalization and platform detection."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.models import InputValidationStatus, Platform
from core.normalizer import detect_platform, normalize_batch, normalize_input
from extractors import (
    FacebookExtractor,
    InstagramExtractor,
    TikTokExtractor,
    XExtractor,
    YouTubeExtractor,
)
from gui.main_window import MainWindow


# ---------------------------------------------------------------------------
# 1. Platform detection & URL aliases (www, non-www, mobile, scheme variants)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected_platform", "expected_username", "expected_url", "expected_direct_id"),
    [
        # Instagram
        ("https://www.instagram.com/natgeo/", Platform.INSTAGRAM, "natgeo", "https://www.instagram.com/natgeo", None),
        ("http://instagram.com/NatGeo?igshid=YmMyMTA2M2Y=", Platform.INSTAGRAM, "NatGeo", "https://www.instagram.com/NatGeo", None),
        ("m.instagram.com/john.doe/reels/?utm_source=ig", Platform.INSTAGRAM, "john.doe", "https://www.instagram.com/john.doe", None),
        ("https://instagr.am/nasa///", Platform.INSTAGRAM, "nasa", "https://www.instagram.com/nasa", None),
        # TikTok
        ("https://www.tiktok.com/@charlidamelio?is_from_webapp=1&sender_device=pc", Platform.TIKTOK, "charlidamelio", "https://www.tiktok.com/@charlidamelio", None),
        ("tiktok.com/@khaby.lame/", Platform.TIKTOK, "khaby.lame", "https://www.tiktok.com/@khaby.lame", None),
        ("https://m.tiktok.com/@tiktok/live?lang=en", Platform.TIKTOK, "tiktok", "https://www.tiktok.com/@tiktok", None),
        ("https://www.tiktok.com/%40encoded_user/", Platform.TIKTOK, "encoded_user", "https://www.tiktok.com/@encoded_user", None),
        # X (Twitter)
        ("https://x.com/elonmusk", Platform.X, "elonmusk", "https://x.com/elonmusk", None),
        ("http://www.twitter.com/Jack/?s=20&t=abc", Platform.X, "Jack", "https://x.com/Jack", None),
        ("mobile.twitter.com/OpenAI/media", Platform.X, "OpenAI", "https://x.com/OpenAI", None),
        ("https://m.x.com/NASA/", Platform.X, "NASA", "https://x.com/NASA", None),
        # Facebook
        ("https://www.facebook.com/zuck/?ref=share&fbclid=IwAR123", Platform.FACEBOOK, "zuck", "https://www.facebook.com/zuck", None),
        ("m.facebook.com/Meta/about/", Platform.FACEBOOK, "Meta", "https://www.facebook.com/Meta", None),
        ("https://mbasic.facebook.com/profile.php?id=100012345678901&mibextid=ZbWKwL", Platform.FACEBOOK, "", "https://www.facebook.com/profile.php?id=100012345678901", "100012345678901"),
        ("https://fb.com/nasa/", Platform.FACEBOOK, "nasa", "https://www.facebook.com/nasa", None),
        ("https://www.facebook.com/people/Jane-Doe/100098765432100/", Platform.FACEBOOK, "", "https://www.facebook.com/profile.php?id=100098765432100", "100098765432100"),
        # YouTube (handle, channel ID, legacy /c and /user)
        ("https://www.youtube.com/@Google/videos?si=tracking123", Platform.YOUTUBE, "@Google", "https://www.youtube.com/@Google", None),
        ("youtube.com/@MrBeast/", Platform.YOUTUBE, "@MrBeast", "https://www.youtube.com/@MrBeast", None),
        ("https://m.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw/featured?app=m", Platform.YOUTUBE, "", "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", "UC_x5XG1OV2P6uZZ5FSM9Ttw"),
        ("https://music.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", Platform.YOUTUBE, "", "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", "UC_x5XG1OV2P6uZZ5FSM9Ttw"),
        ("https://www.youtube.com/c/GoogleDevelopers/videos", Platform.YOUTUBE, "GoogleDevelopers", "https://www.youtube.com/c/GoogleDevelopers", None),
        ("https://www.youtube.com/user/Google/", Platform.YOUTUBE, "Google", "https://www.youtube.com/user/Google", None),
    ],
)
def test_valid_profile_urls(
    raw: str,
    expected_platform: Platform,
    expected_username: str,
    expected_url: str,
    expected_direct_id: str | None,
) -> None:
    assert detect_platform(raw) == expected_platform
    res = normalize_input(raw)
    assert res.validation_status == InputValidationStatus.VALID
    assert res.is_valid is True
    assert res.platform == expected_platform
    assert res.username == expected_username
    assert res.profile_url == expected_url
    assert res.direct_id == expected_direct_id
    assert res.raw_input == raw


# ---------------------------------------------------------------------------
# 2. Username inputs with explicit platform selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "platform", "expected_username", "expected_url", "expected_direct_id"),
    [
        ("  @NatGeo  ", Platform.INSTAGRAM, "NatGeo", "https://www.instagram.com/NatGeo", None),
        ("john.doe", Platform.INSTAGRAM, "john.doe", "https://www.instagram.com/john.doe", None),
        ("@khaby.lame", Platform.TIKTOK, "khaby.lame", "https://www.tiktok.com/@khaby.lame", None),
        ("tiktok", Platform.TIKTOK, "tiktok", "https://www.tiktok.com/@tiktok", None),
        ("@OpenAI", Platform.X, "OpenAI", "https://x.com/OpenAI", None),
        ("jack", Platform.X, "jack", "https://x.com/jack", None),
        ("mark.zuckerberg", Platform.FACEBOOK, "mark.zuckerberg", "https://www.facebook.com/mark.zuckerberg", None),
        ("@Google", Platform.YOUTUBE, "@Google", "https://www.youtube.com/@Google", None),
        ("Google", Platform.YOUTUBE, "@Google", "https://www.youtube.com/@Google", None),
        ("UC_x5XG1OV2P6uZZ5FSM9Ttw", Platform.YOUTUBE, "", "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", "UC_x5XG1OV2P6uZZ5FSM9Ttw"),
    ],
)
def test_valid_usernames_with_platform(
    raw: str,
    platform: Platform,
    expected_username: str,
    expected_url: str,
    expected_direct_id: str | None,
) -> None:
    res = normalize_input(raw, selected_platform=platform)
    assert res.validation_status == InputValidationStatus.VALID
    assert res.is_valid is True
    assert res.platform == platform
    assert res.username == expected_username
    assert res.profile_url == expected_url
    assert res.direct_id == expected_direct_id
    assert res.raw_input == raw


# ---------------------------------------------------------------------------
# 3. Ambiguous bare usernames (MUST NOT guess platform)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["elonmusk", "@Google", "john.doe", "UC_x5XG1OV2P6uZZ5FSM9Ttw", "  natgeo  "])
def test_bare_username_without_platform_is_ambiguous(raw: str) -> None:
    assert detect_platform(raw) == Platform.UNKNOWN
    res = normalize_input(raw, selected_platform=None)
    assert res.validation_status == InputValidationStatus.AMBIGUOUS_INPUT
    assert res.is_valid is False
    assert res.platform == Platform.UNKNOWN
    assert res.error_message


# ---------------------------------------------------------------------------
# 4. Unsupported domains (never match arbitrary or spoofed domains)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/user",
        "https://reddit.com/u/spez",
        "https://linkedin.com/in/someone",
        "https://fake-instagram.com/natgeo",
        "https://instagram.com.evil.net/natgeo",
        "https://notyoutube.com/@Google",
        "https://myx.com/elonmusk",
        "example.com/profile",
        "www.example.org",
    ],
)
def test_unsupported_domains(raw: str) -> None:
    assert detect_platform(raw) == Platform.UNKNOWN
    res = normalize_input(raw)
    assert res.validation_status == InputValidationStatus.UNSUPPORTED_PLATFORM
    assert res.is_valid is False
    assert res.platform == Platform.UNKNOWN
    assert res.error_message


# ---------------------------------------------------------------------------
# 5. Reserved paths must not be treated as usernames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/settings/",
        "https://www.instagram.com/login",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/p/Cx123456/",
        "https://www.instagram.com/reel/Cx123456/",
        "https://www.tiktok.com/explore",
        "https://www.tiktok.com/search?q=music",
        "https://www.tiktok.com/login",
        "https://x.com/home",
        "https://x.com/explore",
        "https://x.com/search?q=python",
        "https://x.com/settings/profile",
        "https://x.com/i/flow/login",
        "https://www.facebook.com/watch/?v=123",
        "https://www.facebook.com/share/p/12345/",
        "https://www.facebook.com/login.php",
        "https://www.facebook.com/marketplace",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/explore",
        "https://www.youtube.com/results?search_query=test",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/share",
    ],
)
def test_reserved_url_paths_rejected(raw: str) -> None:
    res = normalize_input(raw)
    assert res.validation_status == InputValidationStatus.INVALID
    assert res.is_valid is False
    assert res.error_message


@pytest.mark.parametrize(
    ("raw", "platform"),
    [
        ("settings", Platform.INSTAGRAM),
        ("login", Platform.X),
        ("watch", Platform.YOUTUBE),
        ("explore", Platform.TIKTOK),
        ("share", Platform.FACEBOOK),
    ],
)
def test_reserved_plain_usernames_rejected(raw: str, platform: Platform) -> None:
    res = normalize_input(raw, selected_platform=platform)
    assert res.validation_status == InputValidationStatus.INVALID
    assert res.is_valid is False


# ---------------------------------------------------------------------------
# 6. Invalid URLs, empty/whitespace, Unicode & special characters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "platform"),
    [
        ("", None),
        ("   \t\n  ", None),
        ("\u200b\ufeff  ", Platform.INSTAGRAM),
        ("http://", None),
        ("https:///onlypath", None),
        ("ftp://instagram.com/natgeo", None),
        ("javascript:alert(1)", None),
        ("https://www.instagram.com/", None),
        ("https://www.tiktok.com/no_at_sign", None),
        ("https://vm.tiktok.com/ZMj123456/", None),
        ("https://youtu.be/dQw4w9WgXcQ", None),
        ("https://www.youtube.com/channel/NOT_A_VALID_ID", None),
        ("https://www.youtube.com/channel/", None),
        ("https://www.facebook.com/profile.php?id=notnumeric", None),
        ("https://www.facebook.com/profile.php", None),
        ("https://x.com/elonmusk/status/1234567890", None),
        ("https://x.com/has space", None),
        ("user name", Platform.X),
        ("user/extra", Platform.INSTAGRAM),
        ("user🔥", Platform.INSTAGRAM),
        ("user🔥", None),
        ("пользователь", Platform.TIKTOK),
        ("user<script>", Platform.X),
        ("user$invalid!", Platform.FACEBOOK),
        ("..bad.dots..", Platform.INSTAGRAM),
        ("toolongusername_for_x_platform", Platform.X),
    ],
)
def test_invalid_inputs(raw: str, platform: Platform | None) -> None:
    res = normalize_input(raw, selected_platform=platform)
    assert res.validation_status == InputValidationStatus.INVALID
    assert res.is_valid is False
    assert res.error_message


# ---------------------------------------------------------------------------
# 7. YouTube handle vs channel ID distinction & case preservation
# ---------------------------------------------------------------------------

def test_youtube_handle_vs_channel_id_case_preservation() -> None:
    h = normalize_input("https://www.youtube.com/@GoogleDevelopers/videos")
    c1 = normalize_input("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw")
    c2 = normalize_input("https://www.youtube.com/channel/UC_x5xg1ov2p6uzz5fsm9ttw")

    assert h.is_valid and h.username == "@GoogleDevelopers" and h.direct_id is None
    assert c1.is_valid and c1.direct_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw" and c1.username == ""
    assert c2.is_valid and c2.direct_id == "UC_x5xg1ov2p6uzz5fsm9ttw"
    # Channel IDs are case-sensitive -> distinct canonical keys
    assert c1.canonical_key != c2.canonical_key
    assert h.canonical_key != c1.canonical_key


# ---------------------------------------------------------------------------
# 8. Duplicate normalized inputs in batch normalization
# ---------------------------------------------------------------------------

def test_duplicate_normalized_inputs_deduplicated() -> None:
    entries = [
        "https://twitter.com/ElonMusk?s=20",
        "http://www.x.com/elonmusk/",
        "https://mobile.x.com/elonmusk/media",
        "https://m.instagram.com/NatGeo/?igshid=123",
        "instagram.com/natgeo/",
        "https://www.youtube.com/@Google/videos",
        "https://youtube.com/@google?si=xyz",
        "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw",
        "https://m.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw/about",
    ]
    unique, dups = normalize_batch(entries)
    assert len(unique) == 4
    assert dups == 5
    assert [u.platform for u in unique] == [
        Platform.X,
        Platform.INSTAGRAM,
        Platform.YOUTUBE,
        Platform.YOUTUBE,
    ]


# ---------------------------------------------------------------------------
# 9. Platform adapter interface consistency
# ---------------------------------------------------------------------------

def test_platform_adapters_detect_and_normalize() -> None:
    cases = [
        (InstagramExtractor(), "https://instagram.com/natgeo/", "https://www.instagram.com/natgeo"),
        (TikTokExtractor(), "https://m.tiktok.com/@tiktok", "https://www.tiktok.com/@tiktok"),
        (XExtractor(), "https://twitter.com/jack", "https://x.com/jack"),
        (FacebookExtractor(), "https://fb.com/zuck", "https://www.facebook.com/zuck"),
        (YouTubeExtractor(), "https://youtube.com/@Google/videos", "https://www.youtube.com/@Google"),
    ]
    for adapter, url, expected in cases:
        assert adapter.detect_input(url) is True
        assert adapter.detect_input("https://example.com/user") is False
        norm = adapter.normalize_input(url)
        assert norm.is_valid is True
        assert norm.profile_url == expected


# ---------------------------------------------------------------------------
# 10. GUI integration (Single Search & Batch Search offline normalization)
# ---------------------------------------------------------------------------

def test_gui_single_and_batch_normalization(tmp_path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None
    win = MainWindow(db_path=tmp_path / "p2.db")

    # Single Search live hint: ambiguous bare username vs auto-detected URL
    win._single_page._input_edit.setText("elonmusk")
    assert "AMBIGUOUS_INPUT" in win._single_page._detect_hint.text()

    win._single_page._input_edit.setText("https://twitter.com/elonmusk?s=20")
    assert "VALID" in win._single_page._detect_hint.text()
    assert "https://x.com/elonmusk" in win._single_page._detect_hint.text()

    # Batch Search normalized duplicate detection across mixed URLs
    win._batch_page._input_area.setPlainText(
        "\n".join(
            [
                "https://twitter.com/jack?s=20",
                "https://x.com/Jack/",
                "https://www.youtube.com/@Google/videos",
                "https://example.com/unsupported",
            ]
        )
    )
    unique, dups = win._batch_page.parse_entries()
    assert len(unique) == 3
    assert dups == 1
    assert win._batch_page._btn_start.isEnabled()

    win.close()
