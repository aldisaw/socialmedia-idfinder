"""Optional Method B browser acquisition client using Playwright Chromium with SSRF & concurrency hardening.

Public profile pages only:
- No login, no imported browser cookies, no stealth/anti-bot evasion, no CAPTCHA bypass.
- Strict context-level URL/DNS/IP route interception (blocks private/internal IPs and non-HTTP(S) schemes).
- Downloads disabled, service workers blocked, popups closed immediately, unnecessary heavy assets blocked.
- Bounded browser concurrency (max 2 simultaneous Chromium instances) and cancellation-safe cleanup.
- Gracefully disabled when Playwright or Chromium is unavailable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import DEFAULT_REQUEST_TIMEOUT, USER_AGENT, get_resource_path
from core.http_client import FetchResponse
from core.url_security import DnsResolver, UrlSecurityError, validate_public_url

logger = logging.getLogger("social_id_finder.browser")

DEFAULT_RENDER_WAIT_SECONDS = 2.5
MAX_BROWSER_CONCURRENCY = 2
_CLEANUP_STEP_TIMEOUT_SECONDS = 3.0
_BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"image", "media", "font"})

# Per-event-loop semaphore capping concurrent Chromium launches at MAX_BROWSER_CONCURRENCY (M-02)
_LOOP_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _get_browser_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _LOOP_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_BROWSER_CONCURRENCY)
        _LOOP_SEMAPHORES[loop] = sem
    return sem


async def _safe_close_step(obj: Any, method: str) -> None:
    """Shielded, bounded cleanup helper that survives outer task cancellation (M-02)."""
    if obj is None:
        return
    fn = getattr(obj, method, None)
    if fn is None:
        return
    try:
        coro = fn()
        if asyncio.iscoroutine(coro):
            await asyncio.shield(asyncio.wait_for(coro, timeout=_CLEANUP_STEP_TIMEOUT_SECONDS))
    except BaseException:
        # Never allow cancellation or cleanup errors to abort remaining browser/driver shutdown steps
        pass


@dataclass(frozen=True)
class BrowserCapability:
    """Runtime availability report for optional Playwright Chromium support."""

    status: str  # "ENABLED" or "DISABLED"
    available: bool
    reason: str
    executable_path: str = ""


def _find_chromium_executable() -> str:
    """Locate bundled or Playwright-managed Chromium binary without launching a browser."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return ""

    candidates: list[Path] = []
    # 1. Bundled runtime inside frozen Browser Edition (_MEIPASS/ms-playwright)
    bundled = get_resource_path("ms-playwright")
    if bundled.exists():
        candidates.append(bundled)

    # 2. Explicit PLAYWRIGHT_BROWSERS_PATH or OS user Playwright cache
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path and env_path != "0":
        candidates.append(Path(env_path))
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")

    for base in candidates:
        if not base.exists():
            continue
        for folder in sorted(base.glob("chromium_headless_shell-*"), reverse=True):
            for rel in (
                "chrome-headless-shell-win64/chrome-headless-shell.exe",
                "chrome-win/headless_shell.exe",
                "chrome-headless-shell-linux64/chrome-headless-shell",
            ):
                exe = folder / rel
                if exe.exists():
                    return str(exe)
        for folder in sorted(base.glob("chromium-*"), reverse=True):
            for rel in (
                "chrome-win64/chrome.exe",
                "chrome-win/chrome.exe",
                "chrome-linux/chrome",
            ):
                exe = folder / rel
                if exe.exists():
                    return str(exe)
    return ""


def check_browser_capability(
    *,
    _has_playwright: bool | None = None,
    _chromium_path: str | None = None,
) -> BrowserCapability:
    """Check whether optional Playwright + Chromium runtime is available."""
    has_pw = (
        _has_playwright
        if _has_playwright is not None
        else (importlib.util.find_spec("playwright") is not None)
    )
    if not has_pw:
        return BrowserCapability(
            status="DISABLED",
            available=False,
            reason=(
                "Playwright is not installed (optional). "
                "Install in .venv: pip install playwright && playwright install chromium"
            ),
        )

    exe_path = _chromium_path if _chromium_path is not None else _find_chromium_executable()
    if not exe_path:
        return BrowserCapability(
            status="DISABLED",
            available=False,
            reason=(
                "Playwright is installed, but Chromium browser runtime is missing. "
                "Run: playwright install chromium"
            ),
        )

    return BrowserCapability(
        status="ENABLED",
        available=True,
        reason="Playwright Chromium available.",
        executable_path=exe_path,
    )


