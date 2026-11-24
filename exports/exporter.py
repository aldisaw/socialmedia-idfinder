"""Hardened CSV, JSON, and TXT result exporter with CSV formula neutralization, path validation, and atomic writes."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from config import get_default_db_path
from core.logger import sanitize_log_text
from core.models import SearchResult

EXPORT_FIELDS: tuple[str, ...] = (
    "input",
    "platform",
    "username",
    "profile_url",
    "user_id",
    "id_type",
    "acquisition_method",
    "extraction_method",
    "extraction_source",
    "response_status",
    "final_url",
    "confidence",
    "validation_status",
    "status",
    "response_time",
    "error_message",
    "created_at",
)

_NULLABLE_TEXT_FIELDS: frozenset[str] = frozenset(
    {"username", "profile_url", "user_id", "extraction_source", "final_url", "error_message"}
)

_CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r", "\n")

_WINDOWS_RESERVED_DEVICES: frozenset[str] = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return sanitize_log_text(str(value))


def neutralize_csv_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection (=, +, -, @, tab, CR, LF) for CSV cells only (H-03)."""
    if not isinstance(value, str) or not value:
        return value
    stripped_leading = value.lstrip(" \t\r\n")
    if value[0] in _CSV_FORMULA_PREFIXES or (stripped_leading and stripped_leading[0] in ("=", "+", "-", "@")):
        return "'" + value
    return value


def validate_export_path(target_path: Path, fmt: str) -> Path:
    """Validate export target path against device names, symlinks, directories, extension mismatch, and DB overwrite (M-05)."""
    fmt_clean = fmt.strip().lower()
    if fmt_clean not in ("csv", "json", "txt"):
        raise ValueError(f"Unsupported export format: {fmt}")

    raw_str = str(target_path)
    if not raw_str.strip() or "\x00" in raw_str:
        raise ValueError("Invalid export path.")

    p = Path(target_path)
    if p.exists() and p.is_dir():
        raise ValueError(f"Export path points to a directory: '{p}'.")
    if p.is_symlink():
        raise ValueError(f"Export path cannot be a symbolic link: '{p}'.")

    # Reject Windows reserved device names on any path component
    for part in p.parts:
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_DEVICES:
            raise ValueError(f"Export path uses a reserved Windows device name: '{part}'.")

    # Reject SQLite database targets
    name_low = p.name.lower()
    if name_low.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")):
        raise ValueError("Refusing to overwrite a database file during export.")
    try:
        if p.resolve() == get_default_db_path().resolve():
            raise ValueError("Refusing to overwrite the active application database.")
    except OSError:
        pass

    expected_ext = f".{fmt_clean}"
    if p.suffix.lower() != expected_ext:
        raise ValueError(f"Export file extension must be '{expected_ext}' for {fmt_clean.upper()} export.")

    return p


def _atomic_write_text(target_path: Path, content: str) -> None:
    """Write UTF-8 text atomically using a same-directory temp file and os.replace (M-05)."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(target_path.parent),
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def serialize_result_record(result: SearchResult, *, nulls_as_none: bool = True) -> dict[str, Any]:
    """Serialize a SearchResult into a sanitized, schema-consistent export dictionary."""
    raw = result.to_dict()
    record: dict[str, Any] = {}
    for field in EXPORT_FIELDS:
        val = raw.get(field)
        if field == "response_time":
            try:
                record[field] = round(float(val), 3) if val is not None else 0.0
            except (TypeError, ValueError):
                record[field] = 0.0
            continue
        if field == "response_status":
            try:
                record[field] = int(val) if val is not None else 0
            except (TypeError, ValueError):
                record[field] = 0
            continue

        cleaned = _clean_text(val)
        if field in _NULLABLE_TEXT_FIELDS and not cleaned.strip():
            record[field] = None if nulls_as_none else ""
        else:
            record[field] = cleaned
    return record


def select_results(
    results: list[SearchResult],
    selected_indices: list[int] | None = None,
) -> list[SearchResult]:
    """Return either all results or only valid requested indices preserving order."""
    if selected_indices is None:
        return list(results)
    seen: set[int] = set()
    chosen: list[SearchResult] = []
    for idx in selected_indices:
        if 0 <= idx < len(results) and idx not in seen:
            seen.add(idx)
            chosen.append(results[idx])
    return chosen


def export_results(
    results: list[SearchResult],
    target_path: Path,
    fmt: str,
    *,
    selected_indices: list[int] | None = None,
) -> Path:
    """Write SearchResult records to CSV, JSON, or TXT with path validation, formula neutralization, and atomic writes."""
    fmt_clean = fmt.strip().lower()
    validated_path = validate_export_path(Path(target_path), fmt_clean)
    rows = select_results(results, selected_indices)

    if fmt_clean == "json":
        payload = [serialize_result_record(r, nulls_as_none=True) for r in rows]
        _atomic_write_text(validated_path, json.dumps(payload, indent=2, ensure_ascii=False))
    elif fmt_clean == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=list(EXPORT_FIELDS),
            quoting=csv.QUOTE_MINIMAL,
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in rows:
            rec = serialize_result_record(r, nulls_as_none=False)
            safe_rec = {k: neutralize_csv_cell(v) for k, v in rec.items()}
            writer.writerow(safe_rec)
        _atomic_write_text(validated_path, buf.getvalue())
    elif fmt_clean == "txt":
        lines: list[str] = []
        for idx, r in enumerate(rows, start=1):
            rec = serialize_result_record(r, nulls_as_none=True)
            parts = [
                f"[{idx}]",
                f"platform={rec['platform']}",
                f"input={rec['input']}",
                f"username={rec['username'] if rec['username'] is not None else 'null'}",
                f"profile_url={rec['profile_url'] if rec['profile_url'] is not None else 'null'}",
                f"user_id={rec['user_id'] if rec['user_id'] is not None else 'null'}",
                f"id_type={rec['id_type']}",
                f"acquisition_method={rec['acquisition_method']}",
                f"extraction_method={rec['extraction_method']}",
                f"extraction_source={rec['extraction_source'] if rec['extraction_source'] is not None else 'null'}",
                f"response_status={rec['response_status']}",
                f"final_url={rec['final_url'] if rec['final_url'] is not None else 'null'}",
                f"confidence={rec['confidence']}",
                f"validation_status={rec['validation_status']}",
                f"status={rec['status']}",
                f"response_time={rec['response_time']}s",
                f"error_message={rec['error_message'] if rec['error_message'] is not None else 'null'}",
                f"created_at={rec['created_at']}",
            ]
            lines.append(" | ".join(parts))
        _atomic_write_text(validated_path, "\n".join(lines) + ("\n" if lines else ""))

    return validated_path
