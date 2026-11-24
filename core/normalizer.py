"""Core input normalization, URL parsing, platform detection, and validation (Phase 2)."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from core.models import InputValidationStatus, NormalizedInput, Platform

# Exact supported hostnames mapped to canonical Platform.
HOST_PLATFORM_MAP: dict[str, Platform] = {
    # Instagram
    "instagram.com": Platform.INSTAGRAM,
    "www.instagram.com": Platform.INSTAGRAM,
    "m.instagram.com": Platform.INSTAGRAM,
    "instagr.am": Platform.INSTAGRAM,
    "www.instagr.am": Platform.INSTAGRAM,
    # TikTok
    "tiktok.com": Platform.TIKTOK,
    "www.tiktok.com": Platform.TIKTOK,
    "m.tiktok.com": Platform.TIKTOK,
    "vm.tiktok.com": Platform.TIKTOK,
    "vt.tiktok.com": Platform.TIKTOK,
    # X (Twitter)
    "x.com": Platform.X,
    "www.x.com": Platform.X,
    "mobile.x.com": Platform.X,
    "m.x.com": Platform.X,
    "twitter.com": Platform.X,
    "www.twitter.com": Platform.X,
    "mobile.twitter.com": Platform.X,
    "m.twitter.com": Platform.X,
    # Facebook
    "facebook.com": Platform.FACEBOOK,
    "www.facebook.com": Platform.FACEBOOK,
    "m.facebook.com": Platform.FACEBOOK,
    "mbasic.facebook.com": Platform.FACEBOOK,
    "web.facebook.com": Platform.FACEBOOK,
    "touch.facebook.com": Platform.FACEBOOK,
    "fb.com": Platform.FACEBOOK,
    "www.fb.com": Platform.FACEBOOK,
    "m.fb.com": Platform.FACEBOOK,
    # YouTube
    "youtube.com": Platform.YOUTUBE,
    "www.youtube.com": Platform.YOUTUBE,
    "m.youtube.com": Platform.YOUTUBE,
    "music.youtube.com": Platform.YOUTUBE,
    "youtu.be": Platform.YOUTUBE,
    "www.youtu.be": Platform.YOUTUBE,
}

_COMMON_TLDS: frozenset[str] = frozenset(
    {
        "com", "net", "org", "io", "co", "tv", "me", "be", "gg", "xyz",
        "app", "dev", "edu", "gov", "uk", "de", "fr", "jp", "id", "in",
        "ca", "au", "br", "ru", "cn", "info", "biz", "site", "online",
        "social", "am", "ly", "gl", "to", "cc", "us", "eu", "ai",
    }
)

_GLOBAL_RESERVED: frozenset[str] = frozenset(
    {
        "settings", "setting", "login", "logout", "signin", "signup", "register",
        "watch", "explore", "search", "share", "sharer", "help", "about",
        "privacy", "terms", "legal", "support", "home", "notifications",
        "messages", "account", "accounts", "admin", "api", "auth", "oauth",
        "download", "apps", "developers", "blog", "press", "jobs", "careers",
        "contact", "cookies", "policy", "policies", "safety", "status", "tos",
        "discover", "trending", "feed", "feeds", "live", "direct", "inbox",
    }
)

_INSTAGRAM_RESERVED: frozenset[str] = _GLOBAL_RESERVED | frozenset(
    {"p", "reel", "reels", "tv", "stories", "challenge", "graphql", "directory", "nametag", "web"}
)
_INSTAGRAM_TABS: frozenset[str] = frozenset({"reels", "tagged", "saved", "channel", "guides"})

_TIKTOK_RESERVED: frozenset[str] = _GLOBAL_RESERVED | frozenset(
    {"foryou", "following", "friends", "music", "tag", "upload", "t", "video", "embed", "business"}
)
_TIKTOK_TABS: frozenset[str] = frozenset({"live", "reposts", "playlist"})

_X_RESERVED: frozenset[str] = _GLOBAL_RESERVED | frozenset(
    {
        "i", "intent", "hashtag", "compose", "who_to_follow", "lists",
        "communities", "bookmarks", "spaces", "premium", "verified", "grok",
    }
)
_X_TABS: frozenset[str] = frozenset(
    {"with_replies", "media", "likes", "highlights", "followers", "following", "articles", "affiliates"}
)

_FACEBOOK_RESERVED: frozenset[str] = _GLOBAL_RESERVED | frozenset(
    {
        "marketplace", "groups", "events", "gaming", "reel", "reels", "stories",
        "sharer.php", "dialog", "plugins", "photo", "photo.php", "photos",
        "permalink.php", "story.php", "videos", "pages", "pg", "hashtag",
        "friends", "bookmarks", "saved", "memories", "login.php", "reg",
        "recover", "tr", "l.php", "flx", "ads", "business", "watch", "public",
    }
)
_FACEBOOK_TABS: frozenset[str] = frozenset(
    {"about", "posts", "photos", "videos", "reels", "friends", "mentions", "followers", "following", "reviews"}
)

_YOUTUBE_RESERVED: frozenset[str] = _GLOBAL_RESERVED | frozenset(
    {
        "shorts", "playlist", "results", "gaming", "redirect", "embed",
        "attribution_link", "paid_memberships", "premium", "clip", "post",
        "podcasts", "courses", "store", "hashtag", "howyoutube", "creators",
    }
)
_YOUTUBE_TABS: frozenset[str] = frozenset(
    {
        "videos", "shorts", "streams", "playlists", "community", "posts",
        "featured", "about", "live", "podcasts", "releases", "courses", "store", "channels",
    }
)

_RE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_RE_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RE_CONTROL_OR_WS = re.compile(r"[\s\x00-\x1f\x7f]")
_RE_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")

_RE_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_RE_TT_USER = re.compile(r"^[A-Za-z0-9._]{2,24}$")
_RE_X_USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_RE_FB_USER = re.compile(r"^[A-Za-z0-9.]{3,50}$")
_RE_FB_NUMERIC_ID = re.compile(r"^[1-9]\d{4,24}$")
_RE_YT_HANDLE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
_RE_YT_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_RE_YT_LEGACY = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


def _clean_raw(raw_input: str | None) -> str:
    if not raw_input:
        return ""
    return _RE_ZERO_WIDTH.sub("", raw_input).strip()


def _is_valid_hostname(host: str) -> bool:
    if not host or len(host) > 253 or "." not in host:
        return False
    low = host.lower().rstrip(".")
    if low == "localhost" or low.endswith(".localhost"):
        return False
    # Reject IPv4/IPv6 literals as domain hostnames (M-06)
    try:
        ipaddress.ip_address(low.strip("[]"))
        return False
    except ValueError:
        pass
    labels = low.split(".")
    if len(labels) < 2:
        return False
    if all(lbl.isdigit() for lbl in labels):
        return False
    return all(_RE_DOMAIN_LABEL.match(lbl) is not None for lbl in labels)


def _extract_host_from_split(parsed: SplitResult) -> str:
    host = (parsed.hostname or "").lower().rstrip(".")
    return host


def _looks_like_url(text: str) -> bool:
    """Determine if cleaned input is intended as a URL rather than a bare username."""
    if not text:
        return False
    if text.startswith("@"):
        return False
    if _RE_SCHEME.match(text) or text.startswith("//"):
        return True
    # Catch non-standard schemes like javascript: or mailto:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        return True

    low = text.lower()
    if "/" in low:
        first_part = low.split("/", 1)[0].split(":", 1)[0]
        if first_part in HOST_PLATFORM_MAP or _is_valid_hostname(first_part):
            return True
        return False

    # No slash and no scheme: check if exact supported host, www.*, or common TLD domain.
    if low in HOST_PLATFORM_MAP:
        return True
    if low.startswith("www.") and _is_valid_hostname(low):
        return True
    if _is_valid_hostname(low):
        tld = low.rsplit(".", 1)[-1]
        if tld in _COMMON_TLDS:
            return True
    return False


def _parse_url(text: str) -> tuple[SplitResult | None, str]:
    """Normalize scheme and parse URL. Returns (SplitResult, error_message)."""
    if _RE_CONTROL_OR_WS.search(text):
        return None, "URL contains whitespace or invalid control characters."

    candidate = text
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif _RE_SCHEME.match(candidate):
        scheme = candidate.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            return None, f"Unsupported URL scheme '{scheme}'. Only http and https are allowed."
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate):
        return None, "Invalid URL format."
    else:
        candidate = "https://" + candidate

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None, "Malformed URL."

    if parsed.username is not None or parsed.password is not None:
        return None, "URLs containing embedded credentials are not allowed."

    try:
        port = parsed.port
    except ValueError:
        return None, "URL contains an invalid port."
    if port is not None and port not in (80, 443):
        return None, f"Unsupported URL port '{port}'. Only standard HTTP(S) ports are allowed."

    host = _extract_host_from_split(parsed)
    if not host or not _is_valid_hostname(host):
        return None, "URL is missing a valid hostname."

    return parsed, ""


def _invalid(
    raw_input: str,
    platform: Platform,
    message: str,
    status: InputValidationStatus = InputValidationStatus.INVALID,
) -> NormalizedInput:
    return NormalizedInput(
        raw_input=raw_input,
        platform=platform,
        validation_status=status,
        is_valid=False,
        error_message=message,
    )


def _path_segments(parsed: SplitResult) -> list[str]:
    """Decode and return non-empty path segments, stripping trailing/duplicate slashes."""
    raw_segs = [seg for seg in parsed.path.split("/") if seg]
    return [unquote(seg) for seg in raw_segs]


def detect_platform(raw_input: str) -> Platform:
    """Detect supported platform strictly from a parsed URL hostname.

    Never guesses a platform from a bare username and never matches arbitrary domains.
    """
    cleaned = _clean_raw(raw_input)
    if not cleaned or not _looks_like_url(cleaned):
        return Platform.UNKNOWN

    parsed, err = _parse_url(cleaned)
    if err or parsed is None:
        return Platform.UNKNOWN

    host = _extract_host_from_split(parsed)
    return HOST_PLATFORM_MAP.get(host, Platform.UNKNOWN)


# ---------------------------------------------------------------------------
# Platform-specific normalizers
# ---------------------------------------------------------------------------

def _validate_instagram_username(user: str) -> str:
    if not _RE_IG_USER.match(user):
        return "Invalid Instagram username format (allowed: letters, digits, '.', '_', 1-30 chars)."
    if user.startswith(".") or user.endswith(".") or ".." in user:
        return "Instagram username cannot start/end with '.' or contain consecutive dots."
    if user.lower() in _INSTAGRAM_RESERVED:
        return f"'{user}' is a reserved Instagram path, not a valid profile username."
    return ""


def normalize_instagram(raw_input: str, parsed: SplitResult | None = None) -> NormalizedInput:
    cleaned = _clean_raw(raw_input)
    if parsed is not None:
        segs = _path_segments(parsed)
        if not segs:
            return _invalid(raw_input, Platform.INSTAGRAM, "Instagram URL is missing a profile username.")
        if segs[0].lower() in _INSTAGRAM_RESERVED:
            return _invalid(raw_input, Platform.INSTAGRAM, f"Instagram URL path '/{segs[0]}' is not a profile.")
        if len(segs) > 2 or (len(segs) == 2 and segs[1].lower() not in _INSTAGRAM_TABS):
            return _invalid(raw_input, Platform.INSTAGRAM, "Instagram URL does not point to a user profile.")
        username = segs[0].lstrip("@") if segs[0].startswith("@") else segs[0]
    else:
        if _RE_CONTROL_OR_WS.search(cleaned) or "/" in cleaned:
            return _invalid(raw_input, Platform.INSTAGRAM, "Invalid Instagram username.")
        username = cleaned[1:] if cleaned.startswith("@") else cleaned

    err = _validate_instagram_username(username)
    if err:
        return _invalid(raw_input, Platform.INSTAGRAM, err)

    return NormalizedInput(
        raw_input=raw_input,
        platform=Platform.INSTAGRAM,
        username=username,
        profile_url=f"https://www.instagram.com/{username}",
        validation_status=InputValidationStatus.VALID,
        is_valid=True,
    )


def _validate_tiktok_username(user: str) -> str:
    if not _RE_TT_USER.match(user):
        return "Invalid TikTok username format (allowed: letters, digits, '.', '_', 2-24 chars)."
    if user.endswith(".") or ".." in user:
        return "TikTok username cannot end with '.' or contain consecutive dots."
    if user.lower() in _TIKTOK_RESERVED:
        return f"'{user}' is a reserved TikTok path, not a valid profile username."
    return ""


def normalize_tiktok(raw_input: str, parsed: SplitResult | None = None) -> NormalizedInput:
    cleaned = _clean_raw(raw_input)
    if parsed is not None:
        host = _extract_host_from_split(parsed)
        if host in ("vm.tiktok.com", "vt.tiktok.com"):
            return _invalid(
                raw_input,
                Platform.TIKTOK,
                "Short TikTok redirect URLs cannot be normalized offline; use a tiktok.com/@username profile URL.",
            )
        segs = _path_segments(parsed)
        if not segs:
            return _invalid(raw_input, Platform.TIKTOK, "TikTok URL is missing a profile username.")
        first = segs[0]
        if first.lower() in _TIKTOK_RESERVED:
            return _invalid(raw_input, Platform.TIKTOK, f"TikTok URL path '/{first}' is not a profile.")
        if not first.startswith("@"):
            return _invalid(raw_input, Platform.TIKTOK, "TikTok profile URLs must use '/@username'.")
        if len(segs) > 2 or (len(segs) == 2 and segs[1].lower() not in _TIKTOK_TABS):
            return _invalid(raw_input, Platform.TIKTOK, "TikTok URL does not point to a user profile.")
        username = first[1:]
    else:
        if _RE_CONTROL_OR_WS.search(cleaned) or "/" in cleaned:
            return _invalid(raw_input, Platform.TIKTOK, "Invalid TikTok username.")
        username = cleaned[1:] if cleaned.startswith("@") else cleaned

    err = _validate_tiktok_username(username)
    if err:
        return _invalid(raw_input, Platform.TIKTOK, err)

    return NormalizedInput(
        raw_input=raw_input,
        platform=Platform.TIKTOK,
        username=username,
        profile_url=f"https://www.tiktok.com/@{username}",
        validation_status=InputValidationStatus.VALID,
        is_valid=True,
    )


def _validate_x_username(user: str) -> str:
    if not _RE_X_USER.match(user):
        return "Invalid X (Twitter) username format (allowed: letters, digits, '_', 1-15 chars)."
    if user.lower() in _X_RESERVED:
        return f"'{user}' is a reserved X path, not a valid profile username."
    return ""


def normalize_x(raw_input: str, parsed: SplitResult | None = None) -> NormalizedInput:
    cleaned = _clean_raw(raw_input)
    if parsed is not None:
        segs = _path_segments(parsed)
        if not segs:
            return _invalid(raw_input, Platform.X, "X (Twitter) URL is missing a profile username.")
        first = segs[0]
        if first.lower() in _X_RESERVED:
            return _invalid(raw_input, Platform.X, f"X (Twitter) URL path '/{first}' is not a profile.")
        if len(segs) > 2 or (len(segs) == 2 and segs[1].lower() not in _X_TABS):
            return _invalid(raw_input, Platform.X, "X (Twitter) URL does not point to a user profile.")
        username = first[1:] if first.startswith("@") else first
    else:
        if _RE_CONTROL_OR_WS.search(cleaned) or "/" in cleaned:
            return _invalid(raw_input, Platform.X, "Invalid X (Twitter) username.")
        username = cleaned[1:] if cleaned.startswith("@") else cleaned

    err = _validate_x_username(username)
    if err:
        return _invalid(raw_input, Platform.X, err)

    return NormalizedInput(
        raw_input=raw_input,
        platform=Platform.X,
        username=username,
        profile_url=f"https://x.com/{username}",
        validation_status=InputValidationStatus.VALID,
        is_valid=True,
    )


def _validate_facebook_username(user: str) -> str:
    if not _RE_FB_USER.match(user):
        return "Invalid Facebook username format (allowed: letters, digits, '.', 3-50 chars)."
    if user.startswith(".") or user.endswith(".") or ".." in user:
        return "Facebook username cannot start/end with '.' or contain consecutive dots."
    if user.lower() in _FACEBOOK_RESERVED:
        return f"'{user}' is a reserved Facebook path, not a valid profile username."
    return ""


def normalize_facebook(raw_input: str, parsed: SplitResult | None = None) -> NormalizedInput:
    cleaned = _clean_raw(raw_input)
    if parsed is not None:
        segs = _path_segments(parsed)
        if not segs:
            return _invalid(raw_input, Platform.FACEBOOK, "Facebook URL is missing a profile identifier.")
        first_low = segs[0].lower()

        # Numeric profile.php?id=...
        if first_low == "profile.php":
            qs = parse_qs(parsed.query)
            ids = qs.get("id", [])
            if not ids or not _RE_FB_NUMERIC_ID.match(ids[0].strip()):
                return _invalid(raw_input, Platform.FACEBOOK, "Facebook profile.php URL requires a valid numeric 'id'.")
            fb_id = ids[0].strip()
            return NormalizedInput(
                raw_input=raw_input,
                platform=Platform.FACEBOOK,
                username="",
                profile_url=f"https://www.facebook.com/profile.php?id={fb_id}",
                direct_id=fb_id,
                validation_status=InputValidationStatus.VALID,
                is_valid=True,
            )

        # /people/<Name>/<numeric_id>
        if first_low == "people":
            if len(segs) >= 3 and _RE_FB_NUMERIC_ID.match(segs[2]):
                fb_id = segs[2]
                return NormalizedInput(
                    raw_input=raw_input,
                    platform=Platform.FACEBOOK,
                    username="",
                    profile_url=f"https://www.facebook.com/profile.php?id={fb_id}",
                    direct_id=fb_id,
                    validation_status=InputValidationStatus.VALID,
                    is_valid=True,
                )
            return _invalid(raw_input, Platform.FACEBOOK, "Invalid Facebook /people/ profile URL.")

        if first_low in _FACEBOOK_RESERVED:
            return _invalid(raw_input, Platform.FACEBOOK, f"Facebook URL path '/{segs[0]}' is not a profile.")
        if len(segs) > 2 or (len(segs) == 2 and segs[1].lower() not in _FACEBOOK_TABS):
            return _invalid(raw_input, Platform.FACEBOOK, "Facebook URL does not point to a user profile.")

        # Numeric profile path: facebook.com/<numeric_id>[/tab]
        if segs[0].isdigit():
            if not _RE_FB_NUMERIC_ID.match(segs[0]):
                return _invalid(
                    raw_input,
                    Platform.FACEBOOK,
                    "Invalid Facebook numeric profile ID in URL path (expected 5-25 digits, no leading zero).",
                )
            fb_id = segs[0]
            return NormalizedInput(
                raw_input=raw_input,
                platform=Platform.FACEBOOK,
                username="",
                profile_url=f"https://www.facebook.com/profile.php?id={fb_id}",
                direct_id=fb_id,
                validation_status=InputValidationStatus.VALID,
                is_valid=True,
            )

        username = segs[0]
    else:
        if _RE_CONTROL_OR_WS.search(cleaned) or "/" in cleaned:
            return _invalid(raw_input, Platform.FACEBOOK, "Invalid Facebook username.")
        username = cleaned[1:] if cleaned.startswith("@") else cleaned

    err = _validate_facebook_username(username)
    if err:
        return _invalid(raw_input, Platform.FACEBOOK, err)

    return NormalizedInput(
        raw_input=raw_input,
        platform=Platform.FACEBOOK,
        username=username,
        profile_url=f"https://www.facebook.com/{username}",
        validation_status=InputValidationStatus.VALID,
        is_valid=True,
    )


def _validate_youtube_handle(handle: str) -> str:
    if not _RE_YT_HANDLE.match(handle):
        return "Invalid YouTube handle format (allowed: letters, digits, '.', '_', '-', 3-30 chars)."
    if handle in (".", "..") or ".." in handle:
        return "YouTube handle cannot contain consecutive dots ('..')."
    if handle.lower() in _YOUTUBE_RESERVED:
        return f"'{handle}' is a reserved YouTube path, not a valid handle."
    return ""


def normalize_youtube(raw_input: str, parsed: SplitResult | None = None) -> NormalizedInput:
    cleaned = _clean_raw(raw_input)
    if parsed is not None:
        host = _extract_host_from_split(parsed)
        if host in ("youtu.be", "www.youtu.be"):
            return _invalid(raw_input, Platform.YOUTUBE, "youtu.be links point to videos, not channel profiles.")
        segs = _path_segments(parsed)
        if not segs:
            return _invalid(raw_input, Platform.YOUTUBE, "YouTube URL is missing a channel or handle.")

        first = segs[0]
        first_low = first.lower()

        # Handle URL: /@Handle[/tab]
        if first.startswith("@"):
            if len(segs) > 2 or (len(segs) == 2 and segs[1].lower() not in _YOUTUBE_TABS):
                return _invalid(raw_input, Platform.YOUTUBE, "YouTube handle URL contains an unsupported subpath.")
            handle = first[1:]
            err = _validate_youtube_handle(handle)
            if err:
                return _invalid(raw_input, Platform.YOUTUBE, err)
            return NormalizedInput(
                raw_input=raw_input,
                platform=Platform.YOUTUBE,
                username=f"@{handle}",
                profile_url=f"https://www.youtube.com/@{handle}",
                validation_status=InputValidationStatus.VALID,
                is_valid=True,
            )

        # Channel ID URL: /channel/UC...[/tab]
        if first_low == "channel":
            if len(segs) < 2:
                return _invalid(raw_input, Platform.YOUTUBE, "YouTube /channel/ URL is missing the channel ID.")
            if len(segs) > 3 or (len(segs) == 3 and segs[2].lower() not in _YOUTUBE_TABS):
                return _invalid(raw_input, Platform.YOUTUBE, "YouTube /channel/ URL contains an unsupported subpath.")
            channel_id = segs[1]
            if not _RE_YT_CHANNEL_ID.match(channel_id):
                return _invalid(
                    raw_input,
                    Platform.YOUTUBE,
                    "Invalid YouTube channel ID (must start with 'UC' and be 24 characters).",
                )
            return NormalizedInput(
                raw_input=raw_input,
                platform=Platform.YOUTUBE,
                username="",
                profile_url=f"https://www.youtube.com/channel/{channel_id}",
                direct_id=channel_id,
                validation_status=InputValidationStatus.VALID,
                is_valid=True,
            )

        # Legacy /c/<custom> or /user/<legacy>
        if first_low in ("c", "user"):
            if len(segs) < 2:
                return _invalid(raw_input, Platform.YOUTUBE, f"YouTube /{first_low}/ URL is missing an identifier.")
            if len(segs) > 3 or (len(segs) == 3 and segs[2].lower() not in _YOUTUBE_TABS):
                return _invalid(raw_input, Platform.YOUTUBE, f"YouTube /{first_low}/ URL has an unsupported subpath.")
            ident = segs[1]
            if (
                not _RE_YT_LEGACY.match(ident)
                or ident in (".", "..")
                or ".." in ident
                or ident.lower() in _YOUTUBE_RESERVED
            ):
                return _invalid(raw_input, Platform.YOUTUBE, f"Invalid YouTube /{first_low}/ identifier.")
            return NormalizedInput(
                raw_input=raw_input,
                platform=Platform.YOUTUBE,
                username=ident,
                profile_url=f"https://www.youtube.com/{first_low}/{ident}",
                validation_status=InputValidationStatus.VALID,
                is_valid=True,
                canonical_key=f"youtube:{first_low}:{ident.lower()}",
            )

        if first_low in _YOUTUBE_RESERVED:
            return _invalid(raw_input, Platform.YOUTUBE, f"YouTube URL path '/{first}' is not a channel profile.")
        return _invalid(
            raw_input,
            Platform.YOUTUBE,
            "Unsupported YouTube profile URL format. Use /@handle, /channel/UC..., /c/name, or /user/name.",
        )

    # Plain username or channel ID input with YouTube selected
    if _RE_CONTROL_OR_WS.search(cleaned) or "/" in cleaned:
        return _invalid(raw_input, Platform.YOUTUBE, "Invalid YouTube handle or channel ID.")

    if _RE_YT_CHANNEL_ID.match(cleaned):
        return NormalizedInput(
            raw_input=raw_input,
            platform=Platform.YOUTUBE,
            username="",
            profile_url=f"https://www.youtube.com/channel/{cleaned}",
            direct_id=cleaned,
            validation_status=InputValidationStatus.VALID,
            is_valid=True,
        )

    handle = cleaned[1:] if cleaned.startswith("@") else cleaned
    err = _validate_youtube_handle(handle)
    if err:
        return _invalid(raw_input, Platform.YOUTUBE, err)

    return NormalizedInput(
        raw_input=raw_input,
        platform=Platform.YOUTUBE,
        username=f"@{handle}",
        profile_url=f"https://www.youtube.com/@{handle}",
        validation_status=InputValidationStatus.VALID,
        is_valid=True,
    )


_PLATFORM_NORMALIZERS = {
    Platform.INSTAGRAM: normalize_instagram,
    Platform.TIKTOK: normalize_tiktok,
    Platform.X: normalize_x,
    Platform.FACEBOOK: normalize_facebook,
    Platform.YOUTUBE: normalize_youtube,
}


def normalize_input(raw_input: str, selected_platform: Platform | None = None) -> NormalizedInput:
    """Normalize and validate a profile URL or username offline."""
    cleaned = _clean_raw(raw_input)
    if not cleaned:
        return _invalid(
            raw_input or "",
            selected_platform or Platform.UNKNOWN,
            "Input cannot be empty.",
            status=InputValidationStatus.INVALID,
        )

    if _looks_like_url(cleaned):
        parsed, parse_err = _parse_url(cleaned)
        if parse_err or parsed is None:
            return _invalid(raw_input, Platform.UNKNOWN, parse_err or "Invalid URL.")

        host = _extract_host_from_split(parsed)
        detected = HOST_PLATFORM_MAP.get(host, Platform.UNKNOWN)
        if detected == Platform.UNKNOWN:
            return _invalid(
                raw_input,
                Platform.UNKNOWN,
                f"Unsupported platform domain '{host}'.",
                status=InputValidationStatus.UNSUPPORTED_PLATFORM,
            )

        return _PLATFORM_NORMALIZERS[detected](raw_input, parsed)

    # Not a URL: reject obvious malformed paths or whitespace before platform check.
    if _RE_CONTROL_OR_WS.search(cleaned):
        return _invalid(
            raw_input,
            selected_platform or Platform.UNKNOWN,
            "Input contains whitespace or invalid control characters.",
            status=InputValidationStatus.INVALID,
        )
    if "/" in cleaned:
        return _invalid(
            raw_input,
            selected_platform or Platform.UNKNOWN,
            "Invalid profile input. Provide a plain username or a full profile URL.",
            status=InputValidationStatus.INVALID,
        )
    if not re.match(r"^@?[A-Za-z0-9._-]+$", cleaned):
        return _invalid(
            raw_input,
            selected_platform or Platform.UNKNOWN,
            "Input contains unsupported or non-ASCII characters.",
            status=InputValidationStatus.INVALID,
        )

    # Bare username without an explicitly selected platform MUST return AMBIGUOUS_INPUT.
    if selected_platform is None or selected_platform == Platform.UNKNOWN:
        return _invalid(
            raw_input,
            Platform.UNKNOWN,
            "Bare username requires explicit platform selection (or provide a full profile URL).",
            status=InputValidationStatus.AMBIGUOUS_INPUT,
        )

    normalizer = _PLATFORM_NORMALIZERS.get(selected_platform)
    if normalizer is None:
        return _invalid(
            raw_input,
            selected_platform,
            f"Unsupported platform: {selected_platform.value}",
            status=InputValidationStatus.UNSUPPORTED_PLATFORM,
        )

    return normalizer(raw_input, None)


def normalize_batch(
    raw_entries: list[str],
    selected_platform: Platform | None = None,
) -> tuple[list[NormalizedInput], int]:
    """Normalize a list of inputs and deduplicate by canonical normalized identity.

    Returns (unique_items, duplicate_count).
    """
    seen_keys: set[str] = set()
    unique: list[NormalizedInput] = []
    duplicates = 0

    for entry in raw_entries:
        cleaned = _clean_raw(entry)
        if not cleaned:
            continue
        item = normalize_input(entry, selected_platform=selected_platform)
        dedup_key = item.canonical_key if item.is_valid and item.canonical_key else f"raw:{cleaned.lower()}"
        if dedup_key in seen_keys:
            duplicates += 1
            continue
        seen_keys.add(dedup_key)
        unique.append(item)

    return unique, duplicates
