"""Offline unit tests for Instagram extractor."""

from __future__ import annotations

import httpx

from core.http_client import HttpClient
from core.models import Confidence, ExtractionMethod, IdType, ResultStatus, ResultValidationStatus
from core.normalizer import normalize_input
from extractors.instagram import InstagramExtractor


def _make_client(handler) -> HttpClient:
    return HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))


async def test_instagram_shared_data_high_confidence() -> None:
    html = """
    <html><body><script>
      window._sharedData = {
        "entry_data": {
          "ProfilePage": [
            {"graphql": {"user": {"id": "25025320", "username": "instagram"}}}
          ]
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/instagram/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "25025320"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.confidence == Confidence.HIGH.value


async def test_instagram_polaris_json_filters_unrelated_users() -> None:
    html = """
    <html><body>
      <script type="application/json">
        {
          "suggested": [{"user": {"id": "11111111", "username": "suggested_acct"}}],
          "xdt_api__v1__users__web_profile_info": {
            "user": {"pk": "787132", "username": "natgeo"}
          }
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/NatGeo/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "787132"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.confidence == Confidence.HIGH.value


async def test_instagram_login_wall_distinguished_as_login_required() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/accounts/login" in str(req.url):
            return httpx.Response(200, text="<html><head><title>Login • Instagram</title></head><body></body></html>")
        return httpx.Response(302, headers={"Location": "https://www.instagram.com/accounts/login/?next=/natgeo/"})

    client = _make_client(handler)
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/natgeo/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.LOGIN_REQUIRED.value
    assert res.user_id == ""


async def test_instagram_unavailable_id_vs_error() -> None:
    # Public shell with no user ID -> NOT_FOUND (not ERROR)
    client = _make_client(lambda req: httpx.Response(200, text="<html><head><title>Instagram</title></head><body></body></html>"))
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/natgeo/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.confidence == Confidence.NOT_VERIFIED.value


async def test_instagram_wrong_profile_rejected() -> None:
    html = """
    <html><body>
      <script type="application/json">
        {"profile_id": "99999999", "username": "different_user"}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/natgeo/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value


async def test_instagram_regex_fallback_capped_low() -> None:
    html = '<html><body><script>broken({"profile_id":"787132","username":"natgeo"</script></body></html>'
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = InstagramExtractor()
    norm = normalize_input("https://www.instagram.com/natgeo/")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "787132"
    assert res.extraction_method == ExtractionMethod.REGEX_FALLBACK.value
    assert res.confidence == Confidence.LOW.value
