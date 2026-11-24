"""Result validation, evidence-based confidence scoring, and duplicate ID detection."""

from __future__ import annotations

import re

from core.models import (
    Confidence,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)

_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    IdType.NUMERIC_USER_ID.value: re.compile(r"^[1-9]\d{0,24}$"),
    IdType.NUMERIC_PROFILE_ID.value: re.compile(r"^[1-9]\d{0,24}$"),
    IdType.CHANNEL_ID.value: re.compile(r"^UC[A-Za-z0-9_-]{22}$"),
    IdType.SEC_UID.value: re.compile(r"^[A-Za-z0-9_-]{20,140}$"),
}

_ALLOWED_PLATFORM_ID_TYPES: dict[str, frozenset[str]] = {
    Platform.INSTAGRAM.value: frozenset({IdType.NUMERIC_USER_ID.value}),
    Platform.TIKTOK.value: frozenset({IdType.NUMERIC_USER_ID.value, IdType.SEC_UID.value}),
    Platform.X.value: frozenset({IdType.NUMERIC_USER_ID.value}),
    Platform.FACEBOOK.value: frozenset({IdType.NUMERIC_PROFILE_ID.value}),
    Platform.YOUTUBE.value: frozenset({IdType.CHANNEL_ID.value}),
}

_STRUCTURED_METHODS: frozenset[str] = frozenset(
    {
        ExtractionMethod.EMBEDDED_JSON.value,
        ExtractionMethod.HTML_JSON_EXTRACTED.value,
        ExtractionMethod.BROWSER_EXTRACTED.value,
        ExtractionMethod.STRUCTURED_METADATA.value,
        ExtractionMethod.HTML_METADATA.value,
    }
)


def is_valid_id_format(user_id: str, id_type: str, platform: str = "") -> bool:
    """Validate identifier syntax and ensure it is legal for the given platform.

    Distinguishes TikTok SEC_UID (non-numeric) from NUMERIC_USER_ID strictly.
    """
    if not user_id or not isinstance(user_id, str):
        return False
    if user_id != user_id.strip():
        return False
    pattern = _ID_PATTERNS.get(id_type)
    if pattern is None or not pattern.match(user_id):
        return False
    if id_type == IdType.SEC_UID.value and user_id.isdigit():
        return False
    if platform and platform in _ALLOWED_PLATFORM_ID_TYPES:
        if id_type not in _ALLOWED_PLATFORM_ID_TYPES[platform]:
            return False
    return True


def _reject_result(
    result: SearchResult,
    *,
    status: ResultStatus = ResultStatus.NOT_FOUND,
    val_status: ResultValidationStatus = ResultValidationStatus.UNVERIFIED,
    reason: str = "",
) -> SearchResult:
    result.user_id = ""
    result.id_type = IdType.UNKNOWN.value
    result.status = status.value
    result.confidence = Confidence.NOT_VERIFIED.value
    result.validation_status = val_status.value
    if reason and not result.error_message:
        result.error_message = reason
    return result


