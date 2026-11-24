"""X (Twitter) platform adapter — structured SSR/Relay User JSON parser (never confuses Tweet IDs)."""

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
    parse_script_json_by_id,
)

_RE_NUMERIC = re.compile(r"^\d{1,25}$")
_TWEET_TYPENAMES = frozenset(
    {"Tweet", "TweetWithVisibilityResults", "TimelineTweet", "NoteTweet", "TimelineTimelineItem"}
)
_TWEET_ONLY_KEYS = frozenset(
    {"full_text", "bookmark_count", "favorite_count", "retweet_count", "conversation_id_str", "in_reply_to_status_id_str"}
)

# TanStack Router $R profile object marker: i:"#$username#_profile#<Handle>{}"
_RE_TSR_PROFILE_MARKER = re.compile(r'i\s*:\s*"#\$username#_profile#([A-Za-z0-9_]{1,15})\{\}"')
_RE_TSR_REST_ID = re.compile(r'\brestId\s*:\s*"(\d{1,25})"')
_RE_TSR_SCREEN_NAME = re.compile(r'\bscreenName\s*:\s*"([A-Za-z0-9_]{1,15})"')

# Strictly scoped fallback: ONLY inside an explicit User-typed object fragment (never matches Tweet objects)
_RE_USER_TYPED_FALLBACK_1 = re.compile(
    r'"__typename"\s*:\s*"User"[^{}]{0,250}?"rest_id"\s*:\s*"(\d{1,25})"[^{}]{0,400}?"screen_name"\s*:\s*"([A-Za-z0-9_]{1,15})"'
)
_RE_USER_TYPED_FALLBACK_2 = re.compile(
    r'"__typename"\s*:\s*"User"[^{}]{0,400}?"screen_name"\s*:\s*"([A-Za-z0-9_]{1,15})"[^{}]{0,250}?"rest_id"\s*:\s*"(\d{1,25})"'
)


def _extract_balanced_braces(text: str, open_pos: int) -> str | None:
    """Extract balanced {...} block starting at open_pos, respecting quoted strings."""
    if open_pos < 0 or open_pos >= len(text) or text[open_pos] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(open_pos, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_pos : i + 1]
    return None


def _iter_decoded_json_strings_in_script(script_text: str) -> list[dict]:
    """Safely locate and double-decode backslash-escaped JSON string payloads inside self.$R / $_TSR scripts."""
    decoded: list[dict] = []
    # Scan for double-quoted JS strings starting with escaped JSON object: "{\\"
    search_pos = 0
    marker = '"{\\"'
    while True:
        start = script_text.find(marker, search_pos)
        if start == -1:
            break
        # Walk to closing unescaped double quote
        end = -1
        esc = False
        for i in range(start + 1, len(script_text)):
            ch = script_text[i]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                end = i + 1
                break
        if end == -1:
            break
        search_pos = end
        raw_literal = script_text[start:end]
        if "rest_id" not in raw_literal and "screen_name" not in raw_literal:
            continue
        try:
            unescaped = json.loads(raw_literal)
            if isinstance(unescaped, str):
                obj = json.loads(unescaped)
                if isinstance(obj, dict):
                    decoded.append(obj)
        except json.JSONDecodeError:
            continue
    return decoded


def _walk_json_dicts(obj, max_nodes: int = 4000):
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


def _is_tweet_object(node: dict) -> bool:
    typename = str(node.get("__typename") or "")
    if typename in _TWEET_TYPENAMES:
        return True
    if any(k in node for k in _TWEET_ONLY_KEYS):
        return True
    legacy = node.get("legacy")
    if isinstance(legacy, dict) and any(k in legacy for k in _TWEET_ONLY_KEYS):
        return True
    return False


def _extract_screen_name_from_user_node(user_obj: dict) -> str:
    """Extract screen_name from X User object (supports modern core.screen_name and legacy.screen_name)."""
    if isinstance(user_obj.get("screen_name"), str) and user_obj["screen_name"].strip():
        return user_obj["screen_name"].strip()
    core = user_obj.get("core")
    if isinstance(core, dict) and isinstance(core.get("screen_name"), str) and core["screen_name"].strip():
        return core["screen_name"].strip()
    legacy = user_obj.get("legacy")
    if isinstance(legacy, dict) and isinstance(legacy.get("screen_name"), str) and legacy["screen_name"].strip():
        return legacy["screen_name"].strip()
    return ""


