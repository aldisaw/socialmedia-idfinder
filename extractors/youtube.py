"""YouTube platform adapter — targets canonical case-sensitive Channel ID (UC...)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from core.http_client import FetchResponse
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

_RE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_RE_CHANNEL_URL = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})(?:[/?#]|$)")
_RE_HANDLE_IN_URL = re.compile(r"youtube\.com/(@[A-Za-z0-9._-]{3,30})(?:[/?#]|$)", re.I)
_RE_FALLBACK_EXTERNAL_ID = re.compile(
    r'"channelMetadataRenderer"\s*:\s*\{[^{}]{0,2000}?"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'
)
_RE_FALLBACK_CANONICAL_CHANNEL = re.compile(
    r'rel=["\']canonical["\'][^>]{0,200}href=["\']https?://(?:www\.|m\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})["\']'
)


def _extract_handle_from_url(url: str) -> str:
    if not url:
        return ""
    m = _RE_HANDLE_IN_URL.search(url)
    return m.group(1) if m else ""


def _extract_channel_id_from_url(url: str) -> str:
    if not url:
        return ""
    m = _RE_CHANNEL_URL.search(url)
    if m:
        return m.group(1)
    parsed = urlsplit(url)
    qs = parse_qs(parsed.query)
    for val in qs.get("channel_id", []):
        if _RE_CHANNEL_ID.match(val):
            return val
    return ""


class YouTubeExtractor(BaseExtractor):
    platform = Platform.YOUTUBE

    def detect_input(self, raw_input: str) -> bool:
        return detect_platform(raw_input) == self.platform

    def normalize_input(self, raw_input: str) -> NormalizedInput:
        return normalize_input(raw_input, selected_platform=self.platform)

    def _find_yt_initial_data(self, html: str) -> dict | None:
        for marker in (
            "var ytInitialData =",
            "window[\"ytInitialData\"] =",
            "window['ytInitialData'] =",
            "ytInitialData =",
        ):
            obj = extract_json_object_after(html, marker)
            if obj is not None:
                return obj
        return None

    def strategy_embedded_json(
        self,
        html: str,
        soup: BeautifulSoup,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        data = self._find_yt_initial_data(html)
        if not data:
            return []

        candidates: list[ExtractionCandidate] = []
        target_handle = clean_handle(normalized.username)
        final_handle = _extract_handle_from_url(final_url)

        # 1a. metadata.channelMetadataRenderer (authoritative target channel metadata)
        meta = (data.get("metadata") or {}).get("channelMetadataRenderer")
        if isinstance(meta, dict):
            ext_id = str(meta.get("externalId") or "").strip()
            vanity = str(meta.get("vanityChannelUrl") or "")
            owner_urls = meta.get("ownerUrls") if isinstance(meta.get("ownerUrls"), list) else []
            found_handle = _extract_handle_from_url(vanity)
            if not found_handle:
                for ou in owner_urls:
                    found_handle = _extract_handle_from_url(str(ou))
                    if found_handle:
                        break
            if _RE_CHANNEL_ID.match(ext_id):
                confirmed = False
                if normalized.direct_id and ext_id == normalized.direct_id:
                    confirmed = True
                elif target_handle and found_handle and clean_handle(found_handle) == target_handle:
                    confirmed = True
                elif target_handle and final_handle and clean_handle(final_handle) == target_handle and not found_handle:
                    confirmed = True
                candidates.append(
                    ExtractionCandidate(
                        user_id=ext_id,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.EMBEDDED_JSON,
                        extraction_source="ytInitialData.metadata.channelMetadataRenderer.externalId",
                        matched_username=found_handle or final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{ext_id}",
                    )
                )

        # 1b. header.c4TabbedHeaderRenderer.channelId
        header = data.get("header") or {}
        if isinstance(header, dict):
            c4 = header.get("c4TabbedHeaderRenderer")
            if isinstance(c4, dict):
                cid = str(c4.get("channelId") or "").strip()
                handle_text = str(c4.get("channelHandleText", {}).get("runs", [{}])[0].get("text", "")) if isinstance(c4.get("channelHandleText"), dict) else ""
                if _RE_CHANNEL_ID.match(cid):
                    confirmed = bool(
                        (normalized.direct_id and cid == normalized.direct_id)
                        or (target_handle and handle_text and clean_handle(handle_text) == target_handle)
                    )
                    candidates.append(
                        ExtractionCandidate(
                            user_id=cid,
                            id_type=IdType.CHANNEL_ID,
                            extraction_method=ExtractionMethod.EMBEDDED_JSON,
                            extraction_source="ytInitialData.header.c4TabbedHeaderRenderer.channelId",
                            matched_username=handle_text or final_handle,
                            identity_confirmed=confirmed,
                            resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
                        )
                    )

        # 1c. microformat.microformatDataRenderer.urlCanonical
        micro = (data.get("microformat") or {}).get("microformatDataRenderer")
        if isinstance(micro, dict):
            url_canon = str(micro.get("urlCanonical") or "")
            cid = _extract_channel_id_from_url(url_canon)
            vanity_mf = str(micro.get("vanityChannelUrl") or "")
            mf_handle = _extract_handle_from_url(vanity_mf)
            if _RE_CHANNEL_ID.match(cid):
                confirmed = bool(
                    (normalized.direct_id and cid == normalized.direct_id)
                    or (target_handle and mf_handle and clean_handle(mf_handle) == target_handle)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=cid,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.EMBEDDED_JSON,
                        extraction_source="ytInitialData.microformat.microformatDataRenderer.urlCanonical",
                        matched_username=mf_handle or final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
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
        candidates: list[ExtractionCandidate] = []
        target_handle = clean_handle(normalized.username)
        final_handle = _extract_handle_from_url(final_url)

        # JSON-LD blocks
        for block in parse_json_ld_blocks(soup):
            url_val = str(block.get("url") or "")
            ident = str(block.get("identifier") or "")
            cid = ident if _RE_CHANNEL_ID.match(ident) else _extract_channel_id_from_url(url_val)
            if _RE_CHANNEL_ID.match(cid):
                alt = str(block.get("alternateName") or "")
                found_handle = alt if alt.startswith("@") else _extract_handle_from_url(url_val)
                confirmed = bool(
                    (normalized.direct_id and cid == normalized.direct_id)
                    or (target_handle and found_handle and clean_handle(found_handle) == target_handle)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=cid,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="script[type='application/ld+json']",
                        matched_username=found_handle or final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
                    )
                )

        # Microdata <meta itemprop="identifier"> / <meta itemprop="channelId">
        for prop in ("identifier", "channelId"):
            tag = soup.find("meta", attrs={"itemprop": prop})
            if tag and tag.get("content"):
                val = str(tag["content"]).strip()
                if _RE_CHANNEL_ID.match(val):
                    confirmed = bool(
                        (normalized.direct_id and val == normalized.direct_id)
                        or (target_handle and final_handle and clean_handle(final_handle) == target_handle)
                    )
                    candidates.append(
                        ExtractionCandidate(
                            user_id=val,
                            id_type=IdType.CHANNEL_ID,
                            extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                            extraction_source=f"meta[itemprop='{prop}']",
                            matched_username=final_handle,
                            identity_confirmed=confirmed,
                            resolved_profile_url=f"https://www.youtube.com/channel/{val}",
                        )
                    )

        # OpenGraph og:url
        og_url = soup.find("meta", attrs={"property": "og:url"})
        if og_url and og_url.get("content"):
            cid = _extract_channel_id_from_url(str(og_url["content"]))
            if _RE_CHANNEL_ID.match(cid):
                confirmed = bool(
                    (normalized.direct_id and cid == normalized.direct_id)
                    or (target_handle and final_handle and clean_handle(final_handle) == target_handle)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=cid,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.STRUCTURED_METADATA,
                        extraction_source="meta[property='og:url']",
                        matched_username=final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
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
        candidates: list[ExtractionCandidate] = []
        target_handle = clean_handle(normalized.username)
        final_handle = _extract_handle_from_url(final_url)

        canon = soup.find("link", attrs={"rel": "canonical"})
        if canon and canon.get("href"):
            cid = _extract_channel_id_from_url(str(canon["href"]))
            if _RE_CHANNEL_ID.match(cid):
                confirmed = bool(
                    (normalized.direct_id and cid == normalized.direct_id)
                    or (target_handle and final_handle and clean_handle(final_handle) == target_handle)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=cid,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="link[rel='canonical']",
                        matched_username=final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
                    )
                )

        rss = soup.find("link", attrs={"rel": "alternate", "type": "application/rss+xml"})
        if rss and rss.get("href"):
            cid = _extract_channel_id_from_url(str(rss["href"]))
            if _RE_CHANNEL_ID.match(cid):
                confirmed = bool(
                    (normalized.direct_id and cid == normalized.direct_id)
                    or (target_handle and final_handle and clean_handle(final_handle) == target_handle)
                )
                candidates.append(
                    ExtractionCandidate(
                        user_id=cid,
                        id_type=IdType.CHANNEL_ID,
                        extraction_method=ExtractionMethod.HTML_METADATA,
                        extraction_source="link[rel='alternate'][type='application/rss+xml']",
                        matched_username=final_handle,
                        identity_confirmed=confirmed,
                        resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
                    )
                )

        return candidates

    def strategy_regex_fallback(
        self,
        html: str,
        normalized: NormalizedInput,
        final_url: str,
    ) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        for source, pat in (
            ("regex:channelMetadataRenderer.externalId", _RE_FALLBACK_EXTERNAL_ID),
            ("regex:canonical_channel_url", _RE_FALLBACK_CANONICAL_CHANNEL),
        ):
            for match in pat.finditer(html):
                cid = match.group(1)
                if _RE_CHANNEL_ID.match(cid):
                    candidates.append(
                        ExtractionCandidate(
                            user_id=cid,
                            id_type=IdType.CHANNEL_ID,
                            extraction_method=ExtractionMethod.REGEX_FALLBACK,
                            extraction_source=source,
                            matched_username=_extract_handle_from_url(final_url),
                            identity_confirmed=False,
                            resolved_profile_url=f"https://www.youtube.com/channel/{cid}",
                        )
                    )
        return candidates

    def fallback_direct_url_candidate(
        self,
        normalized: NormalizedInput,
        fetch: FetchResponse,
    ) -> ExtractionCandidate | None:
        if normalized.direct_id and _RE_CHANNEL_ID.match(normalized.direct_id):
            return ExtractionCandidate(
                user_id=normalized.direct_id,
                id_type=IdType.CHANNEL_ID,
                extraction_method=ExtractionMethod.DIRECT_URL,
                extraction_source="url:/channel/UC...",
                identity_confirmed=False,
                resolved_profile_url=f"https://www.youtube.com/channel/{normalized.direct_id}",
                notes="Channel ID taken from canonical URL; page body lacked embedded metadata confirmation.",
            )
        return None