class BrowserClient:
    """Async Playwright Chromium reader with SSRF route blocking, bounded concurrency, and safe cleanup."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        render_wait: float = DEFAULT_RENDER_WAIT_SECONDS,
        user_agent: str = USER_AGENT,
        playwright_factory: Callable[[], Any] | None = None,
        capability: BrowserCapability | None = None,
        dns_resolver: DnsResolver | None = None,
        max_html_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.timeout = max(0.1, float(timeout))
        self.render_wait = max(0.0, min(float(render_wait), 8.0))
        self.user_agent = user_agent
        self._playwright_factory = playwright_factory
        self._capability = capability
        self._dns_resolver = dns_resolver
        self.max_html_bytes = max(1024, int(max_html_bytes))

    def get_capability(self) -> BrowserCapability:
        if self._capability is not None:
            return self._capability
        return check_browser_capability()

    async def _validate_browser_url(self, target_url: str) -> None:
        should_resolve = (self._playwright_factory is None) or (self._dns_resolver is not None)
        if should_resolve:
            await asyncio.to_thread(
                validate_public_url,
                target_url,
                resolve_dns=True,
                resolver=self._dns_resolver,
            )
        else:
            validate_public_url(target_url, resolve_dns=False)

    async def fetch_rendered(
        self,
        url: str,
        *,
        timeout: float | None = None,
        wait_selector: str | None = None,
    ) -> FetchResponse:
        """Load a public URL in an isolated stateless Chromium context with SSRF route blocking."""
        nav_timeout = max(0.1, float(timeout)) if timeout is not None else self.timeout

        # Pre-flight SSRF validation BEFORE launching Chromium (H-02)
        try:
            await self._validate_browser_url(url)
        except UrlSecurityError as exc:
            logger.warning("Blocked unsafe browser URL %s: %s", url, exc)
            return FetchResponse(
                url=exc.url or url,
                status_code=0,
                error_type="SSRF_BLOCKED",
                error_message=str(exc),
            )

        launch_kwargs: dict[str, Any] = {"headless": True}
        if self._playwright_factory is None:
            cap = self.get_capability()
            if not cap.available:
                return FetchResponse(
                    url=url,
                    status_code=0,
                    error_type="BROWSER_UNAVAILABLE",
                    error_message=cap.reason,
                )
            if cap.executable_path and getattr(sys, "_MEIPASS", None):
                launch_kwargs["executable_path"] = cap.executable_path
            from playwright.async_api import async_playwright

            pw_cm = async_playwright()
        else:
            pw_cm = self._playwright_factory()

        semaphore = _get_browser_semaphore()
        async with semaphore:
            pw = None
            browser = None
            context = None
            page = None
            popup_pages: list[Any] = []
            blocked_nav_error: list[str] = []

            try:
                pw = await pw_cm.start()
                browser = await pw.chromium.launch(**launch_kwargs)
                # Fresh isolated context: no persistent profile, no cookies, no downloads, no service workers
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    locale="en-US",
                    java_script_enabled=True,
                    accept_downloads=False,
                    service_workers="block",
                    ignore_https_errors=False,
                )

                # Close any popup page immediately so popups cannot bypass validation (H-02)
                def _on_context_page(new_page: Any) -> None:
                    if page is not None and new_page is not page:
                        popup_pages.append(new_page)
                        try:
                            asyncio.create_task(_safe_close_step(new_page, "close"))
                        except RuntimeError:
                            pass

                if hasattr(context, "on"):
                    context.on("page", _on_context_page)

                # Context-wide route interceptor covering main nav, redirects, JS nav, iframes, fetch, XHR, subresources
                async def _route_interceptor(route: Any) -> None:
                    req = getattr(route, "request", None)
                    req_url = str(getattr(req, "url", "") or "")
                    res_type = str(getattr(req, "resource_type", "") or "").lower()
                    is_nav = False
                    if req is not None:
                        is_nav_attr = getattr(req, "is_navigation_request", None)
                        is_nav = bool(is_nav_attr() if callable(is_nav_attr) else is_nav_attr)

                    # Block unnecessary heavy assets for memory/bandwidth hygiene (L-01)
                    if res_type in _BLOCKED_RESOURCE_TYPES:
                        await route.abort("blockedbyclient")
                        return

                    # Validate every navigation, redirect, iframe, fetch, XHR, and subresource URL
                    try:
                        await self._validate_browser_url(req_url)
                    except UrlSecurityError as exc:
                        logger.warning("Browser route blocked (%s, nav=%s): %s", res_type or "unknown", is_nav, exc)
                        if is_nav and not blocked_nav_error:
                            blocked_nav_error.append(str(exc))
                        await route.abort("blockedbyclient")
                        return

                    await route.continue_()

                if hasattr(context, "route"):
                    await context.route("**/*", _route_interceptor)

                page = await context.new_page()

                redirect_chain: list[str] = []

                def _on_response(resp: Any) -> None:
                    try:
                        if 300 <= int(resp.status) < 400:
                            loc = resp.headers.get("location", "")
                            if loc:
                                redirect_chain.append(loc)
                    except Exception:
                        pass

                if hasattr(page, "on"):
                    page.on("response", _on_response)

                response = await asyncio.wait_for(
                    page.goto(url, wait_until="domcontentloaded", timeout=int(nav_timeout * 1000)),
                    timeout=nav_timeout + 1.0,
                )

                if blocked_nav_error:
                    return FetchResponse(
                        url=url,
                        status_code=0,
                        redirect_chain=redirect_chain,
                        error_type="SSRF_BLOCKED",
                        error_message=blocked_nav_error[0],
                    )

                # Bounded wait for public client-side hydration (never hangs indefinitely)
                if wait_selector and self.render_wait > 0:
                    try:
                        await page.wait_for_selector(
                            wait_selector,
                            state="attached",
                            timeout=int(self.render_wait * 1000),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                elif self.render_wait > 0:
                    await asyncio.sleep(self.render_wait)

                if blocked_nav_error:
                    return FetchResponse(
                        url=url,
                        status_code=0,
                        redirect_chain=redirect_chain,
                        error_type="SSRF_BLOCKED",
                        error_message=blocked_nav_error[0],
                    )

                final_url = str(getattr(page, "url", url) or url)
                try:
                    await self._validate_browser_url(final_url)
                except UrlSecurityError as exc:
                    return FetchResponse(
                        url=final_url,
                        status_code=0,
                        redirect_chain=redirect_chain,
                        error_type="SSRF_BLOCKED",
                        error_message=str(exc),
                    )

                html = await page.content()
                if html and len(html.encode("utf-8", errors="ignore")) > self.max_html_bytes:
                    return FetchResponse(
                        url=final_url,
                        status_code=0,
                        redirect_chain=redirect_chain,
                        error_type="RESPONSE_TOO_LARGE",
                        error_message=f"Rendered DOM exceeded maximum allowed size ({self.max_html_bytes} bytes).",
                    )

                status_code = int(response.status) if (response is not None and hasattr(response, "status")) else 200

                error_type = ""
                error_message = ""
                if status_code == 429:
                    error_type = "RATE_LIMITED"
                    error_message = "HTTP 429 Too Many Requests (browser)."
                elif status_code == 403:
                    error_type = "FORBIDDEN"
                    error_message = "HTTP 403 Forbidden (browser)."
                elif status_code == 404:
                    error_type = "NOT_FOUND"
                    error_message = "HTTP 404 Not Found (browser)."
                elif status_code >= 500:
                    error_type = "SERVER_ERROR"
                    error_message = f"HTTP {status_code} server error (browser)."
                elif status_code >= 400:
                    error_type = "HTTP_ERROR"
                    error_message = f"HTTP {status_code} error (browser)."

                return FetchResponse(
                    url=final_url,
                    status_code=status_code,
                    text=html or "",
                    redirect_chain=redirect_chain,
                    error_type=error_type,
                    error_message=error_message,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                return FetchResponse(
                    url=url,
                    status_code=0,
                    error_type="TIMEOUT",
                    error_message=f"Browser navigation timed out after {nav_timeout:.1f}s.",
                )
            except Exception as exc:
                if blocked_nav_error:
                    return FetchResponse(
                        url=url,
                        status_code=0,
                        error_type="SSRF_BLOCKED",
                        error_message=blocked_nav_error[0],
                    )
                name = type(exc).__name__
                if "Timeout" in name:
                    return FetchResponse(
                        url=url,
                        status_code=0,
                        error_type="TIMEOUT",
                        error_message=f"Browser navigation timed out after {nav_timeout:.1f}s.",
                    )
                logger.warning("Browser fetch failed for %s: %s", url, name)
                return FetchResponse(
                    url=url,
                    status_code=0,
                    error_type="BROWSER_ERROR",
                    error_message=f"Browser acquisition failed: {name}",
                )
            finally:
                # Shielded, bounded cleanup in reverse order; survives cancellation without orphan Chromium
                for pop in popup_pages:
                    await _safe_close_step(pop, "close")
                for obj, method in ((page, "close"), (context, "close"), (browser, "close"), (pw, "stop")):
                    await _safe_close_step(obj, method)