class XExtractor(BaseExtractor):
    platform = Platform.X

    def detect_input(self, raw_input: str) -> bool:
        return detect_platform(raw_input) == self.platform

    def normalize_input(self, raw_input: str) -> NormalizedInput:
        return normalize_input(raw_input, selected_platform=self.platform)

    def _collect_user_obj(
        self,
        user_obj: dict,
        source: str,
        target_user: str,
        is_explicit_profile_node: bool,
    ) -> ExtractionCandidate | None:
        # CRITICAL: Never treat a Tweet/Post object as a User object!
        if _is_tweet_object(user_obj):
            return None

        typename = str(user_obj.get("__typename") or "")
        if typename and typename != "User":
            return None

        legacy = user_obj.get("legacy") if isinstance(user_obj.get("legacy"), dict) else {}
        uid = str(user_obj.get("rest_id") or user_obj.get("id_str") or legacy.get("id_str") or "").strip()
        screen = _extract_screen_name_from_user_node(user_obj)
        if not _RE_NUMERIC.match(uid) or not screen:
            return None

        matches_target = clean_handle(screen) == target_user
        if not matches_target and not is_explicit_profile_node:
            return None

        return ExtractionCandidate(
            user_id=uid,
            id_type=IdType.NUMERIC_USER_ID,
            extraction_method=ExtractionMethod.EMBEDDED_JSON,
            extraction_source=source,
            matched_username=screen,
            identity_confirmed=matches_target,
            resolved_profile_url=f"https://x.com/{screen}",
        )

    def _extract_from_tsr_scripts(
        self,
        soup: BeautifulSoup,
        target_user: str,
    ) -> list[ExtractionCandidate]:
        """Parse X's self.$R / self.$_TSR Relay SSR streaming payloads."""
        candidates: list[ExtractionCandidate] = []

        for sc in soup.find_all("script"):
            txt = sc.string or sc.get_text() or ""
            if not txt or ("$R" not in txt and "$_TSR" not in txt):
                continue

            # 1) Decode backslash-escaped Relay JSON string payloads ("{\"data\":...}")
            for payload in _iter_decoded_json_strings_in_script(txt):
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

                # Explicit top-level profile lookup nodes (report mismatch if screen_name differs!)
                for profile_key in ("user_result_by_screen_name", "user"):
                    holder = data.get(profile_key) if isinstance(data, dict) else None
                    if isinstance(holder, dict):
                        res_node = holder.get("result") if isinstance(holder.get("result"), dict) else holder
                        cand = self._collect_user_obj(
                            res_node,
                            f"self.$R.relay.{profile_key}.result.rest_id",
                            target_user,
                            is_explicit_profile_node=True,
                        )
                        if cand:
                            candidates.append(cand)

                # Walk nested Relay graph strictly for __typename == "User" matching target screen_name
                for node in _walk_json_dicts(data):
                    if node.get("__typename") == "User":
                        cand = self._collect_user_obj(
                            node,
                            "self.$R.relay.User.rest_id",
                            target_user,
                            is_explicit_profile_node=False,
                        )
                        if cand:
                            candidates.append(cand)

            # 2) Structured TanStack $R profile state: i:"#$username#_profile#<Handle>{}",...,l:$R[n]={...restId:"...",...screenName:"..."}
            for m in _RE_TSR_PROFILE_MARKER.finditer(txt):
                # Locate l:$R[n]={...} or l:{...} inside the enclosing profile state entry
                l_idx = txt.find("l:", m.end())
                if l_idx == -1 or l_idx - m.end() > 160:
                    continue
                brace_pos = txt.find("{", l_idx)
                if brace_pos == -1 or brace_pos - l_idx > 30:
                    continue
                block = _extract_balanced_braces(txt, brace_pos)
                if not block:
                    continue
                m_uid = _RE_TSR_REST_ID.search(block)
                m_scr = _RE_TSR_SCREEN_NAME.search(block)
                if m_uid and m_scr:
                    uid = m_uid.group(1)
                    screen = m_scr.group(1)
                    candidates.append(
                        ExtractionCandidate(
                            user_id=uid,
                            id_type=IdType.NUMERIC_USER_ID,
                            extraction_method=ExtractionMethod.EMBEDDED_JSON,
                            extraction_source="self.$R.profile.restId",
                            matched_username=screen,
                            identity_confirmed=(clean_handle(screen) == target_user),
                            resolved_profile_url=f"https://x.com/{screen}",
                        )
                    )

        return candidates

    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        # 1a. Modern X Relay / TanStack SSR streaming payloads (self.$R / self.$_TSR)
        candidates.extend(self._extract_from_tsr_scripts(soup, target_user))

        # 1b. window.__INITIAL_STATE__
        init_state = extract_json_object_after(html, "window.__INITIAL_STATE__ =")
        if isinstance(init_state, dict):
            users_map = ((init_state.get("entities") or {}).get("users") or {}).get("entities")
            if isinstance(users_map, dict):
                for _k, uobj in users_map.items():
                    if isinstance(uobj, dict):
                        cand = self._collect_user_obj(
                            uobj,
                            "__INITIAL_STATE__.entities.users.entities",
                            target_user,
                            is_explicit_profile_node=False,
                        )
                        if cand:
                            candidates.append(cand)

        # 1c. <script id="__NEXT_DATA__">
        next_data = parse_script_json_by_id(soup, "__NEXT_DATA__")
        if isinstance(next_data, dict):
            page_props = (next_data.get("props") or {}).get("pageProps")
            if isinstance(page_props, dict):
                for key in ("user", "profileUser", "userResult"):
                    node = page_props.get(key)
                    if isinstance(node, dict):
                        inner = node.get("result") if isinstance(node.get("result"), dict) else node
                        cand = self._collect_user_obj(
                            inner,
                            f"__NEXT_DATA__.props.pageProps.{key}",
                            target_user,
                            is_explicit_profile_node=True,
                        )
                        if cand:
                            candidates.append(cand)

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
            if btype != "ProfilePage":
                continue
            entity = block.get("mainEntity") or block.get("author")
            if not isinstance(entity, dict):
                continue
            uid = str(entity.get("identifier") or "").strip()
            screen = str(entity.get("additionalName") or entity.get("alternateName") or "").strip().lstrip("@")
            if _RE_NUMERIC.match(uid) and screen:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json'].ProfilePage",
                        matched_username=screen,
                        identity_confirmed=(clean_handle(screen) == target_user),
                        resolved_profile_url=f"https://x.com/{screen}",
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

        for elem in soup.find_all(attrs={"data-user-id": True, "data-screen-name": True}):
            uid = str(elem.get("data-user-id") or "").strip()
            screen = str(elem.get("data-screen-name") or "").strip().lstrip("@")
            if _RE_NUMERIC.match(uid) and clean_handle(screen) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="html[data-user-id][data-screen-name]",
                        matched_username=screen,
                        identity_confirmed=True,
                        resolved_profile_url=f"https://x.com/{screen}",
                    )
                )

        return candidates

    def strategy_regex_fallback(
        self,
        html: str,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        """Strictly User-typed fallback only (never uses broad document-wide rest_id regex)."""
        target_user = clean_handle(normalized.username)
        candidates: list[ExtractionCandidate] = []

        for m in _RE_USER_TYPED_FALLBACK_1.finditer(html):
            uid, screen = m.group(1), m.group(2)
            if clean_handle(screen) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:User.rest_id+screen_name",
                        matched_username=screen,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://x.com/{screen}",
                    )
                )

        for m in _RE_USER_TYPED_FALLBACK_2.finditer(html):
            screen, uid = m.group(1), m.group(2)
            if clean_handle(screen) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_USER_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:User.screen_name+rest_id",
                        matched_username=screen,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://x.com/{screen}",
                    )
                )

        return candidates