def validate_and_score(result: SearchResult, normalized: NormalizedInput) -> SearchResult:
    """Enforce strict ID validation, status consistency, and evidence-backed confidence rules.

    Rules:
    - FOUND requires a valid, format-checked, non-conflicting identifier with evidence.
    - Regex-only extraction never receives HIGH confidence (capped at LOW).
    - Missing extraction source/evidence cannot be treated as verified HIGH confidence.
    - Format validity alone never grants HIGH confidence without verified target ownership.
    - Non-FOUND statuses never carry unverified HIGH confidence or fabricated IDs.
    """
    # 1. Invalid normalized input must always produce INVALID_INPUT / NOT_VERIFIED
    if not normalized.is_valid:
        return _reject_result(
            result,
            status=ResultStatus.INVALID_INPUT,
            val_status=ResultValidationStatus.INVALID_INPUT,
            reason=normalized.error_message or "Invalid input.",
        )

    # 2. Non-FOUND terminal statuses must remain consistent and never carry fabricated IDs
    if result.status != ResultStatus.FOUND.value:
        result.user_id = ""
        result.id_type = IdType.UNKNOWN.value
        result.confidence = Confidence.NOT_VERIFIED.value
        if result.validation_status == ResultValidationStatus.VERIFIED.value:
            result.validation_status = ResultValidationStatus.UNVERIFIED.value
        return result

    # 3. FOUND requires a non-empty identifier
    uid = (result.user_id or "").strip()
    if not uid:
        return _reject_result(
            result,
            status=ResultStatus.NOT_FOUND,
            val_status=ResultValidationStatus.NOT_AVAILABLE,
            reason="No identifier present for FOUND status.",
        )
    result.user_id = uid

    # 4. Reject identity mismatches and conflicting IDs completely
    if result.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value:
        return _reject_result(
            result,
            status=ResultStatus.NOT_FOUND,
            val_status=ResultValidationStatus.IDENTITY_MISMATCH,
            reason="Extracted identifier did not match target profile identity.",
        )

    if result.validation_status == ResultValidationStatus.CONFLICTING_IDS.value:
        return _reject_result(
            result,
            status=ResultStatus.NOT_FOUND,
            val_status=ResultValidationStatus.CONFLICTING_IDS,
            reason="Conflicting identifiers detected for target profile.",
        )

    # 5. Platform-specific format & ID-type enforcement
    if not is_valid_id_format(result.user_id, result.id_type, result.platform):
        return _reject_result(
            result,
            status=ResultStatus.NOT_FOUND,
            val_status=ResultValidationStatus.UNVERIFIED,
            reason="Extracted candidate failed platform identifier format validation.",
        )

    method = result.extraction_method
    has_source = bool((result.extraction_source or "").strip())

    # 6. Missing evidence / source cannot be treated as validated ownership
    if method == ExtractionMethod.NONE.value or not has_source:
        result.confidence = Confidence.NOT_VERIFIED.value
        result.validation_status = ResultValidationStatus.UNVERIFIED.value
        return result

    # 7. Regex fallback can NEVER receive HIGH confidence (capped at LOW)
    if method == ExtractionMethod.REGEX_FALLBACK.value:
        result.confidence = Confidence.LOW.value
        if result.validation_status == ResultValidationStatus.VERIFIED.value:
            result.validation_status = ResultValidationStatus.PARTIALLY_VERIFIED.value
        return result

    # 8. URL-extracted IDs must NEVER be claimed as page-verified (HIGH); capped at MEDIUM / PARTIALLY_VERIFIED
    if method == ExtractionMethod.URL_EXTRACTED.value:
        result.confidence = Confidence.MEDIUM.value
        result.validation_status = ResultValidationStatus.PARTIALLY_VERIFIED.value
        return result

    # 9. Direct URL extraction: HIGH only if explicitly verified, else MEDIUM
    if method == ExtractionMethod.DIRECT_URL.value:
        if result.validation_status == ResultValidationStatus.VERIFIED.value:
            result.confidence = Confidence.HIGH.value
        elif result.validation_status == ResultValidationStatus.PARTIALLY_VERIFIED.value:
            result.confidence = Confidence.MEDIUM.value
        else:
            result.confidence = Confidence.NOT_VERIFIED.value
        return result

    # 9. Structured extraction (EMBEDDED_JSON / STRUCTURED_METADATA / HTML_METADATA)
    if result.validation_status == ResultValidationStatus.VERIFIED.value:
        if method in _STRUCTURED_METHODS:
            result.confidence = Confidence.HIGH.value
        else:
            result.confidence = Confidence.MEDIUM.value
    elif result.validation_status == ResultValidationStatus.PARTIALLY_VERIFIED.value:
        result.confidence = Confidence.MEDIUM.value
    else:
        result.confidence = Confidence.NOT_VERIFIED.value

    return result


def find_duplicate_user_ids(results: list[SearchResult]) -> dict[tuple[str, str], list[int]]:
    """Identify duplicate (platform, user_id) pairs without merging distinct account rows.

    Returns mapping of (platform, user_id) -> list of row indices where count > 1.
    """
    buckets: dict[tuple[str, str], list[int]] = {}
    for idx, res in enumerate(results):
        if res.status == ResultStatus.FOUND.value and res.user_id:
            key = (res.platform, res.user_id)
            buckets.setdefault(key, []).append(idx)
    return {k: idxs for k, idxs in buckets.items() if len(idxs) > 1}
