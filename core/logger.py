"""Sanitized application logger writing exclusively to AppDataLocation logs (M-04, L-04)."""

from __future__ import annotations

import logging
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Callable

from config import get_logs_dir

_URL_CREDS_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+:[^/\s@]+)@")

_SENSITIVE_PATTERNS = (
    # Authorization / Proxy-Authorization headers with scheme + credential (Basic, Bearer, Digest, Token, etc.)
    re.compile(r"(?i)(?:proxy-)?authorization\s*[:=]\s*(?:(?:basic|bearer|digest|token)\s+)?[^\s,;]+"),
    # Standalone Bearer/Basic tokens
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*"),
    # Sensitive key=value / header: value pairs (cookies, session tokens, API keys, passwords, secrets)
    re.compile(
        r"(?i)\b(cookie|set-cookie|token|access_token|refresh_token|id_token|"
        r"password|passwd|pwd|secret|api[_-]?key|x-api-key|"
        r"sessionid|csrftoken|ds_user_id|c_user|xs|datr)\s*[:=]\s*[^\s,;&\"']+"
    ),
)

_RE_CRLF = re.compile(r"[\r\n]+")


def sanitize_log_text(message: str, *, escape_newlines: bool = False) -> str:
    """Redact potential credentials, URL userinfo, auth headers, session tokens, and cookies."""
    clean = _URL_CREDS_RE.sub(r"\1[REDACTED]@", message)
    for pattern in _SENSITIVE_PATTERNS:
        clean = pattern.sub("[REDACTED]", clean)
    if escape_newlines:
        clean = _RE_CRLF.sub(" ", clean)
    return clean


class SanitizingFilter(logging.Filter):
    """Sanitizes log messages, all arguments (including exceptions), tracebacks, and CRLF sequences (M-04)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_log_text(record.msg, escape_newlines=True)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (
                        sanitize_log_text(v, escape_newlines=True)
                        if isinstance(v, str)
                        else (v if isinstance(v, (int, float)) else sanitize_log_text(str(v), escape_newlines=True))
                    )
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    sanitize_log_text(a, escape_newlines=True)
                    if isinstance(a, str)
                    else (a if isinstance(a, (int, float)) else sanitize_log_text(str(a), escape_newlines=True))
                    for a in record.args
                )
        if record.exc_info:
            try:
                tb_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
                record.exc_text = sanitize_log_text(tb_text, escape_newlines=False)
                record.exc_info = None
            except Exception:
                record.exc_info = None
                record.exc_text = "[REDACTED EXCEPTION]"
        elif record.exc_text:
            record.exc_text = sanitize_log_text(record.exc_text, escape_newlines=False)
        return True


class CallbackHandler(logging.Handler):
    """Forwards formatted log lines to a UI callback."""

    def __init__(self, callback: Callable[[str, str], None]) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._callback(record.levelname, msg)
        except Exception:
            pass


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure rotating file logging inside AppDataLocation with secret redaction."""
    logger = logging.getLogger("social_id_finder")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    has_file_handler = any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    if not has_file_handler:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        try:
            log_file = get_logs_dir() / "app.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(SanitizingFilter())
            logger.addHandler(file_handler)
        except OSError:
            pass

    return logger


def shutdown_logging() -> None:
    """Flush and close all file handlers attached to the application logger cleanly (L-04)."""
    logger = logging.getLogger("social_id_finder")
    for handler in list(logger.handlers):
        try:
            handler.flush()
            if isinstance(handler, RotatingFileHandler):
                handler.close()
                logger.removeHandler(handler)
        except Exception:
            pass


def attach_ui_log_callback(callback: Callable[[str, str], None]) -> CallbackHandler:
    logger = logging.getLogger("social_id_finder")
    handler = CallbackHandler(callback)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    handler.addFilter(SanitizingFilter())
    logger.addHandler(handler)
    return handler
