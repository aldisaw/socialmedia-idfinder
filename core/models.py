"""Core domain models and schemas for Social ID Finder."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    X = "x"
    FACEBOOK = "facebook"
    YOUTUBE = "youtube"
    UNKNOWN = "unknown"

    @property
    def display_name(self) -> str:
        return {
            Platform.INSTAGRAM: "Instagram",
            Platform.TIKTOK: "TikTok",
            Platform.X: "X (Twitter)",
            Platform.FACEBOOK: "Facebook",
            Platform.YOUTUBE: "YouTube",
            Platform.UNKNOWN: "Unknown",
        }[self]


SUPPORTED_PLATFORMS: tuple[Platform, ...] = (
    Platform.INSTAGRAM,
    Platform.TIKTOK,
    Platform.X,
    Platform.FACEBOOK,
    Platform.YOUTUBE,
)


class AcquisitionMethod(str, Enum):
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    NONE = "NONE"


class InputValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    AMBIGUOUS_INPUT = "AMBIGUOUS_INPUT"


class IdType(str, Enum):
    NUMERIC_USER_ID = "NUMERIC_USER_ID"
    NUMERIC_PROFILE_ID = "NUMERIC_PROFILE_ID"
    CHANNEL_ID = "CHANNEL_ID"
    SEC_UID = "SEC_UID"
    UNKNOWN = "UNKNOWN"


class ResultValidationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CONFLICTING_IDS = "CONFLICTING_IDS"
    INVALID_INPUT = "INVALID_INPUT"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NOT_VERIFIED = "NOT_VERIFIED"


class ResultStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED = "BLOCKED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INVALID_INPUT = "INVALID_INPUT"
    ERROR = "ERROR"


class ExtractionMethod(str, Enum):
    EMBEDDED_JSON = "EMBEDDED_JSON"
    HTML_JSON_EXTRACTED = "HTML_JSON_EXTRACTED"
    BROWSER_EXTRACTED = "BROWSER_EXTRACTED"
    STRUCTURED_METADATA = "STRUCTURED_METADATA"
    HTML_METADATA = "HTML_METADATA"
    REGEX_FALLBACK = "REGEX_FALLBACK"
    DIRECT_URL = "DIRECT_URL"
    URL_EXTRACTED = "URL_EXTRACTED"
    NONE = "NONE"


@dataclass
class NormalizedInput:
    """Canonicalized representation of a user input entry."""

    raw_input: str
    platform: Platform
    username: str = ""
    profile_url: str = ""
    direct_id: str | None = None
    validation_status: InputValidationStatus = InputValidationStatus.VALID
    is_valid: bool = True
    error_message: str = ""
    canonical_key: str = ""

    def __post_init__(self) -> None:
        if not self.is_valid and self.validation_status == InputValidationStatus.VALID:
            self.validation_status = InputValidationStatus.INVALID
        elif self.validation_status != InputValidationStatus.VALID:
            self.is_valid = False

        if self.is_valid and not self.canonical_key:
            if self.platform == Platform.YOUTUBE and self.direct_id:
                self.canonical_key = f"{self.platform.value}:channel:{self.direct_id}"
            elif self.direct_id:
                self.canonical_key = f"{self.platform.value}:id:{self.direct_id}"
            elif self.username:
                self.canonical_key = f"{self.platform.value}:{self.username.lower()}"
            elif self.profile_url:
                self.canonical_key = f"{self.platform.value}:{self.profile_url.lower()}"


@dataclass
class ExtractionCandidate:
    """Single candidate identifier produced by an extraction strategy."""

    user_id: str
    id_type: IdType
    extraction_method: ExtractionMethod
    extraction_source: str
    matched_username: str = ""
    identity_confirmed: bool = False
    resolved_profile_url: str = ""
    notes: str = ""


@dataclass
class SearchResult:
    """Standardized extraction result schema with unified acquisition & diagnostic evidence."""

    platform: str
    input: str
    username: str = ""
    profile_url: str = ""
    user_id: str = ""
    id_type: str = IdType.UNKNOWN.value
    acquisition_method: str = AcquisitionMethod.HTTP.value
    extraction_method: str = ExtractionMethod.NONE.value
    extraction_source: str = ""
    response_status: int = 0
    final_url: str = ""
    confidence: str = Confidence.NOT_VERIFIED.value
    validation_status: str = ResultValidationStatus.UNVERIFIED.value
    status: str = ResultStatus.NOT_FOUND.value
    response_time: float = 0.0
    error_message: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
