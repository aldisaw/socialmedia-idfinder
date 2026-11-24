"""Offline unit tests for TikTok extractor."""

from __future__ import annotations

import httpx

from core.http_client import HttpClient
from core.models import Confidence, ExtractionMethod, IdType, ResultStatus, ResultValidationStatus
from core.normalizer import normalize_input
from extractors.tiktok import TikTokExtractor


def _make_client(handler) -> HttpClient:
    return HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))


async def test_tiktok_universal_data_prefers_numeric_over_sec_uid() -> None:
    html = """
    <html><body>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {
          "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
              "userInfo": {
                "user": {
                  "id": "107955",
                  "uniqueId": "tiktok",
                  "secUid": "MS4wLjABAAAAv7iSuuXDJGDvJkmH_vz124h1234567890abcdef"
                }
              }
            }
          }
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = TikTokExtractor()
    norm = normalize_input("https://www.tiktok.com/@tiktok")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "107955"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.confidence == Confidence.HIGH.value


async def test_tiktok_sec_uid_never_labeled_as_numeric_user_id() -> None:
    html = """
    <html><body>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {
          "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
              "userInfo": {
                "user": {
                  "uniqueId": "charlidamelio",
                  "secUid": "MS4wLjABAAAA-charli-secuid-token-0123456789"
                }
              }
            }
          }
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = TikTokExtractor()
    norm = normalize_input("https://www.tiktok.com/@charlidamelio")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "MS4wLjABAAAA-charli-secuid-token-0123456789"
    assert res.id_type == IdType.SEC_UID.value
    assert res.id_type != IdType.NUMERIC_USER_ID.value
    assert res.confidence == Confidence.HIGH.value


async def test_tiktok_sigi_state_ignores_other_users() -> None:
    html = """
    <html><body>
      <script id="SIGI_STATE" type="application/json">
        {
          "UserModule": {
            "users": {
              "other_user": {"id": "99999999", "uniqueId": "other_user"},
              "khaby.lame": {"id": "127905465618821121", "uniqueId": "khaby.lame"}
            }
          }
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = TikTokExtractor()
    norm = normalize_input("https://www.tiktok.com/@khaby.lame")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "127905465618821121"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.confidence == Confidence.HIGH.value


async def test_tiktok_wrong_profile_rejected() -> None:
    html = """
    <html><body>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {"__DEFAULT_SCOPE__":{"webapp.user-detail":{"userInfo":{"user":{"id":"107955","uniqueId":"wronguser"}}}}}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = TikTokExtractor()
    norm = normalize_input("https://www.tiktok.com/@tiktok")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value


async def test_tiktok_regex_fallback_sec_uid_low_confidence() -> None:
    html = '<html><body><script>broken({"uniqueId":"tiktok","secUid":"MS4wLjABAAAA_fallback_secuid_123456789"</script></body></html>'
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = TikTokExtractor()
    norm = normalize_input("https://www.tiktok.com/@tiktok")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.id_type == IdType.SEC_UID.value
    assert res.extraction_method == ExtractionMethod.REGEX_FALLBACK.value
    assert res.confidence == Confidence.LOW.value
