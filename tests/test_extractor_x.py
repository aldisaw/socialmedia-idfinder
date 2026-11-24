"""Offline unit tests for X (Twitter) extractor."""

from __future__ import annotations

import httpx

from core.http_client import HttpClient
from core.models import Confidence, ExtractionMethod, IdType, ResultStatus, ResultValidationStatus
from core.normalizer import normalize_input
from extractors.x import XExtractor


def _make_client(handler) -> HttpClient:
    return HttpClient(timeout=5.0, retry_limit=1, transport=httpx.MockTransport(handler))


async def test_x_embedded_initial_state_filters_unrelated_users() -> None:
    html = """
    <html><body><script>
      window.__INITIAL_STATE__ = {
        "entities": {
          "users": {
            "entities": {
              "999999": {"id_str": "999999", "screen_name": "recommended_other"},
              "12": {"id_str": "12", "screen_name": "jack"},
              "888888": {"id_str": "888888", "screen_name": "commenter"}
            }
          }
        }
      };
    </script></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://twitter.com/Jack")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "12"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.confidence == Confidence.HIGH.value


async def test_x_json_ld_profile_page() -> None:
    html = """
    <html><head>
      <script type="application/ld+json" data-testid="UserProfileSchema-test">
        {
          "@context": "https://schema.org",
          "@type": "ProfilePage",
          "mainEntity": {
            "@type": "Person",
            "identifier": "44196397",
            "additionalName": "elonmusk"
          }
        }
      </script>
    </head><body></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/elonmusk")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "44196397"
    assert res.extraction_method == ExtractionMethod.STRUCTURED_METADATA.value
    assert res.confidence == Confidence.HIGH.value


async def test_x_wrong_profile_in_json_ld_rejected() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@type": "ProfilePage", "author": {"identifier": "44196397", "additionalName": "someoneelse"}}
      </script>
    </head><body></body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/jack")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value


async def test_x_js_shell_without_identity_returns_not_found() -> None:
    html = "<html><head><title>X</title></head><body><div id='react-root'>JavaScript is not available.</div></body></html>"
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/openai")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""
    assert res.confidence == Confidence.NOT_VERIFIED.value


async def test_x_regex_fallback_capped_low() -> None:
    html = '<html><body><script>broken_json({"__typename":"User","rest_id":"783214","screen_name":"X"</script></body></html>'
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/X")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "783214"
    assert res.extraction_method == ExtractionMethod.REGEX_FALLBACK.value
    assert res.confidence == Confidence.LOW.value


async def test_x_tsr_relay_escaped_json_extracts_user_and_rejects_tweet_rest_id() -> None:
    import json

    relay_payload = {
        "data": {
            "timeline": {
                "items": [
                    {
                        "__typename": "Tweet",
                        "rest_id": "2123280290281910544",
                        "legacy": {"full_text": "hello world", "screen_name": "X"},
                        "core": {
                            "user_results": {
                                "result": {
                                    "__typename": "User",
                                    "rest_id": "99999999",
                                    "core": {"screen_name": "unrelated_author"},
                                }
                            }
                        },
                    }
                ]
            },
            "user_result_by_screen_name": {
                "result": {
                    "__typename": "User",
                    "rest_id": "783214",
                    "core": {"screen_name": "X"},
                }
            },
        }
    }
    escaped_literal = json.dumps(json.dumps(relay_payload))
    html = f"""
    <html><body>
      <script>(self.$R=self.$R||{{}})["tsr"]=[];self.$_TSR={{}};
        $R[14]={{kind:1,data:{escaped_literal}}};
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/X")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "783214"
    assert res.user_id != "2123280290281910544"
    assert res.id_type == IdType.NUMERIC_USER_ID.value
    assert res.extraction_method == ExtractionMethod.EMBEDDED_JSON.value
    assert res.confidence == Confidence.HIGH.value


async def test_x_tsr_tweet_only_never_returns_tweet_id_as_user_id() -> None:
    import json

    tweet_only_payload = {
        "data": {
            "tweet": {
                "__typename": "Tweet",
                "rest_id": "2123280290281910544",
                "legacy": {"full_text": "hello", "screen_name": "X"},
            }
        }
    }
    escaped_literal = json.dumps(json.dumps(tweet_only_payload))
    html = f'<script>(self.$R=self.$R||{{}})["tsr"]=[];$R[1]={escaped_literal};</script>'
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/X")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.user_id == ""


async def test_x_tsr_profile_object_and_malformed_stream_handling() -> None:
    html = """
    <html><body>
      <script>(self.$R=self.$R||{})["tsr"]=[];
        $R[1]="{\\"broken_escaped_json\\": ";
        $R[5]={i:"#$username#_profile#X{}",s:"success",l:$R[6]={profileStatus:"Available",restId:"783214",screenName:"X"}};
      </script>
    </body></html>
    """
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/X")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.FOUND.value
    assert res.user_id == "783214"
    assert res.extraction_source == "self.$R.profile.restId"
    assert res.confidence == Confidence.HIGH.value


async def test_x_tsr_conflicting_screen_name_rejected() -> None:
    import json

    wrong_payload = {
        "data": {
            "user_result_by_screen_name": {
                "result": {
                    "__typename": "User",
                    "rest_id": "783214",
                    "core": {"screen_name": "other_account"},
                }
            }
        }
    }
    escaped = json.dumps(json.dumps(wrong_payload))
    html = f'<script>(self.$R=self.$R||{{}})["tsr"]=[];$R[1]={escaped};</script>'
    client = _make_client(lambda req: httpx.Response(200, text=html))
    ext = XExtractor()
    norm = normalize_input("https://x.com/X")
    res = ext.validate_result(await ext.extract_user_id(norm, http_client=client), norm)
    await client.aclose()

    assert res.status == ResultStatus.NOT_FOUND.value
    assert res.validation_status == ResultValidationStatus.IDENTITY_MISMATCH.value
