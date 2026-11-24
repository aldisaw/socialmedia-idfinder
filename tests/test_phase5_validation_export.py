"""Comprehensive offline tests for Phase 5 validation, confidence scoring, export hardening, and Results GUI."""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

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
from core.validator import find_duplicate_user_ids, is_valid_id_format, validate_and_score
from exports.exporter import EXPORT_FIELDS, export_results
from gui.pages.results_page import ResultsPage


# ---------------------------------------------------------------------------
# 1. Platform-specific ID format & ID-type validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("platform", "uid", "id_type", "expected"),
    [
        # Valid across all 5 platforms
        (Platform.INSTAGRAM.value, "25025320", IdType.NUMERIC_USER_ID.value, True),
        (Platform.TIKTOK.value, "107955", IdType.NUMERIC_USER_ID.value, True),
        (Platform.TIKTOK.value, "MS4wLjABAAAAv7iSuuXDJGDvJkmH_vz124h", IdType.SEC_UID.value, True),
        (Platform.X.value, "12", IdType.NUMERIC_USER_ID.value, True),
        (Platform.FACEBOOK.value, "100012345678901", IdType.NUMERIC_PROFILE_ID.value, True),
        (Platform.YOUTUBE.value, "UCK8sQmJBp8GCxrOtXWBpyEA", IdType.CHANNEL_ID.value, True),
        # Invalid / malformed / leading zero / whitespace / wrong case
        (Platform.INSTAGRAM.value, "", IdType.NUMERIC_USER_ID.value, False),
        (Platform.INSTAGRAM.value, "000123", IdType.NUMERIC_USER_ID.value, False),
        (Platform.INSTAGRAM.value, "not_digits", IdType.NUMERIC_USER_ID.value, False),
        (Platform.INSTAGRAM.value, " 25025320 ", IdType.NUMERIC_USER_ID.value, False),
        (Platform.YOUTUBE.value, "ucK8sQmJBp8GCxrOtXWBpyEA", IdType.CHANNEL_ID.value, False),  # lowercase uc rejected
        (Platform.YOUTUBE.value, "UCshort", IdType.CHANNEL_ID.value, False),
        (Platform.YOUTUBE.value, "PL1234567890123456789012", IdType.CHANNEL_ID.value, False),  # playlist ID rejected
        (Platform.FACEBOOK.value, "00123", IdType.NUMERIC_PROFILE_ID.value, False),  # leading zero rejected
        # Wrong ID types for platform / numeric misclassified as SEC_UID
        (Platform.TIKTOK.value, "1234567890123456789012", IdType.SEC_UID.value, False),
        (Platform.TIKTOK.value, "MS4wLjABAAAAv7iSuuXDJGDvJkmH_vz124h", IdType.NUMERIC_USER_ID.value, False),
        (Platform.INSTAGRAM.value, "UCK8sQmJBp8GCxrOtXWBpyEA", IdType.CHANNEL_ID.value, False),
        (Platform.X.value, "100012345678901", IdType.NUMERIC_PROFILE_ID.value, False),
        (Platform.FACEBOOK.value, "100012345678901", IdType.NUMERIC_USER_ID.value, False),
        (Platform.YOUTUBE.value, "123456789", IdType.NUMERIC_USER_ID.value, False),
    ],
)
def test_platform_id_format_and_type_validation(
    platform: str, uid: str, id_type: str, expected: bool
) -> None:
    assert is_valid_id_format(uid, id_type, platform) is expected


# ---------------------------------------------------------------------------
# 2. Confidence evidence rules, mismatch, conflicts, regex cap, missing source
# ---------------------------------------------------------------------------

