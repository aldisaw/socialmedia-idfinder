"""Comprehensive offline unit tests for Facebook extractor (URL, root selectedID, Playwright fallback, restrictions)."""

from __future__ import annotations

import httpx

from config import AppSettings
from core.browser_client import BrowserClient
from core.engine import ExtractionEngine
from core.http_client import HttpClient
from core.models import (
    AcquisitionMethod,
    Confidence,
    ExtractionMethod,
    IdType,
    InputValidationStatus,
    ResultStatus,
    ResultValidationStatus,
)
from core.normalizer import normalize_input
from extractors.facebook import FacebookExtractor


def _make_client(handler) -> HttpClient:
    return HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))


class _MockPage:
    def __init__(self, html: str, final_url: str, status: int = 200) -> None:
        self._html = html
        self.url = final_url
        self._status = status

    def on(self, _ev: str, _cb) -> None:
        pass

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 15000):
        class _Resp:
            def __init__(self, s: int) -> None:
                self.status = s

        return _Resp(self._status)

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        pass


class _MockPlaywright:
    def __init__(self, page: _MockPage) -> None:
        self._page = page
        self.chromium = self

    async def start(self):
        return self

    async def stop(self) -> None:
        pass

    async def launch(self, headless: bool = True):
        return self

    async def new_context(self, **kwargs):
        return self

    async def new_page(self):
        return self._page

    async def close(self) -> None:
        pass


def _make_mock_browser(html: str, final_url: str, status: int = 200) -> BrowserClient:
    page = _MockPage(html=html, final_url=final_url, status=status)
    return BrowserClient(timeout=5.0, render_wait=0.0, playwright_factory=lambda: _MockPlaywright(page))


# ---------------------------------------------------------------------------
# 1. URL-based extraction: profile.php?id & numeric path (not page-verified)
# ---------------------------------------------------------------------------

async def test_facebook_profile_php_numeric_url_extracted_not_page_verified() -> None:
    norm = normalize_input("https://m.facebook.com/profile.php?id=100012345678901&ref=share")
    assert norm.is_valid and norm.direct_id == "100012345678901"

    ext = FacebookExtractor()
    # Must extract directly from URL without claiming page-verified HIGH confidence
    res = ext.validate_result(await ext.extract_user_id(norm), norm)

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100012345678901"
    assert res.id_type == IdType.NUMERIC_PROFILE_ID.value
    assert res.acquisition_method == AcquisitionMethod.NONE.value
    assert res.extraction_method == ExtractionMethod.URL_EXTRACTED.value
    assert res.extraction_source == "url:profile.php?id"
    assert res.final_url == "https://www.facebook.com/profile.php?id=100012345678901"
    assert res.confidence == Confidence.MEDIUM.value
    assert res.validation_status == ResultValidationStatus.PARTIALLY_VERIFIED.value


async def test_facebook_numeric_path_url_extracted_not_page_verified() -> None:
    norm = normalize_input("https://www.facebook.com/100098765432100/about?ref=bookmarks")
    assert norm.is_valid and norm.direct_id == "100098765432100"

    ext = FacebookExtractor()
    res = ext.validate_result(await ext.extract_user_id(norm), norm)

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100098765432100"
    assert res.id_type == IdType.NUMERIC_PROFILE_ID.value
    assert res.extraction_method == ExtractionMethod.URL_EXTRACTED.value
    assert res.extraction_source == "url:numeric_path"
    assert res.confidence == Confidence.MEDIUM.value
    assert res.validation_status == ResultValidationStatus.PARTIALLY_VERIFIED.value


# ---------------------------------------------------------------------------
# 2. Root-profile selectedID & userID (HTTPX & Playwright Browser fallback)
# ---------------------------------------------------------------------------

async def test_facebook_vanity_profile_with_root_selected_id_http() -> None:
    html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/jane.doe" />
    </head><body>
      <script type="application/json">
        {"require": [["ProfileCometHeader", {"selectedID": "100012345678901", "userVanity": "jane.doe"}]]}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100012345678901"
    assert res.id_type == IdType.NUMERIC_PROFILE_ID.value
    assert res.acquisition_method == AcquisitionMethod.HTTP.value
    assert res.extraction_method == ExtractionMethod.HTML_JSON_EXTRACTED.value
    assert res.extraction_source == "html_json:selectedID"
    assert res.response_status == 200
    assert res.final_url == "https://www.facebook.com/jane.doe"
    assert res.confidence == Confidence.HIGH.value
    assert res.validation_status == ResultValidationStatus.VERIFIED.value


