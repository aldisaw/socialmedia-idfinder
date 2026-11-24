"""Instagram platform adapter — targets publicly exposed NUMERIC_USER_ID."""

from __future__ import annotations

import json
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
    extract_json_object_after,
    parse_json_ld_blocks,
)

_RE_NUMERIC = re.compile(r"^\d{3,25}$")
_RE_IG_HANDLE_IN_URL = re.compile(r"instagram\.com/([A-Za-z0-9._]{1,30})(?:[/?#]|$)", re.I)

_RE_PAIR_PROFILE_ID_USER = re.compile(
    r'"(?:profile_id|pk|id)"\s*:\s*"(\d{3,25})"[^{}]{0,300}?"username"\s*:\s*"([A-Za-z0-9._]{1,30})"'
)
_RE_PAIR_USER_PROFILE_ID = re.compile(
    r'"username"\s*:\s*"([A-Za-z0-9._]{1,30})"[^{}]{0,300}?"(?:profile_id|pk|id)"\s*:\s*"(\d{3,25})"'
)


def _extract_ig_username_from_url(url: str) -> str:
    m = _RE_IG_HANDLE_IN_URL.search(url or "")
    if not m:
        return ""
    val = m.group(1)
    if val.lower() in ("accounts", "p", "reel", "reels", "explore", "login"):
        return ""
    return val


def _walk_json_dicts(obj, max_nodes: int = 3000):
    stack = [obj]
    visited = 0
    while stack and visited < max_nodes:
        cur = stack.pop()
        visited += 1
        if isinstance(cur, dict):
            yield cur
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