def test_confidence_scoring_evidence_rules() -> None:
    norm = normalize_input("https://www.youtube.com/@Google")

    # HIGH: verified structured evidence + valid format + source present
    high = validate_and_score(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input=norm.raw_input,
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="ytInitialData.metadata.channelMetadataRenderer.externalId",
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert high.confidence == Confidence.HIGH.value

    # MEDIUM: structured evidence but partially verified
    med = validate_and_score(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input=norm.raw_input,
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.STRUCTURED_METADATA.value,
            extraction_source="meta[property='og:url']",
            validation_status=ResultValidationStatus.PARTIALLY_VERIFIED.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert med.confidence == Confidence.MEDIUM.value

    # LOW: regex fallback MUST NEVER receive HIGH even if caller marked VERIFIED
    reg = validate_and_score(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input=norm.raw_input,
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.REGEX_FALLBACK.value,
            extraction_source="regex:externalId",
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert reg.confidence == Confidence.LOW.value
    assert reg.validation_status == ResultValidationStatus.PARTIALLY_VERIFIED.value

    # NOT_VERIFIED: missing extraction_source cannot be treated as validated ownership
    no_src = validate_and_score(
        SearchResult(
            platform=Platform.YOUTUBE.value,
            input=norm.raw_input,
            user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
            id_type=IdType.CHANNEL_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="",
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert no_src.confidence == Confidence.NOT_VERIFIED.value
    assert no_src.validation_status == ResultValidationStatus.UNVERIFIED.value


def test_identity_mismatch_and_conflicting_ids_rejected() -> None:
    norm = normalize_input("https://x.com/jack")

    mismatch = validate_and_score(
        SearchResult(
            platform=Platform.X.value,
            input=norm.raw_input,
            user_id="44196397",
            id_type=IdType.NUMERIC_USER_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="json.user",
            validation_status=ResultValidationStatus.IDENTITY_MISMATCH.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert mismatch.status == ResultStatus.NOT_FOUND.value
    assert mismatch.user_id == ""
    assert mismatch.confidence == Confidence.NOT_VERIFIED.value

    conflict = validate_and_score(
        SearchResult(
            platform=Platform.X.value,
            input=norm.raw_input,
            user_id="12",
            id_type=IdType.NUMERIC_USER_ID.value,
            extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
            extraction_source="json.user",
            validation_status=ResultValidationStatus.CONFLICTING_IDS.value,
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert conflict.status == ResultStatus.NOT_FOUND.value
    assert conflict.user_id == ""
    assert conflict.confidence == Confidence.NOT_VERIFIED.value


# ---------------------------------------------------------------------------
# 3. Result status consistency across all statuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    [
        ResultStatus.NOT_FOUND.value,
        ResultStatus.BLOCKED.value,
        ResultStatus.ERROR.value,
        ResultStatus.TIMEOUT.value,
        ResultStatus.CANCELLED.value,
        ResultStatus.INVALID_INPUT.value,
    ],
)
def test_non_found_statuses_never_carry_id_or_high_confidence(status: str) -> None:
    norm = normalize_input("https://x.com/jack")
    res = validate_and_score(
        SearchResult(
            platform=Platform.X.value,
            input=norm.raw_input,
            user_id="12",  # stale/fabricated ID must be scrubbed
            id_type=IdType.NUMERIC_USER_ID.value,
            confidence=Confidence.HIGH.value,
            validation_status=ResultValidationStatus.VERIFIED.value,
            status=status,
        ),
        norm,
    )
    assert res.status == status
    assert res.user_id == ""
    assert res.id_type == IdType.UNKNOWN.value
    assert res.confidence == Confidence.NOT_VERIFIED.value


def test_found_without_id_downgraded_to_not_found() -> None:
    norm = normalize_input("https://x.com/jack")
    res = validate_and_score(
        SearchResult(
            platform=Platform.X.value,
            input=norm.raw_input,
            user_id="",
            status=ResultStatus.FOUND.value,
        ),
        norm,
    )
    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.confidence == Confidence.NOT_VERIFIED.value


def test_duplicate_ids_identified_without_merging_accounts() -> None:
    r1 = SearchResult(
        platform=Platform.X.value,
        input="https://x.com/old_handle",
        username="old_handle",
        user_id="12",
        status=ResultStatus.FOUND.value,
    )
    r2 = SearchResult(
        platform=Platform.X.value,
        input="https://x.com/jack",
        username="jack",
        user_id="12",
        status=ResultStatus.FOUND.value,
    )
    r3 = SearchResult(
        platform=Platform.YOUTUBE.value,
        input="https://www.youtube.com/@Google",
        username="@Google",
        user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
        status=ResultStatus.FOUND.value,
    )
    rows = [r1, r2, r3]
    dups = find_duplicate_user_ids(rows)
    assert dups == {(Platform.X.value, "12"): [0, 1]}
    assert len(rows) == 3  # accounts never merged


# ---------------------------------------------------------------------------
# 4. Export hardening: CSV escaping, JSON nulls, UTF-8, TXT, selected, redaction
# ---------------------------------------------------------------------------

def test_exports_csv_json_txt_utf8_nulls_escaping_and_redaction(tmp_path: Path) -> None:
    r_found = SearchResult(
        platform=Platform.YOUTUBE.value,
        input="https://www.youtube.com/@Café_日本語",
        username="@Café_日本語",
        profile_url="https://www.youtube.com/@Café_日本語",
        user_id="UCK8sQmJBp8GCxrOtXWBpyEA",
        id_type=IdType.CHANNEL_ID.value,
        extraction_method=ExtractionMethod.EMBEDDED_JSON.value,
        extraction_source='ytInitialData,"quoted",source',
        confidence=Confidence.HIGH.value,
        validation_status=ResultValidationStatus.VERIFIED.value,
        status=ResultStatus.FOUND.value,
        response_time=0.452,
        error_message='Note: "comma, newline\ncheck"',
    )
    r_missing_with_secret = SearchResult(
        platform=Platform.INSTAGRAM.value,
        input="https://www.instagram.com/private_user",
        username="private_user",
        profile_url="https://www.instagram.com/private_user",
        user_id="",
        id_type=IdType.UNKNOWN.value,
        extraction_method=ExtractionMethod.NONE.value,
        extraction_source="",
        confidence=Confidence.NOT_VERIFIED.value,
        validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
        status=ResultStatus.BLOCKED.value,
        response_time=0.31,
        error_message="Blocked Cookie=sessionid=topsecret123 Authorization: Bearer mySecretJwtToken",
    )

    # JSON export: null handling + UTF-8 + credential redaction
    json_path = export_results([r_found, r_missing_with_secret], tmp_path / "out.json", "json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert tuple(data[0].keys()) == EXPORT_FIELDS
    assert data[0]["username"] == "@Café_日本語"
    assert data[0]["user_id"] == "UCK8sQmJBp8GCxrOtXWBpyEA"
    assert data[1]["user_id"] is None  # missing identifier serialized as JSON null
    assert data[1]["extraction_source"] is None
    assert "topsecret123" not in data[1]["error_message"]
    assert "mySecretJwtToken" not in data[1]["error_message"]
    assert "[REDACTED]" in data[1]["error_message"]

    # CSV export: proper escaping of commas, quotes, newlines, UTF-8 + redaction
    csv_path = export_results([r_found, r_missing_with_secret], tmp_path / "out.csv", "csv")
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert tuple(rows[0].keys()) == EXPORT_FIELDS
    assert rows[0]["username"] == "'@Café_日本語"  # H-03: leading '@' neutralized in CSV only
    assert rows[0]["extraction_source"] == 'ytInitialData,"quoted",source'
    assert rows[0]["error_message"] == 'Note: "comma, newline\ncheck"'
    assert rows[1]["user_id"] == ""
    assert "topsecret123" not in rows[1]["error_message"]
    assert "mySecretJwtToken" not in rows[1]["error_message"]

    # TXT export: preserves all 14 fields + UTF-8 + redaction
    txt_path = export_results([r_found, r_missing_with_secret], tmp_path / "out.txt", "txt")
    txt = txt_path.read_text(encoding="utf-8")
    for required_token in (
        "platform=youtube",
        "username=@Café_日本語",
        "user_id=UCK8sQmJBp8GCxrOtXWBpyEA",
        "id_type=CHANNEL_ID",
        "validation_status=VERIFIED",
        "created_at=",
        "user_id=null",
        "[REDACTED]",
    ):
        assert required_token in txt
    assert "topsecret123" not in txt
    assert "mySecretJwtToken" not in txt

    # Selected-indices export
    sel_path = export_results(
        [r_found, r_missing_with_secret],
        tmp_path / "selected.json",
        "json",
        selected_indices=[0],
    )
    sel_data = json.loads(sel_path.read_text(encoding="utf-8"))
    assert len(sel_data) == 1
    assert sel_data[0]["user_id"] == "UCK8sQmJBp8GCxrOtXWBpyEA"


# ---------------------------------------------------------------------------
# 5. ResultsPage filtering, sorting, Copy Selected IDs, filtered & selected export
# ---------------------------------------------------------------------------

def test_results_page_filtering_sorting_copy_and_export(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    page = ResultsPage()
    results = [
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
            response_time=0.8,
        ),
        SearchResult(
            platform=Platform.X.value,
            input="https://x.com/jack",
            username="jack",
            user_id="12",
            id_type=IdType.NUMERIC_USER_ID.value,
            extraction_method=ExtractionMethod.REGEX_FALLBACK.value,
            extraction_source="regex:rest_id",
            confidence=Confidence.LOW.value,
            validation_status=ResultValidationStatus.PARTIALLY_VERIFIED.value,
            status=ResultStatus.FOUND.value,
            response_time=0.2,
        ),
        SearchResult(
            platform=Platform.INSTAGRAM.value,
            input="https://www.instagram.com/locked",
            username="locked",
            status=ResultStatus.BLOCKED.value,
            confidence=Confidence.NOT_VERIFIED.value,
            response_time=1.5,
        ),
    ]
    page.set_results(results)
    assert len(page.filtered_results()) == 3

    # Filter: High Confidence
    idx_high = page._filter_combo.findData("conf:HIGH")
    page._filter_combo.setCurrentIndex(idx_high)
    assert len(page.filtered_results()) == 1
    assert page.filtered_results()[0].user_id == "UCK8sQmJBp8GCxrOtXWBpyEA"

    # Export filtered matches displayed results
    filtered_out = export_results(page.get_export_target_results("filtered"), tmp_path / "filt.json", "json")
    assert len(json.loads(filtered_out.read_text(encoding="utf-8"))) == 1

    # Filter: Blocked
    page._filter_combo.setCurrentIndex(page._filter_combo.findData("status:BLOCKED"))
    assert len(page.filtered_results()) == 1
    assert page.filtered_results()[0].platform == Platform.INSTAGRAM.value

    # Reset to All & sort by response time ascending
    page._filter_combo.setCurrentIndex(page._filter_combo.findData("all"))
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("time_asc"))
    assert [r.response_time for r in page.filtered_results()] == [0.2, 0.8, 1.5]

    # Sort by platform (A-Z): instagram, x, youtube
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("platform"))
    assert [r.platform for r in page.filtered_results()] == ["instagram", "x", "youtube"]

    # Sort by confidence (HIGH -> LOW -> NOT_VERIFIED)
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("confidence"))
    assert [r.confidence for r in page.filtered_results()] == ["HIGH", "LOW", "NOT_VERIFIED"]

    # Select first two rows and Copy Selected IDs + Export Selected
    sel_model = page._table.selectionModel()
    assert sel_model is not None
    sel_model.select(
        page._table.model().index(0, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    sel_model.select(
        page._table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    copied = page.copy_selected_ids()
    assert copied == ["UCK8sQmJBp8GCxrOtXWBpyEA", "12"]

    selected_rows = page.get_export_target_results("selected")
    assert len(selected_rows) == 2
    assert [r.user_id for r in selected_rows] == ["UCK8sQmJBp8GCxrOtXWBpyEA", "12"]

    page.close()
