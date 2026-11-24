"""Core package for Social ID Finder."""

from core.models import (
    SUPPORTED_PLATFORMS,
    AcquisitionMethod,
    Confidence,
    ExtractionCandidate,
    ExtractionMethod,
    IdType,
    InputValidationStatus,
    NormalizedInput,
    Platform,
    ResultStatus,
    ResultValidationStatus,
    SearchResult,
)

__all__ = [
    "AcquisitionMethod",
    "Confidence",
    "ExtractionCandidate",
    "ExtractionMethod",
    "IdType",
    "InputValidationStatus",
    "NormalizedInput",
    "Platform",
    "ResultStatus",
    "ResultValidationStatus",
    "SUPPORTED_PLATFORMS",
    "SearchResult",
]
