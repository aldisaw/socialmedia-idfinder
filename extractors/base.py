"""Abstract base interface and shared multi-strategy pipeline for platform extractors."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Callable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from core.http_client import FetchResponse, HttpClient
from core.models import (
    AcquisitionMethod,
    Confidence,
    ExtractionCandidate,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)
from core.validator import is_valid_id_format, validate_and_score

logger = logging.getLogger("social_id_finder.extractor")

ProgressCallback = Callable[[str], None]

_LOGIN_PATH_PREFIXES = (
    "/accounts/login",
    "/login/",
    "/login.php",
    "/i/flow/login",
    "/checkpoint/",
    "/auth/login",
    "/signin",
)
_LOGIN_HTML_PHRASES = (
    "you must log in to continue",
    "log in to facebook",
    "log in to instagram",
    "log in to tiktok",
    "sign in to x",
    "log in to continue",
)
_LOGIN_TITLE_HINTS = ("log in", "login", "sign in")

_CAPTCHA_TITLE_HINTS = (
    "captcha",
    "attention required",
    "just a moment",
    "verify",
    "security check",
    "human verification",
)
_CAPTCHA_HTML_HINTS = (
    "captcha-verify",
    "verify you are human",
    "cf-challenge",
    "challenge-platform",
    "arkoselabs",
    "g-recaptcha",
    "tt-captcha",
    "tiktok-verify-page",
    "puzzle-captcha",
)


def extract_json_object_after(text: str, marker: str) -> dict | None:
    """Safely extract a balanced JSON object immediately following a JS assignment marker."""
    if marker.endswith("="):
        lhs = re.escape(marker[:-1].strip())
        m = re.search(rf"{lhs}\s*=\s*\{{", text)
        if not m:
            return None
        start = m.end() - 1
    else:
        idx = text.find(marker)
        if idx == -1:
            return None
        start = text.find("{", idx + len(marker))
        if start == -1:
            return None

    depth = 0
    in_string = False
    escape = False
    end = -1
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break

    if end == -1:
        return None
    try:
        obj = json.loads(text[start:end])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_script_json_by_id(soup: BeautifulSoup, script_id: str) -> dict | None:
    """Parse JSON inside <script id='...'> safely."""
    tag = soup.find("script", id=script_id)
    if not tag:
        return None
    raw = (tag.string or tag.get_text() or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def parse_json_ld_blocks(soup: BeautifulSoup) -> list[dict]:
    """Parse all valid <script type='application/ld+json'> objects."""
    blocks: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
        elif isinstance(parsed, list):
            blocks.extend(item for item in parsed if isinstance(item, dict))
    return blocks


def clean_handle(handle: str) -> str:
    """Normalize handle for case-insensitive identity comparison."""
    return (handle or "").strip().lstrip("@").lower()


def _url_is_login_route(url: str) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    path = (parsed.path or "").lower()
    if path == "/login":
        return True
    return any(path.startswith(prefix) or prefix in path for prefix in _LOGIN_PATH_PREFIXES)


def detect_access_restriction(
    *,
    final_url: str,
    html: str = "",
    redirect_chain: list[str] | None = None,
    status_code: int = 0,
) -> tuple[ResultStatus | None, str]:
    """Detect login redirects, login-required HTML, or CAPTCHA challenges before generic errors.

    Never misclassifies unrelated HTTP 400 errors as LOGIN_REQUIRED.
    """
    urls_to_check = list(redirect_chain or [])
    if final_url:
        urls_to_check.append(final_url)

    for u in urls_to_check:
        if _url_is_login_route(u):
            http_note = f" (HTTP {status_code})" if status_code else ""
            return (
                ResultStatus.LOGIN_REQUIRED,
                f"Platform redirected to login/authentication page{http_note}; authentication is not bypassed.",
            )

    if not html:
        return None, ""

    snippet = html[:12000].lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", snippet, re.S)
    title_text = title_match.group(1).strip() if title_match else ""

    # CAPTCHA / human verification interstitials
    if any(k in title_text for k in _CAPTCHA_TITLE_HINTS) or any(k in snippet for k in _CAPTCHA_HTML_HINTS):
        # Avoid false positives on large valid pages that only mention captcha inside JS bundle
        body_len = len(html)
        if body_len < 120_000 or any(k in title_text for k in _CAPTCHA_TITLE_HINTS):
            return (
                ResultStatus.CAPTCHA_REQUIRED,
                "Platform presented a CAPTCHA or human-verification challenge (not bypassed).",
            )

    # Explicit login-required HTML (including HTTP 200 or HTTP 400 login walls)
    if any(phrase in snippet for phrase in _LOGIN_HTML_PHRASES):
        return (
            ResultStatus.LOGIN_REQUIRED,
            f"Platform returned a login-required wall (HTTP {status_code or 200}); authentication is not bypassed.",
        )
    if title_text and any(k in title_text for k in _LOGIN_TITLE_HINTS):
        if any(marker in snippet for marker in ('type="password"', "type='password'", "name=\"pass\"", "name='pass'", "login_form")):
            return (
                ResultStatus.LOGIN_REQUIRED,
                f"Platform returned a login page (HTTP {status_code or 200}); authentication is not bypassed.",
            )

    return None, ""


class BaseExtractor(ABC):
    """Consistent contract and 4-priority strategy pipeline for all platform adapters."""

    platform: Platform = Platform.UNKNOWN

    @abstractmethod
    def detect_input(self, raw_input: str) -> bool:
        """Return True if raw_input matches this platform's URL format."""

    @abstractmethod
    def normalize_input(self, raw_input: str) -> NormalizedInput:
        """Canonicalize a username or URL for this platform."""

    # Ordered multi-strategy hooks (overridden per platform)
    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        return []

    def strategy_structured_metadata(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        return []

    def strategy_html_attributes(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        return []

    def strategy_regex_fallback(
        self,
        html: str,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        return []

    def fallback_direct_url_candidate(
        self,
        normalized: NormalizedInput,
        fetch: FetchResponse,
    ) -> ExtractionCandidate | None:
        """Optional direct-URL fallback when profile URL itself carried a canonical ID."""
        return None

    def detect_access_block(self, fetch: FetchResponse) -> tuple[ResultStatus | None, str]:
        return detect_access_restriction(
            final_url=fetch.url,
            html=fetch.text or "",
            redirect_chain=fetch.redirect_chain,
            status_code=fetch.status_code,
        )

    def _request_headers(self, normalized: NormalizedInput) -> dict[str, str] | None:
        return None

    def _error_result_from_fetch(
        self,
        normalized: NormalizedInput,
        fetch: FetchResponse,
        acquisition_method: AcquisitionMethod = AcquisitionMethod.HTTP,
    ) -> SearchResult:
        status_map = {
            "TIMEOUT": ResultStatus.TIMEOUT.value,
            "RATE_LIMITED": ResultStatus.RATE_LIMITED.value,
            "FORBIDDEN": ResultStatus.BLOCKED.value,
            "SSRF_BLOCKED": ResultStatus.BLOCKED.value,
            "NOT_FOUND": ResultStatus.NOT_FOUND.value,
        }
        status = status_map.get(fetch.error_type, ResultStatus.ERROR.value)
        return SearchResult(
            platform=self.platform.value,
            input=normalized.raw_input,
            username=normalized.username,
            profile_url=normalized.profile_url,
            user_id="",
            id_type=IdType.UNKNOWN.value,
            acquisition_method=acquisition_method.value,
            extraction_method=ExtractionMethod.NONE.value,
            extraction_source="",
            response_status=fetch.status_code,
            final_url=fetch.url or normalized.profile_url,
            confidence=Confidence.NOT_VERIFIED.value,
            validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
            status=status,
            error_message=fetch.error_message or f"Fetch failed ({fetch.error_type}).",
        )

    def _evaluate_candidates(
        self,
        candidates: list[ExtractionCandidate],
        normalized: NormalizedInput,
    ) -> tuple[ExtractionCandidate | None, ResultValidationStatus, str]:
        """Validate candidate list against target identity and detect conflicting IDs."""
        valid_format: list[ExtractionCandidate] = [
            c for c in candidates if is_valid_id_format(c.user_id, c.id_type.value, self.platform.value)
        ]
        if not valid_format:
            return None, ResultValidationStatus.UNVERIFIED, ""

        target_user = clean_handle(normalized.username)
        target_direct = (normalized.direct_id or "").strip()

        matched: list[ExtractionCandidate] = []
        mismatch_seen = False

        for cand in valid_format:
            if target_direct:
                if cand.user_id != target_direct:
                    mismatch_seen = True
                    continue
                cand.identity_confirmed = True

            cand_user = clean_handle(cand.matched_username)
            if target_user and cand_user:
                if cand_user != target_user:
                    mismatch_seen = True
                    continue
                cand.identity_confirmed = True

            matched.append(cand)

        if not matched:
            if mismatch_seen:
                return (
                    None,
                    ResultValidationStatus.IDENTITY_MISMATCH,
                    "Extracted identifier belonged to a different profile identity and was rejected.",
                )
            return None, ResultValidationStatus.UNVERIFIED, ""

        by_type: dict[IdType, set[str]] = {}
        for c in matched:
            by_type.setdefault(c.id_type, set()).add(c.user_id)

        primary = matched[0]
        for c in matched:
            if c.id_type in (IdType.NUMERIC_USER_ID, IdType.NUMERIC_PROFILE_ID, IdType.CHANNEL_ID):
                primary = c
                break

        distinct_same_type = by_type.get(primary.id_type, set())
        if len(distinct_same_type) > 1:
            return (
                None,
                ResultValidationStatus.CONFLICTING_IDS,
                f"Conflicting {primary.id_type.value} candidates found ({len(distinct_same_type)} distinct values); rejected to avoid false ID.",
            )

        if primary.identity_confirmed:
            return primary, ResultValidationStatus.VERIFIED, ""
        return primary, ResultValidationStatus.PARTIALLY_VERIFIED, ""

    def _build_found_result(
        self,
        normalized: NormalizedInput,
        candidate: ExtractionCandidate,
        val_status: ResultValidationStatus,
        *,
        acquisition_method: AcquisitionMethod,
        response_status: int,
        final_url: str,
    ) -> SearchResult:
        return SearchResult(
            platform=self.platform.value,
            input=normalized.raw_input,
            username=candidate.matched_username or normalized.username,
            profile_url=candidate.resolved_profile_url or normalized.profile_url,
            user_id=candidate.user_id,
            id_type=candidate.id_type.value,
            acquisition_method=acquisition_method.value,
            extraction_method=candidate.extraction_method.value,
            extraction_source=candidate.extraction_source,
            response_status=response_status,
            final_url=final_url,
            confidence=Confidence.NOT_VERIFIED.value,
            validation_status=val_status.value,
            status=ResultStatus.FOUND.value,
            error_message=candidate.notes,
        )

    def extract_from_response(
        self,
        normalized: NormalizedInput,
        fetch: FetchResponse,
        *,
        acquisition_method: AcquisitionMethod = AcquisitionMethod.HTTP,
        progress_cb: ProgressCallback | None = None,
    ) -> SearchResult:
        """Run restriction detection + 4-priority extraction pipeline on an acquired FetchResponse."""
        # Inspect redirect history / final URL / login or CAPTCHA HTML BEFORE generic HTTP 400/403 errors
        if fetch.error_type not in ("TIMEOUT", "RATE_LIMITED", "NOT_FOUND", "NETWORK_ERROR", "SERVER_ERROR"):
            restricted_status, restricted_reason = self.detect_access_block(fetch)
            if restricted_status is not None:
                return SearchResult(
                    platform=self.platform.value,
                    input=normalized.raw_input,
                    username=normalized.username,
                    profile_url=normalized.profile_url,
                    user_id="",
                    id_type=IdType.UNKNOWN.value,
                    acquisition_method=acquisition_method.value,
                    extraction_method=ExtractionMethod.NONE.value,
                    extraction_source="",
                    response_status=fetch.status_code,
                    final_url=fetch.url or normalized.profile_url,
                    confidence=Confidence.NOT_VERIFIED.value,
                    validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
                    status=restricted_status.value,
                    error_message=restricted_reason,
                )

        if not fetch.ok:
            return self._error_result_from_fetch(normalized, fetch, acquisition_method=acquisition_method)

        html = fetch.text or ""
        final_url = fetch.url or normalized.profile_url
        if not html.strip():
            direct = self.fallback_direct_url_candidate(normalized, fetch)
            if direct is not None:
                return self._build_found_result(
                    normalized,
                    direct,
                    ResultValidationStatus.PARTIALLY_VERIFIED,
                    acquisition_method=acquisition_method,
                    response_status=fetch.status_code,
                    final_url=final_url,
                )
            return SearchResult(
                platform=self.platform.value,
                input=normalized.raw_input,
                username=normalized.username,
                profile_url=normalized.profile_url,
                acquisition_method=acquisition_method.value,
                response_status=fetch.status_code,
                final_url=final_url,
                status=ResultStatus.NOT_FOUND.value,
                validation_status=ResultValidationStatus.NOT_AVAILABLE.value,
                error_message="Empty HTML response received from profile page.",
            )

        soup = BeautifulSoup(html, "lxml")

        strategies = [
            ("Priority 1: Embedded JSON", lambda: self.strategy_embedded_json(html, soup, normalized, final_url)),
            ("Priority 2: Structured Metadata", lambda: self.strategy_structured_metadata(html, soup, normalized, final_url)),
            ("Priority 3: HTML Attributes", lambda: self.strategy_html_attributes(html, soup, normalized, final_url)),
            ("Priority 4: Regex Fallback", lambda: self.strategy_regex_fallback(html, normalized, final_url)),
        ]

        last_val_status = ResultValidationStatus.NOT_AVAILABLE
        last_reason = ""
        best_partial: tuple[ExtractionCandidate, ResultValidationStatus] | None = None

        for label, fn in strategies:
            if progress_cb:
                progress_cb(f"[{self.platform.display_name}] ({acquisition_method.value}) Running {label}")
            try:
                candidates = fn()
            except Exception as exc:
                logger.warning("[%s] %s failed safely: %s", self.platform.value, label, exc)
                continue

            if not candidates:
                continue

            chosen, val_status, reason = self._evaluate_candidates(candidates, normalized)
            if chosen is not None:
                if val_status == ResultValidationStatus.VERIFIED and chosen.id_type != IdType.SEC_UID:
                    return self._build_found_result(
                        normalized,
                        chosen,
                        val_status,
                        acquisition_method=acquisition_method,
                        response_status=fetch.status_code,
                        final_url=final_url,
                    )
                if best_partial is None:
                    best_partial = (chosen, val_status)
                elif best_partial[0].id_type == IdType.SEC_UID and chosen.id_type != IdType.SEC_UID:
                    best_partial = (chosen, val_status)
                if val_status == ResultValidationStatus.PARTIALLY_VERIFIED and chosen.extraction_method in (
                    ExtractionMethod.EMBEDDED_JSON,
                    ExtractionMethod.STRUCTURED_METADATA,
                ):
                    return self._build_found_result(
                        normalized,
                        chosen,
                        val_status,
                        acquisition_method=acquisition_method,
                        response_status=fetch.status_code,
                        final_url=final_url,
                    )
            elif reason:
                last_val_status = val_status
                last_reason = reason
                if val_status in (
                    ResultValidationStatus.IDENTITY_MISMATCH,
                    ResultValidationStatus.CONFLICTING_IDS,
                ):
                    return SearchResult(
                        platform=self.platform.value,
                        input=normalized.raw_input,
                        username=normalized.username,
                        profile_url=normalized.profile_url,
                        user_id="",
                        id_type=IdType.UNKNOWN.value,
                        acquisition_method=acquisition_method.value,
                        extraction_method=ExtractionMethod.NONE.value,
                        extraction_source="",
                        response_status=fetch.status_code,
                        final_url=final_url,
                        confidence=Confidence.NOT_VERIFIED.value,
                        validation_status=val_status.value,
                        status=ResultStatus.NOT_FOUND.value,
                        error_message=reason,
                    )

        if best_partial is not None:
            return self._build_found_result(
                normalized,
                best_partial[0],
                best_partial[1],
                acquisition_method=acquisition_method,
                response_status=fetch.status_code,
                final_url=final_url,
            )

        direct = self.fallback_direct_url_candidate(normalized, fetch)
        if direct is not None:
            return self._build_found_result(
                normalized,
                direct,
                ResultValidationStatus.PARTIALLY_VERIFIED,
                acquisition_method=acquisition_method,
                response_status=fetch.status_code,
                final_url=final_url,
            )

        return SearchResult(
            platform=self.platform.value,
            input=normalized.raw_input,
            username=normalized.username,
            profile_url=normalized.profile_url,
            user_id="",
            id_type=IdType.UNKNOWN.value,
            acquisition_method=acquisition_method.value,
            extraction_method=ExtractionMethod.NONE.value,
            extraction_source="",
            response_status=fetch.status_code,
            final_url=final_url,
            confidence=Confidence.NOT_VERIFIED.value,
            validation_status=last_val_status.value,
            status=ResultStatus.NOT_FOUND.value,
            error_message=last_reason or f"No publicly verifiable user ID exposed on {self.platform.display_name} profile.",
        )

    async def extract_user_id(
        self,
        normalized: NormalizedInput,
        timeout: float = 15.0,
        retry_limit: int = 1,
        progress_cb: ProgressCallback | None = None,
        http_client: HttpClient | None = None,
    ) -> SearchResult:
        """Fetch public profile page via HTTP (Method A) and execute ordered extraction pipeline."""
        owns_client = http_client is None
        client = http_client or HttpClient(timeout=timeout, retry_limit=retry_limit)
        try:
            if progress_cb:
                progress_cb(f"[{self.platform.display_name}] (HTTP) Fetching {normalized.profile_url}")
            fetch = await client.get(
                normalized.profile_url,
                headers=self._request_headers(normalized),
                timeout=timeout,
                retry_limit=retry_limit,
            )
        finally:
            if owns_client:
                await client.aclose()

        return self.extract_from_response(
            normalized,
            fetch,
            acquisition_method=AcquisitionMethod.HTTP,
            progress_cb=progress_cb,
        )

    def validate_result(self, result: SearchResult, normalized: NormalizedInput) -> SearchResult:
        """Validate extracted user ID against target profile."""
        return validate_and_score(result, normalized)
