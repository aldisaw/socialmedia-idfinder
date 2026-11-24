"""Platform extractor adapters."""

from extractors.base import BaseExtractor
from extractors.facebook import FacebookExtractor
from extractors.instagram import InstagramExtractor
from extractors.tiktok import TikTokExtractor
from extractors.x import XExtractor
from extractors.youtube import YouTubeExtractor

__all__ = [
    "BaseExtractor",
    "FacebookExtractor",
    "InstagramExtractor",
    "TikTokExtractor",
    "XExtractor",
    "YouTubeExtractor",
]
