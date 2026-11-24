"""Facebook platform adapter — public-source extraction with URL, root-profile selectedID, and Playwright support.

Strictly public profile sources only: no login, no cookies, no CAPTCHA/protection bypass.
Never confuses Page IDs, Post IDs, Photo IDs, Comment IDs, or Graph API child objects with personal profile IDs.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

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
from core.normalizer import detect_platform, normalize_input
from extractors.base import BaseExtractor, ProgressCallback, clean_handle, parse_json_ld_blocks

_RE_FB_ID = re.compile(r"^[1-9]\d{0,24}$")
_RE_FB_PROFILE_URI = re.compile(r"fb://profile/([1-9]\d{0,24})(?:[/?#]|$)", re.I)
_RE_USER_VANITY_PAIR_1 = re.compile(
    r'"userID"\s*:\s*"([1-9]\d{0,24})"[^{}]{0,300}?"userVanity"\s*:\s*"([A-Za-z0-9.]{3,50})"'
)
_RE_USER_VANITY_PAIR_2 = re.compile(
    r'"userVanity"\s*:\s*"([A-Za-z0-9.]{3,50})"[^{}]{0,300}?"userID"\s*:\s*"([1-9]\d{0,24})"'
)
_RE_FB_PROFILE_REGEX = re.compile(r"fb://profile/([1-9]\d{0,24})")

# Disallowed JSON subtrees — never extract selectedID/userID from posts, photos, comments, feed items, or child actors
_EXCLUDED_SUBTREE_KEYS: frozenset[str] = frozenset(
    {
        "post",
        "posts",
        "story",
        "stories",
        "photo",
        "photos",
        "comment",
        "comments",
        "feedback",
        "attachment",
        "attachments",
        "timeline_list_feed_units",
        "feed_units",
        "feed",
        "edges",
        "node",
        "media",
        "video",
        "videos",
        "reel",
        "reels",
        "actor",
        "actors",
        "author",
        "likers",
        "reactors",
        "friends",
        "suggested",
        "recommendations",
        "notifications",
        "messages",
        "comet_sections",
        "page",
        "pages",
    }
)

_EXCLUDED_TYPENAMES: frozenset[str] = frozenset(
    {
        "Page",
        "Post",
        "Story",
        "Photo",
        "Comment",
        "Feedback",
        "Video",
        "Reel",
        "Group",
        "Event",
        "MarketplaceListing",
        "GraphQLStory",
        "StoryAttachment",
        "Organization",
        "LocalBusiness",
    }
)

_EXCLUDED_OBJECT_KEYS: frozenset[str] = frozenset(
    {"post_id", "story_fbid", "photo_id", "comment_id", "feedback_id", "media_id", "attachment_id"}
)

_ROOT_PROFILE_CONTEXT_HINTS: tuple[str, ...] = (
    "profile",
    "user",
    "owner",
    "routeprops",
    "props",
    "require",
    "define",
    "bbox",
    "root",
    "header",
    "entity",
    "variables",
)


def _extract_profile_php_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.path.rstrip("/").lower().endswith("/profile.php"):
        ids = parse_qs(parsed.query).get("id", [])
        if ids and _RE_FB_ID.match(ids[0].strip()):
            return ids[0].strip()
    return ""


def _extract_vanity_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    segs = [s for s in parsed.path.split("/") if s]
    if segs and segs[0].lower() not in ("profile.php", "people", "pages", "pg", "login", "login.php"):
        if not segs[0].isdigit():
            return segs[0]
    return ""


def _classify_url_id_source(raw_input: str) -> str:
    low = (raw_input or "").strip().lower()
    if "profile.php" in low:
        return "url:profile.php?id"
    if "/people/" in low:
        return "url:people_path"
    return "url:numeric_path"


def _walk_root_profile_dicts(obj, max_nodes: int = 3000):
    """Iterative bounded walk yielding (dict_node, path_keys) while pruning post/photo/comment/feed subtrees."""
    stack: list[tuple[object, tuple[str, ...]]] = [(obj, ())]
    visited = 0
    while stack and visited < max_nodes:
        cur, path = stack.pop()
        visited += 1
        if isinstance(cur, dict):
            typename = str(cur.get("__typename") or "")
            if typename in _EXCLUDED_TYPENAMES:
                continue
            if any(k in cur for k in _EXCLUDED_OBJECT_KEYS):
                continue
            yield cur, path
            for k, v in cur.items():
                k_low = str(k).lower()
                if k_low in _EXCLUDED_SUBTREE_KEYS:
                    continue
                if isinstance(v, (dict, list)):
                    stack.append((v, path + (k_low,)))
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append((v, path))


def _is_root_profile_context(node: dict, path: tuple[str, ...]) -> bool:
    """Confirm a selectedID node belongs to a root profile context, not an arbitrary JSON object."""
    space_type = str(node.get("selectedSpaceType") or "").strip().lower()
    if space_type and space_type != "profile":
        return False
    if space_type == "profile":
        return True
    if node.get("__typename") == "User":
        return True
    if any(k in node for k in ("userVanity", "vanity", "userID", "is_profile", "profile_url", "profile_owner")):
        return True
    if not path:
        return True
    return any(any(hint in seg for hint in _ROOT_PROFILE_CONTEXT_HINTS) for seg in path)


class FacebookExtractor(BaseExtractor):
    platform = Platform.FACEBOOK

    def detect_input(self, raw_input: str) -> bool:
        return detect_platform(raw_input) == self.platform

    def normalize_input(self, raw_input: str) -> NormalizedInput:
        return normalize_input(raw_input, selected_platform=self.platform)

    async def extract_user_id(
        self,
        normalized: NormalizedInput,
        timeout: float = 15.0,
        retry_limit: int = 1,
        progress_cb: ProgressCallback | None = None,
        http_client: HttpClient | None = None,
    ) -> SearchResult:
        """Sequential Facebook extraction:
        1) profile.php?id=<numeric_id> -> URL_EXTRACTED (not page-verified)
        2) numeric profile URL /<numeric_id> or /people/.../<numeric_id> -> URL_EXTRACTED (not page-verified)
        3) vanity URL -> HTTPX (Method A), followed by Playwright fallback (Method B via engine)
        """
        if normalized.direct_id and _RE_FB_ID.match(normalized.direct_id):
            url_source = _classify_url_id_source(normalized.raw_input)
            if progress_cb:
                progress_cb(f"[Facebook] Extracted numeric profile ID directly from URL ({url_source})")
            return SearchResult(
                platform=self.platform.value,
                input=normalized.raw_input,
                username="",
                profile_url=f"https://www.facebook.com/profile.php?id={normalized.direct_id}",
                user_id=normalized.direct_id,
                id_type=IdType.NUMERIC_PROFILE_ID.value,
                acquisition_method=AcquisitionMethod.NONE.value,
                extraction_method=ExtractionMethod.URL_EXTRACTED.value,
                extraction_source=url_source,
                response_status=0,
                final_url=normalized.profile_url,
                confidence=Confidence.MEDIUM.value,
                validation_status=ResultValidationStatus.PARTIALLY_VERIFIED.value,
                status=ResultStatus.FOUND.value,
                error_message="ID extracted from Facebook URL; not page-verified.",
            )

        return await super().extract_user_id(
            normalized,
            timeout=timeout,
            retry_limit=retry_limit,
            progress_cb=progress_cb,
            http_client=http_client,
        )

    def extract_from_response(
        self,
        normalized: NormalizedInput,
        fetch: FetchResponse,
        *,
        acquisition_method: AcquisitionMethod = AcquisitionMethod.HTTP,
        progress_cb: ProgressCallback | None = None,
    ) -> SearchResult:
        result = super().extract_from_response(
            normalized,
            fetch,
            acquisition_method=acquisition_method,
            progress_cb=progress_cb,
        )
        if (
            acquisition_method == AcquisitionMethod.BROWSER
            and result.status == ResultStatus.FOUND.value
            and result.extraction_method
            in (
                ExtractionMethod.HTML_JSON_EXTRACTED.value,
                ExtractionMethod.EMBEDDED_JSON.value,
                ExtractionMethod.STRUCTURED_METADATA.value,
                ExtractionMethod.HTML_METADATA.value,
            )
        ):
            result.extraction_method = ExtractionMethod.BROWSER_EXTRACTED.value
            if result.extraction_source and not result.extraction_source.startswith("browser_dom:"):
                result.extraction_source = f"browser_dom:{result.extraction_source}"
        return result

    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        target_user = clean_handle(normalized.username)
        target_direct = (normalized.direct_id or "").strip()
        candidates: list[ExtractionCandidate] = []

        og_url_tag = soup.find("meta", attrs={"property": "og:url"})
        og_url = str(og_url_tag.get("content") or "") if og_url_tag else ""
        page_vanity = _extract_vanity_from_url(og_url) or _extract_vanity_from_url(final_url)

        for script in soup.find_all("script", attrs={"type": "application/json"}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            for node, path in _walk_root_profile_dicts(data):
                typename = str(node.get("__typename") or "")
                # Never confuse Facebook Pages with personal profiles
                if typename == "Page" or ("pageID" in node and "userID" not in node and "selectedID" not in node):
                    continue

                vanity = str(node.get("userVanity") or node.get("vanity") or "").strip()
                if not vanity and node.get("url"):
                    vanity = _extract_vanity_from_url(str(node["url"]))

                # 1) Root-profile selectedID candidate
                if "selectedID" in node and _is_root_profile_context(node, path):
                    sel_id = str(node.get("selectedID") or "").strip()
                    if _RE_FB_ID.match(sel_id):
                        matched_v = vanity or page_vanity
                        confirmed = bool(
                            (target_direct and sel_id == target_direct)
                            or (target_user and vanity and clean_handle(vanity) == target_user)
                            or (target_user and page_vanity and clean_handle(page_vanity) == target_user and not vanity)
                        )
                        candidates.append(
                            ExtractionCandidate(
                                user_id=sel_id,
                                id_type=IdType.NUMERIC_PROFILE_ID,
                                extraction_method=ExtractionMethod.HTML_JSON_EXTRACTED,
                                extraction_source="html_json:selectedID",
                                matched_username=matched_v,
                                identity_confirmed=confirmed,
                                resolved_profile_url=f"https://www.facebook.com/profile.php?id={sel_id}",
                            )
                        )

                # 2) Root-profile userID / explicit User node
                uid = str(node.get("userID") or "").strip()
                if not uid and typename == "User":
                    uid = str(node.get("id") or "").strip()

                if not _RE_FB_ID.match(uid):
                    continue

                confirmed = False
                if target_direct and uid == target_direct:
                    confirmed = True
                elif target_user and vanity and clean_handle(vanity) == target_user:
                    confirmed = True
                elif target_user and vanity and clean_handle(vanity) != target_user:
                    if "userID" in node and "userVanity" in node:
                        candidates.append(
                            ExtractionCandidate(
                                user_id=uid,
                                id_type=IdType.NUMERIC_PROFILE_ID,
                                extraction_method=ExtractionMethod.HTML_JSON_EXTRACTED,
                                extraction_source="html_json:userID",
                                matched_username=vanity,
                                identity_confirmed=False,
                                resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                            )
                        )
                    continue
                elif not vanity and not target_direct:
                    continue

                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.HTML_JSON_EXTRACTED,
                        extraction_source="html_json:userID",
                        matched_username=vanity,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
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
        target_direct = (normalized.direct_id or "").strip()
        candidates: list[ExtractionCandidate] = []

        og_url_tag = soup.find("meta", attrs={"property": "og:url"})
        og_url = str(og_url_tag.get("content") or "") if og_url_tag else ""
        page_vanity = _extract_vanity_from_url(og_url) or _extract_vanity_from_url(final_url)

        # App Links: strictly fb://profile/<id> (explicitly ignore fb://page/<id>)
        for prop in ("al:ios:url", "al:android:url"):
            tag = soup.find("meta", attrs={"property": prop})
            if not tag or not tag.get("content"):
                continue
            content = str(tag["content"]).strip()
            if "fb://page/" in content.lower():
                continue
            m = _RE_FB_PROFILE_URI.search(content)
            if m:
                uid = m.group(1)
                confirmed = bool(
                    (target_direct and uid == target_direct)
                    or (target_user and page_vanity and clean_handle(page_vanity) == target_user)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source=f"meta[property='{prop}']",
                        matched_username=page_vanity,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                    )
                )

        # JSON-LD Person / ProfilePage only (skip Organization / LocalBusiness pages)
        for block in parse_json_ld_blocks(soup):
            btype = str(block.get("@type") or "")
            if btype not in ("Person", "ProfilePage"):
                continue
            entity = block.get("mainEntity") if isinstance(block.get("mainEntity"), dict) else block
            if str(entity.get("@type") or "") not in ("Person", ""):
                continue
            uid = str(entity.get("identifier") or "").strip()
            if _RE_FB_ID.match(uid):
                vanity = str(entity.get("alternateName") or "").strip().lstrip("@") or _extract_vanity_from_url(
                    str(entity.get("url") or "")
                )
                confirmed = bool(
                    (target_direct and uid == target_direct)
                    or (target_user and vanity and clean_handle(vanity) == target_user)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json'].Person",
                        matched_username=vanity,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
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
        target_direct = (normalized.direct_id or "").strip()
        candidates: list[ExtractionCandidate] = []

        canon = soup.find("link", attrs={"rel": "canonical"})
        if canon and canon.get("href"):
            uid = _extract_profile_php_id(str(canon["href"]))
            if uid:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="link[rel='canonical']",
                        identity_confirmed=bool(target_direct and uid == target_direct),
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                    )
                )

        og_url = soup.find("meta", attrs={"property": "og:url"})
        if og_url and og_url.get("content"):
            uid = _extract_profile_php_id(str(og_url["content"]))
            if uid:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="meta[property='og:url']",
                        identity_confirmed=bool(target_direct and uid == target_direct),
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
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

        for m in _RE_USER_VANITY_PAIR_1.finditer(html):
            uid, vanity = m.group(1), m.group(2)
            if not target_user or clean_handle(vanity) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:userID+userVanity",
                        matched_username=vanity,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                    )
                )

        for m in _RE_USER_VANITY_PAIR_2.finditer(html):
            vanity, uid = m.group(1), m.group(2)
            if not target_user or clean_handle(vanity) == target_user:
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:userVanity+userID",
                        matched_username=vanity,
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                    )
                )

        if not candidates:
            for m in _RE_FB_PROFILE_REGEX.finditer(html):
                uid = m.group(1)
                candidates.append(
                    ExtractionCandidate(
                        user_id=uid,
                        id_type=IdType.NUMERIC_PROFILE_ID,
                        extraction_method=ExtractionMethod.REGEX_FALLBACK,
                        extraction_source="regex:fb://profile",
                        identity_confirmed=False,
                        resolved_profile_url=f"https://www.facebook.com/profile.php?id={uid}",
                    )
                )

        return candidates