async def test_facebook_vanity_falls_back_to_playwright_browser_extracted() -> None:
    # HTTPX hits Facebook's /login/ 400 redirect; Playwright renders public profile with selectedID
    def http_handler(req: httpx.Request) -> httpx.Response:
        if "/login/" in str(req.url):
            return httpx.Response(400, text="<html><title>Error</title><body>Error</body></html>")
        return httpx.Response(302, headers={"Location": "https://www.facebook.com/login/?next=https://www.facebook.com/jane.doe"})

    rendered_html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/jane.doe" />
    </head><body>
      <script type="application/json">
        {"ProfileCometHeader": {"selectedID": "100012345678901", "userVanity": "jane.doe"}}
      </script>
    </body></html>
    """
    hc = _make_client(http_handler)
    bc = _make_mock_browser(rendered_html, final_url="https://www.facebook.com/jane.doe", status=200)
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)

    async with ExtractionEngine(settings=settings, http_client=hc, browser_client=bc) as eng:
        res = await eng.extract(normalize_input("https://www.facebook.com/jane.doe"))
    await hc.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100012345678901"
    assert res.acquisition_method == AcquisitionMethod.BROWSER.value
    assert res.extraction_method == ExtractionMethod.BROWSER_EXTRACTED.value
    assert res.extraction_source == "browser_dom:html_json:selectedID"
    assert res.response_status == 200
    assert res.final_url == "https://www.facebook.com/jane.doe"
    assert res.confidence == Confidence.HIGH.value


async def test_facebook_vanity_direct_http_400_falls_back_to_playwright() -> None:
    # Facebook often rejects raw HTTPX requests with direct HTTP 400 (no /login/ redirect)
    hc = _make_client(lambda req: httpx.Response(400, text="<html><title>Error</title><body>Error</body></html>"))
    rendered_html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/zuck" />
    </head><body>
      <script type="application/json">
        {"ProfileCometHeaderQuery": {"selectedID": "4", "selectedSpaceType": "profile"}}
      </script>
    </body></html>
    """
    bc = _make_mock_browser(rendered_html, final_url="https://www.facebook.com/zuck", status=200)
    settings = AppSettings(enable_browser_fallback=True, retry_limit=0)

    async with ExtractionEngine(settings=settings, http_client=hc, browser_client=bc) as eng:
        res = await eng.extract(normalize_input("https://www.facebook.com/zuck"))
    await hc.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "4"
    assert res.acquisition_method == AcquisitionMethod.BROWSER.value
    assert res.extraction_method == ExtractionMethod.BROWSER_EXTRACTED.value
    assert res.confidence == Confidence.HIGH.value


async def test_facebook_embedded_json_personal_profile_high() -> None:
    html = """
    <html><body>
      <script type="application/json">
        {"require": [["ProfileLoader", {"userID": "100012345678901", "userVanity": "jane.doe", "__typename": "User"}]]}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100012345678901"
    assert res.id_type == IdType.NUMERIC_PROFILE_ID.value
    assert res.extraction_method == ExtractionMethod.HTML_JSON_EXTRACTED.value
    assert res.confidence == Confidence.HIGH.value


# ---------------------------------------------------------------------------
# 3. Strict context rejection: posts, photos, comments, Page IDs, malformed, conflicts
# ---------------------------------------------------------------------------

async def test_facebook_selected_id_from_post_photo_comment_rejected() -> None:
    html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/jane.doe" />
    </head><body>
      <script type="application/json">
        {
          "posts": [{"selectedID": "100099999999901", "post_id": "9876543210"}],
          "photos": {"selectedID": "100099999999902"},
          "comments": [{"selectedID": "100099999999903", "userVanity": "jane.doe"}],
          "timeline_list_feed_units": {"edges": [{"node": {"__typename": "Story", "selectedID": "100099999999904"}}]},
          "feedback": {"selectedID": "100099999999905"}
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.confidence == Confidence.NOT_VERIFIED.value


async def test_facebook_does_not_confuse_page_id_with_personal_profile_id() -> None:
    html = """
    <html><head>
      <meta property="al:ios:url" content="fb://page/20531316728" />
      <meta property="al:android:url" content="fb://page/20531316728" />
    </head><body>
      <script type="application/json">
        {"page": {"__typename": "Page", "pageID": "20531316728", "selectedID": "20531316728", "userVanity": "meta"}}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/meta")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""


