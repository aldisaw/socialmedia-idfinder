"""TikTok platform adapter — distinguishes NUMERIC_USER_ID, SEC_UID, and uniqueId username."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from core.models import (
    ExtractionCandidate,
    ExtractionMethod,
    IdType,
    NormalizedInput,
    Platform,
)
from core.normalizer import detect_platform, normalize_input
from extractors.base import (
    BaseExtractor,
    clean_handle,
    parse_json_ld_blocks,
    parse_script_json_by_id,
)

_RE_NUMERIC = re.compile(r"^\d{3,25}$")
_RE_SEC_UID = re.compile(r"^[A-Za-z0-9_-]{20,140}$")
_RE_APP_LINK_UID = re.compile(r"(?:snssdk\d+|tiktok)://user/profile/(\d{3,25})(?:[/?#]|$)", re.I)
_RE_HANDLE_IN_URL = re.compile(r"tiktok\.com/@([A-Za-z0-9._]{2,24})(?:[/?#]|$)", re.I)

_RE_PAIR_ID_UNIQUE = re.compile(
    r'"id"\s*:\s*"(\d{3,25})"[^{}]{0,600}?"uniqueId"\s*:\s*"([A-Za-z0-9._]{2,24})"'
)
_RE_PAIR_UNIQUE_ID = re.compile(
    r'"uniqueId"\s*:\s*"([A-Za-z0-9._]{2,24})"[^{}]{0,600}?"id"\s*:\s*"(\d{3,25})"'
)
_RE_PAIR_UNIQUE_SECUID = re.compile(
    r'"uniqueId"\s*:\s*"([A-Za-z0-9._]{2,24})"[^{}]{0,600}?"secUid"\s*:\s*"(MS4wLjAB[A-Za-z0-9_-]{12,130})"'
)
_RE_PAIR_SECUID_UNIQUE = re.compile(
    r'"secUid"\s*:\s*"(MS4wLjAB[A-Za-z0-9_-]{12,130})"[^{}]{0,600}?"uniqueId"\s*:\s*"([A-Za-z0-9._]{2,24})"'
)


def _is_sec_uid(val: str) -> bool:
    return bool(val and not val.isdigit() and _RE_SEC_UID.match(val))


def _extract_handle_from_url(url: str) -> str:
    m = _RE_HANDLE_IN_URL.search(url or "")
    return m.group(1) if m else ""


class TikTokExtractor(BaseExtractor):
    platform = Platform.TIKTOK

    def detect_input(self, raw_input: str) -> bool:
        return detect_platform(raw_input) == self.platform

    def normalize_input(self, raw_input: str) -> NormalizedInput:
        return normalize_input(raw_input, selected_platform=self.platform)

    def _candidates_from_user_node(
        self,
        user_node: dict,
        source_prefix: str,
        target_user: str,
    ) -> list[ExtractionCandidate]:
        out: list[ExtractionCandidate] = []
        unique_id = str(user_node.get("uniqueId") or "").strip()
        uid = str(user_node.get("id") or "").strip()
        sec_uid = str(user_node.get("secUid") or "").strip()
        confirmed = bool(unique_id and clean_handle(unique_id) == target_user)
        resolved_url = f"https://www.tiktok.com/@{unique_id}" if unique_id else ""

        if _RE_NUMERIC.match(uid):
            out.append(
                ExtractionCandidate(
                    user_id=uid,
                    id_type=IdType.NUMERIC_USER_ID,
                    extraction_method=ExtractionMethod.EMBEDDED_JSON,
                    extraction_source=f"{source_prefix}.id",
                    matched_username=unique_id,
                    identity_confirmed=confirmed,
                    resolved_profile_url=resolved_url,
                )
            )

        if _is_sec_uid(sec_uid):
            out.append(
                ExtractionCandidate(
                    user_id=sec_uid,
                    id_type=IdType.SEC_UID,
                    extraction_method=ExtractionMethod.EMBEDDED_JSON,
                    extraction_source=f"{source_prefix}.secUid",
                    matched_username=unique_id,
                    identity_confirmed=confirmed,
                    resolved_profile_url=resolved_url,
                )
            )
        return out

    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        # 1a. __UNIVERSAL_DATA_FOR_REHYDRATION__
        uni = parse_script_json_by_id(soup, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
        if isinstance(uni, dict):
            scope = uni.get("__DEFAULT_SCOPE__")
            if isinstance(scope, dict):
                detail = scope.get("webapp.user-detail")
                if isinstance(detail, dict):
                    user_node = (detail.get("userInfo") or {}).get("user")
                    if isinstance(user_node, dict):
                        candidates.extend(
                            self._candidates_from_user_node(
                                user_node,
                                "__UNIVERSAL_DATA_FOR_REHYDRATION__.__DEFAULT_SCOPE__.webapp.user-detail.userInfo.user",
                                target_user,
                            )
                        )

        # 1b. Legacy SIGI_STATE
        sigi = parse_script_json_by_id(soup, "SIGI_STATE")
        if isinstance(sigi, dict):
            users = (sigi.get("UserModule") or {}).get("users")
            if isinstance(users, dict):
                for _k, user_node in users.items():
                    if not isinstance(user_node, dict):
                        continue
                    # In multi-user map, match target uniqueId only unless single profile entry
                    uname = clean_handle(str(user_node.get("uniqueId") or ""))
                    if uname == target_user or len(users) == 1:
                        candidates.extend(
                            self._candidates_from_user_node(
                                user_node,
                                "SIGI_STATE.UserModule.users",
                                target_user,
                            )
                        )

        return candidates

    def strategy_structured_metadata(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        for block in parse_json_ld_blocks(soup):
            btype = str(block.get("@type") or "")
            if btype not in ("Person", "ProfilePage"):
                continue
            entity = block.get("mainEntity") if isinstance(block.get("mainEntity"), dict) else block
            ident = str(entity.get("identifier") or "").strip()
            alt = str(entity.get("alternateName") or "").strip().lstrip("@")
            url_handle = _extract_handle_from_url(str(entity.get("url") or ""))
            handle = alt or url_handle
            confirmed = bool(handle and clean_handle(handle) == target_user)

            if _RE_NUMERIC.match(ident):
                candidates.append(
                    ExtractionCandidate(
                        user_id=ident,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json'].identifier",
                        matched_username=handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.tiktok.com/@{handle}" if handle else normalized.profile_url,
                    )
                )
            elif _is_sec_uid(ident):
                candidates.append(
                    ExtractionCandidate(
                        user_id=ident,
                        id_type=IdType.SEC_UID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json'].identifier",
                        matched_username=handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.tiktok.com/@{handle}" if handle else normalized.profile_url,
                    )
                )

        return candidates

    def strategy_html_attributes(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        canon = soup.find("link", attrs={"rel": "canonical"})
        canon_handle = _extract_handle_from_url(str(canon.get("href") or "")) if canon else ""
        page_handle = canon_handle or _extract_handle_from_url(final_url)

        for prop in ("al:ios:url", "al:android:url"):
            tag = soup.find("meta", attrs={"property": prop})
            if not tag or not tag.get("content"):
                continue
            m = _RE_APP_LINK_UID.search(str(tag["content"]))
            if m:
                uid = m.group(1)
                confirmed = bool(page_handle and clean_handle(page_handle) == target_user)
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source=f"meta[property='{prop}']",
                        matched_username=page_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.tiktok.com/@{page_handle}" if page_handle else normalized.profile_url,
                    )
                )

        return candidates

    def strategy_regex_fallback(
        self,
        html: str,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        for m in _RE_PAIR_ID_UNIQUE.finditer(html):
            uid, uname = m.group(1), m.group(2)
            if clean_handle(uname) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:id+uniqueId",
                        matched_username=uname,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.tiktok.com/@{uname}",
                    )
                )

        for m in _RE_PAIR_UNIQUE_ID.finditer(html):
            uname, uid = m.group(1), m.group(2)
            if clean_handle(uname) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:uniqueId+id",
                        matched_username=uname,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.tiktok.com/@{uname}",
                    )
                )

        if not candidates:
            for pat, order in ((_RE_PAIR_UNIQUE_SECUID, True), (_RE_PAIR_SECUID_UNIQUE, False)):
                for m in pat.finditer(html):
                    uname, sec_uid = (m.group(1), m.group(2)) if order else (m.group(2), m.group(1))
                    if clean_handle(uname) == target_user:
                        candidates.append(
                            ExtractionCandidate(
                                user_id=sec_uid,
                                id_type=IdType.SEC_UID,
                                extraction_method=ExtractionMethod.REGEX_FALLBACK,
                                extraction_source="regex:secUid+uniqueId",
                                matched_username=uname,
                                identity_confirmed=False,
                                resolved_profile_url=f"https://www.tiktok.com/@{uname}",
                            )
                        )

        return candidates
