"""Offline unit tests for YouTube extractor (mocked HTTP, zero external calls)."""

from __future__ import annotations

import httpx
import pytest

from core.http_client import HttpClient
from core.models import Confidence, ExtractionMethod, IdType, ResultStatus, ResultValidationStatus
from core.normalizer import normalize_input
from extractors.youtube import YouTubeExtractor


def _make_client(handler) -> HttpClient:
    return HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))


async def test_youtube_embedded_json_handle_high_confidence() -> None:
    html = """
    <html><head><title>Google - YouTube</title></head><body>
    <script>
      var ytInitialData = {
        "metadata": {
          "channelMetadataRenderer": {
            "externalId": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
            "vanityChannelUrl": "http://www.youtube.com/@Google"
          }
        },
        "contents": {
          "recommendations": [{"browseId": "UCOTHER00000000000000001"}]
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@Google/videos")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert res.id_type == IdType.CHANNEL_ID.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.extraction_source == "ytInitialData.metadata.channelMetadataRenderer.externalId"
    assert res.confidence == Confidence.HIGH.value
    assert res.validation_status == ResultValidationStatus.VERIFIED.value


async def test_youtube_ignores_unrelated_recommendation_ids() -> None:
    # No channelMetadataRenderer, only video/playlist/recommendation IDs
    html = """
    <html><body>
    <script>
      var ytInitialData = {
        "contents": {
          "videoId": "dQw4w9WgXcQ",
          "playlistId": "PL12345678901234567890",
          "relatedChannel": {"browseId": "UCUNRELATED0000000000001"}
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@SomeChannel")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.confidence == Confidence.NOT_VERIFIED.value


async def test_youtube_wrong_profile_identity_rejected() -> None:
    html = """
    <html><body><script>
      var ytInitialData = {
        "metadata": {
          "channelMetadataRenderer": {
            "externalId": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
            "vanityChannelUrl": "http://www.youtube.com/@DifferentChannel"
          }
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@Google")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value


async def test_youtube_conflicting_ids_rejected() -> None:
    html = """
    <html><body><script>
      var ytInitialData = {
        "metadata": {
          "channelMetadataRenderer": {
            "externalId": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
            "vanityChannelUrl": "http://www.youtube.com/@Google"
          }
        },
        "header": {
          "c4TabbedHeaderRenderer": {
            "channelId": "UCZZZZZZZZZZZZZZZZZZZZZZ",
            "channelHandleText": {"runs": [{"text": "@Google"}]}
          }
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@Google")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.validation_status == ResultValidationStatus.CONFLICTING_IDS.value


async def test_youtube_malformed_json_falls_back_to_html_attributes() -> None:
    html = """
    <html><head>
      <link rel="canonical" href="https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw" />
    </head><body>
      <script>var ytInitialData = { malformed json !!!</script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@Google")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert res.extraction_method == ExtractionMethod.HTML_METADATA.value
    assert res.confidence == Confidence.HIGH.value


async def test_youtube_regex_fallback_capped_low() -> None:
    # Malformed JSON so Priority 1 fails, no meta/link tags so Priority 2/3 fail, Priority 4 matches
    html = """
    <html><body><script>
      var ytInitialData = {"channelMetadataRenderer":{"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"}, broken
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = YouTubeExtractor()
    norm = normalize_input("https://www.youtube.com/@Google")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert res.extraction_method == ExtractionMethod.REGEX_FALLBACK.value
    assert res.confidence == Confidence.LOW.value


async def test_youtube_channel_url_direct_and_404() -> None:
    norm = normalize_input("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw")
    ext = YouTubeExtractor()

    # 404 must return NOT_FOUND even though URL has channel ID
    client_404 = _make_client(lambda req: httpx.Response(404, text="Not Found"))
    res_404 = ext.validate_result(await ext.extract_user_id(norm, http_client=client_404), norm)
    await client_404.aclose()
    assert res_404.status == ResultStatus.NOT_FOUND.value
    assert res_404.user_id == ""

    # 200 with minimal HTML falls back to DIRECT_URL with MEDIUM confidence
    client_200 = _make_client(lambda req: httpx.Response(200, text="<html><body>ok</body></html>"))
    res_200 = ext.validate_result(await ext.extract_user_id(norm, http_client=client_200), norm)
    await client_200.aclose()
    assert res_200.status == ResultStatus.FOUND.value
    assert res_200.user_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert res_200.extraction_method == ExtractionMethod.DIRECT_URL.value
    assert res_200.confidence == Confidence.MEDIUM.value