async def test_facebook_malformed_ids_rejected() -> None:
    # Leading-zero and too-short URL IDs rejected at normalization
    bad_url_1 = normalize_input("https://www.facebook.com/profile.php?id=00012345")
    bad_url_2 = normalize_input("https://www.facebook.com/00012345")
    bad_url_3 = normalize_input("https://www.facebook.com/123")
    assert bad_url_1.validation_status == InputValidationStatus.INVALID
    assert bad_url_2.validation_status == InputValidationStatus.INVALID
    assert bad_url_3.validation_status == InputValidationStatus.INVALID

    # Malformed selectedID in HTML JSON rejected
    html = """
    <html><body>
      <script type="application/json">
        {"ProfileCometHeader": {"selectedID": "00012345", "userVanity": "jane.doe"}}
      </script>
      <script type="application/json">
        {"ProfileCometHeader": {"selectedID": "not_numeric", "userVanity": "jane.doe"}}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""


async def test_facebook_conflicting_profile_ids_rejected() -> None:
    html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/jane.doe" />
    </head><body>
      <script type="application/json">
        {
          "ProfileCometHeader": {"selectedID": "100011111111111", "userVanity": "jane.doe"},
          "ProfileOwner": {"userID": "100022222222222", "userVanity": "jane.doe"}
        }
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.validation_status == ResultValidationStatus.CONFLICTING_IDS.value
    assert res.confidence == Confidence.NOT_VERIFIED.value


# ---------------------------------------------------------------------------
# 4. Login redirects, CAPTCHA, app links, unrelated HTTP 400, wrong vanity
# ---------------------------------------------------------------------------

async def test_facebook_app_link_profile_meta_high() -> None:
    html = """
    <html><head>
      <meta property="og:url" content="https://www.facebook.com/zuck" />
      <meta property="al:ios:url" content="fb://profile/100000000000004" />
    </head><body></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/zuck")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "100000000000004"
    assert res.extraction_method == ExtractionMethod.STRUCTURED_METADATA.value
    assert res.confidence == Confidence.HIGH.value


async def test_facebook_login_redirect_reports_login_required() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "login.php" in str(req.url):
            return httpx.Response(200, text="<html><head><title>Log in to Facebook</title></head><body></body></html>")
        return httpx.Response(302, headers={"Location": "https://www.facebook.com/login.php?next=https://www.facebook.com/zuck"})

    client = _make_client(handler)
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/zuck")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.LOGIN_REQUIRED.value
    assert res.user_id == ""
    assert res.response_status == 200
    assert "login.php" in res.final_url


async def test_facebook_login_redirect_with_http_400_reports_login_required() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/login/" in str(req.url):
            return httpx.Response(400, text="<html><head><title>Error</title></head><body>Sorry, something went wrong.</body></html>")
        return httpx.Response(302, headers={"Location": "https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Fzuck"})

    client = _make_client(handler)
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/zuck")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.LOGIN_REQUIRED.value
    assert res.user_id == ""
    assert res.response_status == 400
    assert "/login/" in res.final_url


async def test_facebook_captcha_reports_captcha_required() -> None:
    client = _make_client(
        lambda req: httpx.Response(
            200,
            text="<html><head><title>Security Check Required</title></head><body>Please verify you are human captcha-verify</body></html>",
        )
    )
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/zuck")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.CAPTCHA_REQUIRED.value
    assert res.user_id == ""
    assert res.confidence == Confidence.NOT_VERIFIED.value


async def test_facebook_unrelated_http_400_remains_error() -> None:
    client = _make_client(
        lambda req: httpx.Response(400, text="<html><head><title>Bad Request</title></head><body>Malformed query</body></html>")
    )
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/zuck")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.ERROR.value
    assert res.response_status == 400
    assert res.user_id == ""


async def test_facebook_wrong_vanity_rejected() -> None:
    html = """
    <html><body>
      <script type="application/json">
        {"userID": "100012345678901", "userVanity": "other.person"}
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = FacebookExtractor()
    norm = normalize_input("https://www.facebook.com/jane.doe")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value