class InstagramExtractor(BaseExtractor):
    platform = Platform.INSTAGRAM

    def detect_input(self, raw_input: str) -> bool:
        return detect_platform(raw_input) == self.platform

    def normalize_input(self, raw_input: str) -> NormalizedInput:
        return normalize_input(raw_input, selected_platform=self.platform)

    def _candidate_from_user_dict(
        self,
        user_obj: dict,
        source: str,
        target_user: str,
        explicit_profile: bool,
    ) -> ExtractionCandidate | None:
        uid = str(user_obj.get("profile_id") or user_obj.get("pk") or user_obj.get("id") or "").strip()
        uname = str(user_obj.get("username") or "").strip()
        if not _RE_NUMERIC.match(uid) or not uname:
            return None

        matches_target = clean_handle(uname) == target_user
        if not matches_target and not explicit_profile:
            return None

        return ExtractionCandidate(
            user_id=uid,
            id_type=IdType.NUMERIC_USER_ID,
            extraction_method=ExtractionMethod.EMBEDDED_JSON,
            extraction_source=source,
            matched_username=uname,
            identity_confirmed=matches_target,
            resolved_profile_url=f"https://www.instagram.com/{uname}",
        )

    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        authoritative: list[ExtractionCandidate] = []
        secondary_users: list[ExtractionCandidate] = []

        # 1a. window._sharedData
        shared = extract_json_object_after(html, "window._sharedData =")
        if isinstance(shared, dict):
            pages = (shared.get("entry_data") or {}).get("ProfilePage")
            if isinstance(pages, list) and pages:
                user_obj = ((pages[0] or {}).get("graphql") or {}).get("user")
                if isinstance(user_obj, dict):
                    cand = self._candidate_from_user_dict(
                        user_obj,
                        "window._sharedData.entry_data.ProfilePage.graphql.user",
                        target_user,
                        explicit_profile=True,
                    )
                    if cand:
                        authoritative.append(cand)

        # 1b. <script type="application/json"> (Polaris / Relay profile payloads)
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            for node in _walk_json_dicts(data):
                # Explicit authoritative profile container keys
                for pkey in ("profilePage", "xdt_api__v1__users__web_profile_info"):
                    sub = node.get(pkey)
                    if isinstance(sub, dict):
                        inner = sub.get("user") if isinstance(sub.get("user"), dict) else sub
                        cand = self._candidate_from_user_dict(
                            inner,
                            f"script[type='application/json'].{pkey}",
                            target_user,
                            explicit_profile=True,
                        )
                        if cand:
                            authoritative.append(cand)

                # Secondary nested user objects (skip 17-digit 178414... Graph FBID stubs lacking pk)
                sub_user = node.get("user")
                if isinstance(sub_user, dict):
                    has_pk = bool(sub_user.get("pk") or sub_user.get("profile_id"))
                    raw_id = str(sub_user.get("id") or "").strip()
                    is_graph_fbid = (not has_pk) and len(raw_id) == 17 and raw_id.startswith("178414")
                    if not is_graph_fbid:
                        cand = self._candidate_from_user_dict(
                            sub_user,
                            "script[type='application/json'].user",
                            target_user,
                            explicit_profile=False,
                        )
                        if cand:
                            secondary_users.append(cand)

                # Direct Polaris profile_id + username dict
                if "profile_id" in node and "username" in node:
                    cand = self._candidate_from_user_dict(
                        node,
                        "script[type='application/json'].profile_id",
                        target_user,
                        explicit_profile=True,
                    )
                    if cand:
                        authoritative.append(cand)

                # Rendered Polaris profile props: {"props": {"id": "<numeric>", ...}} paired with verified page title/canonical
                res_node = node.get("resource")
                props_node = node.get("props")
                if (
                    isinstance(res_node, dict)
                    and "PolarisProfile" in str(res_node.get("__dr") or "")
                    and isinstance(props_node, dict)
                ):
                    pid = str(props_node.get("id") or "").strip()
                    if _RE_NUMERIC.match(pid):
                        page_user = _extract_ig_username_from_url(final_url)
                        title_tag = soup.title.string if (soup.title and soup.title.string) else ""
                        title_m = re.search(r"\(@([A-Za-z0-9._]{1,30})\)", title_tag)
                        title_user = title_m.group(1) if title_m else ""
                        matched_u = title_user or page_user
                        if matched_u:
                            authoritative.append(
                                ExtractionCandidate(
                                    user_id=pid,
                                    id_type=IdType.NUMERIC_USER_ID,
                                    extraction_method=ExtractionMethod.EMBEDDED_JSON,
                                    extraction_source="script[type='application/json'].PolarisProfile.props.id",
                                    matched_username=matched_u,
                                    identity_confirmed=(clean_handle(matched_u) == target_user),
                                    resolved_profile_url=f"https://www.instagram.com/{matched_u}",
                                )
                            )

        return authoritative if authoritative else secondary_users

    def strategy_structured_metadata(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        # JSON-LD Person / ProfilePage
        for block in parse_json_ld_blocks(soup):
            btype = str(block.get("@type") or "")
            if btype not in ("Person", "ProfilePage"):
                continue
            entity = block.get("mainEntity") if isinstance(block.get("mainEntity"), dict) else block
            uid = str(entity.get("identifier") or "").strip()
            alt = str(entity.get("alternateName") or "").strip().lstrip("@")
            url_user = _extract_ig_username_from_url(str(entity.get("url") or ""))
            uname = alt or url_user
            if _RE_NUMERIC.match(uid) and uname:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json'].identifier",
                        matched_username=uname,
                        identity_confirmed=(clean_handle(uname) == target_user),
                        resolved_profile_url=f"https://www.instagram.com/{uname}",
                    )
                )

        # instapp:owner_user_id paired with canonical/og:url
        owner_meta = soup.find("meta", attrs={"property": "instapp:owner_user_id"})
        if owner_meta and owner_meta.get("content"):
            uid = str(owner_meta["content"]).strip()
            if _RE_NUMERIC.match(uid):
                og_url = soup.find("meta", attrs={"property": "og:url"})
                canon = soup.find("link", attrs={"rel": "canonical"})
                page_user = (
                    _extract_ig_username_from_url(str(og_url.get("content") or "")) if og_url else ""
                ) or (
                    _extract_ig_username_from_url(str(canon.get("href") or "")) if canon else ""
                ) or _extract_ig_username_from_url(final_url)
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="meta[property='instapp:owner_user_id']",
                        matched_username=page_user,
                        identity_confirmed=bool(page_user and clean_handle(page_user) == target_user),
                        resolved_profile_url=f"https://www.instagram.com/{page_user}" if page_user else normalized.profile_url,
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

        for elem in soup.find_all(attrs={"data-profile-id": True, "data-username": True}):
            uid = str(elem.get("data-profile-id") or "").strip()
            uname = str(elem.get("data-username") or "").strip().lstrip("@")
            if _RE_NUMERIC.match(uid) and clean_handle(uname) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="html[data-profile-id][data-username]",
                        matched_username=uname,
                        identity_confirmed=True,
                        resolved_profile_url=f"https://www.instagram.com/{uname}",
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

        for m in _RE_PAIR_PROFILE_ID_USER.finditer(html):
            uid, uname = m.group(1), m.group(2)
            if clean_handle(uname) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:profile_id+username",
                        matched_username=uname,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.instagram.com/{uname}",
                    )
                )

        for m in _RE_PAIR_USER_PROFILE_ID.finditer(html):
            uname, uid = m.group(1), m.group(2)
            if clean_handle(uname) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:username+profile_id",
                        matched_username=uname,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.instagram.com/{uname}",
                    )
                )

        return candidates
