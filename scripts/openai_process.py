#!/usr/bin/env python3
"""Review and post-process MinerU XLSX output with the OpenAI Responses API.

The original MinerU workbook and parser artifacts are never modified.  Every
API response is archived under ``/parsed/openai/cache`` and a separate
processed workbook is produced with review columns next to the untouched raw
values.  Successful calls are reused by a deterministic cache whose identity
includes the input rows, source PDF page, model, prompt, schema and settings.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import requests
import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from pypdf import PdfReader, PdfWriter


CACHE_SCHEMA_VERSION = 2
OUTPUT_SCHEMA_VERSION = 1
PROMPT_VERSION = "catalog-openai-review-v2"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_API_BASE = "https://api.openai.com/v1"

REQUIRED_INPUT_COLUMNS = {
    "source_file",
    "page",
    "raw_parameter_name",
    "raw_value",
    "table_index",
    "row_index",
    "column_index",
}

MODEL_INPUT_COLUMNS = [
    "source_file",
    "page",
    "page_idx",
    "product_family",
    "article_number",
    "raw_parameter_name",
    "raw_value",
    "table_index",
    "row_index",
    "column_index",
    "header_path",
    "cell_tag",
    "rowspan",
    "colspan",
    "bbox",
    "bbox_scope",
    "bbox_units",
    "source_sha256",
]

REVIEW_COLUMNS = [
    "openai_row_id",
    "openai_verdict",
    "openai_confidence",
    "openai_issues",
    "openai_processed_product_family",
    "openai_processed_article_number",
    "openai_processed_parameter_name",
    "openai_processed_value",
    "openai_evidence",
    "openai_model",
    "openai_response_id",
    "openai_cache_key",
    "openai_cache_hit",
    "openai_verification_basis",
]

VERDICTS = {"ok", "suspicious", "incorrect", "missing_context"}
QUALITY_VALUES = {"pass", "warning", "fail", "uncertain"}

INSTRUCTIONS = """You verify table extraction from technical cutting-tool catalogues of any manufacturer.
Compare every supplied MinerU row with the attached source PDF page when a PDF is present.
Preserve source spelling, punctuation, decimal separators, leading zeroes, symbols, tolerances,
and parameter names such as d1, d2, d3, l1, l2, l3, Art.-Nr. and Ø exactly.
Return exactly one review item for every supplied row_id and no unknown row_id.
Never silently normalize or rewrite a raw value. Put a corrected or enriched value only in the
corresponding processed_* field, and only when it is directly supported by visible source evidence.
Use null when a value is unavailable or uncertain. Report merged-header, shifted-column,
missing-cell, OCR-confusion and article-number problems. Keep evidence concise and factual.
For unsupported or invisible evidence return missing_context. Never confirm guessed values.
"""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_quality": {
            "type": "string",
            "enum": sorted(QUALITY_VALUES),
        },
        "summary": {"type": "string"},
        "table_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table_index": {"type": ["integer", "null"]},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error"],
                    },
                    "issue": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["table_index", "severity", "issue", "evidence"],
                "additionalProperties": False,
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": sorted(VERDICTS),
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "issues": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "processed_product_family": {"type": ["string", "null"]},
                    "processed_article_number": {"type": ["string", "null"]},
                    "processed_parameter_name": {"type": ["string", "null"]},
                    "processed_value": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "row_id",
                    "verdict",
                    "confidence",
                    "issues",
                    "processed_product_family",
                    "processed_article_number",
                    "processed_parameter_name",
                    "processed_value",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["document_quality", "summary", "table_findings", "rows"],
    "additionalProperties": False,
}


class ProcessingError(RuntimeError):
    """Base class for expected post-processing failures."""


class ConfigurationError(ProcessingError):
    """Invalid local configuration."""


class ResponseValidationError(ProcessingError):
    """The API response cannot safely be joined to the input rows."""


class OpenAIAPIError(ProcessingError):
    """An OpenAI HTTP call failed without exposing authorization headers."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.response_headers = dict(response_headers or {})


@dataclass(frozen=True)
class WorkbookRecord:
    row_id: str
    sheet_name: str
    workbook_row: int
    values: dict[str, Any]


@dataclass(frozen=True)
class Batch:
    source_file: str
    source_sha256: str | None
    page_idx: int | None
    batch_index: int
    records: tuple[WorkbookRecord, ...]
    source_path: str | None
    source_page_pdf: bytes | None
    source_page_sha256: str | None
    source_warning: str | None

    @property
    def verification_basis(self) -> str:
        if self.source_page_pdf is not None:
            return "source_pdf_and_extracted_rows"
        return "extracted_rows_only"


@dataclass(frozen=True)
class Config:
    input_path: Path
    output_root: Path
    logs_root: Path
    catalogs_root: Path
    api_base: str
    api_key: str | None
    model: str
    reasoning_effort: str
    batch_rows: int
    timeout_seconds: int
    max_retries: int
    validation_retries: int
    max_output_tokens: int
    include_source_page: bool
    selection_manifest: Path | None
    allow_insecure_api_base: bool
    force: bool
    source_filters: tuple[str, ...]
    source_sha_filters: tuple[str, ...]
    source_pdf_overrides: tuple[tuple[str, str], ...]
    organization: str | None = None
    project: str | None = None
    escalation_model: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    selected_pages_only: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%S.%fZ")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, payload)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value, got {raw!r}.")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}.") from exc


def _load_api_key() -> str | None:
    direct = os.getenv("OPENAI_API_KEY", "").strip()
    if direct:
        return direct
    key_file = os.getenv("OPENAI_API_KEY_FILE", "").strip()
    if not key_file:
        return None
    path = Path(key_file)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"Could not read OPENAI_API_KEY_FILE: {exc}") from exc
    return value or None


def load_workbook_records(
    path: Path,
    source_filters: Sequence[str] = (),
    source_sha_filters: Sequence[str] = (),
    parse_selection: Mapping[tuple[str, int], str] | None = None,
    allow_empty: bool = False,
) -> tuple[list[str], list[WorkbookRecord]]:
    if not path.is_file():
        raise ConfigurationError(
            f"Input workbook does not exist: {path}. Run `make export` first."
        )
    filters = {value.casefold() for value in source_filters if value.strip()}
    sha_filters = {value.casefold() for value in source_sha_filters if value.strip()}
    records: list[WorkbookRecord] = []
    header_order: list[str] | None = None
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        for worksheet in workbook.worksheets:
            if worksheet.title == "_manifest":
                continue
            rows = worksheet.iter_rows()
            try:
                header_cells = next(rows)
            except StopIteration:
                continue
            headers = [
                str(cell.value) if cell.value is not None else ""
                for cell in header_cells
            ]
            if len(headers) != len(set(headers)) or any(
                not header for header in headers
            ):
                raise ConfigurationError(
                    f"Worksheet {worksheet.title!r} has empty or duplicate headers."
                )
            missing = REQUIRED_INPUT_COLUMNS - set(headers)
            if missing:
                raise ConfigurationError(
                    f"Worksheet {worksheet.title!r} misses columns: {sorted(missing)}"
                )
            if header_order is None:
                header_order = headers
            elif headers != header_order:
                raise ConfigurationError(
                    "All raw worksheets must have the same columns and order."
                )
            for row_number, cells in enumerate(rows, start=2):
                if all(cell.value is None for cell in cells):
                    continue
                for cell in cells:
                    if cell.data_type == "f":
                        raise ConfigurationError(
                            f"Formula found in raw worksheet {worksheet.title!r}, row {row_number}."
                        )
                values = {
                    header: _json_value(cell.value)
                    for header, cell in zip(headers, cells, strict=False)
                }
                source_file = str(values.get("source_file") or "")
                if filters or sha_filters:
                    source_match = (
                        source_file.casefold() in filters
                        or Path(source_file).name.casefold() in filters
                    )
                    sha_match = (
                        str(values.get("source_sha256") or "").casefold() in sha_filters
                    )
                    if not source_match and not sha_match:
                        continue
                if parse_selection is not None:
                    source_sha = str(values.get("source_sha256") or "").casefold()
                    page_idx = _integer_or_none(values.get("page_idx"))
                    if page_idx is None:
                        page = _integer_or_none(values.get("page"))
                        page_idx = page - 1 if page is not None else None
                    cache_key = str(values.get("cache_key") or "").casefold()
                    expected_cache_key = (
                        parse_selection.get((source_sha, page_idx))
                        if page_idx is not None
                        else None
                    )
                    if expected_cache_key is None or cache_key != expected_cache_key:
                        continue
                identity = {
                    "sheet": worksheet.title,
                    "workbook_row": row_number,
                    "values": values,
                }
                row_id = _sha256_json(identity)[:32]
                records.append(
                    WorkbookRecord(
                        row_id=row_id,
                        sheet_name=worksheet.title,
                        workbook_row=row_number,
                        values=values,
                    )
                )
    finally:
        workbook.close()
    if header_order is None:
        raise ConfigurationError("The workbook contains no raw worksheets.")
    if not records and not allow_empty:
        detail = (
            " matching the source/manifest selection"
            if filters or sha_filters or parse_selection is not None
            else ""
        )
        raise ConfigurationError(f"The workbook contains no data rows{detail}.")
    return header_order, records


def load_parse_selection(
    path: Path | None,
) -> tuple[dict[tuple[str, int], str] | None, str | None]:
    if path is None:
        return None, None
    if not path.is_file():
        raise ConfigurationError(f"OpenAI selection manifest does not exist: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(
            f"Could not read OpenAI selection manifest: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ConfigurationError("OpenAI selection manifest must contain an object.")
    entries = raw.get("parse_selection")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError(
            "OpenAI selection manifest has no parse_selection entries."
        )
    selection: dict[tuple[str, int], str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ConfigurationError(f"Invalid parse_selection entry at index {index}.")
        source_sha = str(entry.get("source_sha256") or "").casefold()
        cache_key = str(entry.get("cache_key") or "").casefold()
        page_idx = _integer_or_none(entry.get("page_idx"))
        if (
            len(source_sha) != 64
            or any(character not in "0123456789abcdef" for character in source_sha)
            or len(cache_key) != 64
            or any(character not in "0123456789abcdef" for character in cache_key)
            or page_idx is None
            or page_idx < 0
        ):
            raise ConfigurationError(f"Invalid parse_selection entry at index {index}.")
        identity = (source_sha, page_idx)
        if identity in selection:
            raise ConfigurationError(
                f"Duplicate parse_selection for source {source_sha}, page_idx {page_idx}."
            )
        selection[identity] = cache_key
    return selection, _sha256_file(path)


def _integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _page_idx(record: WorkbookRecord) -> int | None:
    direct = _integer_or_none(record.values.get("page_idx"))
    if direct is not None:
        return direct
    page = _integer_or_none(record.values.get("page"))
    if page is None:
        return None
    return page - 1


def _record_sort_key(record: WorkbookRecord) -> tuple[Any, ...]:
    return (
        _integer_or_none(record.values.get("table_index")) or 0,
        _integer_or_none(record.values.get("row_index")) or 0,
        _integer_or_none(record.values.get("column_index")) or 0,
        record.sheet_name,
        record.workbook_row,
    )


def _resolve_source_path(
    source_file: str,
    catalogs_root: Path,
    source_sha256: str | None = None,
) -> tuple[Path | None, str | None]:
    supplied = Path(source_file)
    candidates: list[Path] = []
    if supplied.is_absolute():
        candidates.append(supplied)
        try:
            candidates.append(catalogs_root / supplied.relative_to("/catalogs"))
        except ValueError:
            pass
    else:
        candidates.extend([catalogs_root / supplied, catalogs_root / supplied.name])
    existing = list(
        dict.fromkeys(path.resolve() for path in candidates if path.is_file())
    )
    if source_sha256:
        existing = [path for path in existing if _sha256_file(path) == source_sha256]
    if not existing:
        try:
            by_name = list(
                dict.fromkeys(
                    path.resolve()
                    for path in catalogs_root.rglob("*")
                    if path.is_file()
                    and path.name.casefold() == supplied.name.casefold()
                )
            )
            if source_sha256:
                by_name = [
                    path for path in by_name if _sha256_file(path) == source_sha256
                ]
            existing = by_name
        except OSError as exc:
            return None, f"Could not scan catalogs for {source_file!r}: {exc}"
    if not existing and source_sha256:
        try:
            existing = [
                path.resolve()
                for path in sorted(catalogs_root.rglob("*.pdf"))
                if path.is_file() and _sha256_file(path) == source_sha256
            ]
        except OSError as exc:
            return None, f"Could not match source SHA-256 for {source_file!r}: {exc}"
    if not existing:
        return None, f"Source PDF was not found for {source_file!r}."
    if len(existing) > 1 and not source_sha256:
        return (
            None,
            f"Multiple source PDFs match {source_file!r}; visual verification is ambiguous.",
        )
    if existing[0].suffix.casefold() != ".pdf":
        return None, f"Source file is not a PDF: {existing[0]}"
    return existing[0], None


def _extract_pdf_page(
    source: Path, page_idx: int | None
) -> tuple[bytes | None, str | None]:
    if page_idx is None or page_idx < 0:
        return None, "Source page index is unavailable."
    try:
        reader = PdfReader(str(source))
        if page_idx >= len(reader.pages):
            return None, (
                f"Source page index {page_idx} is outside the {len(reader.pages)}-page PDF."
            )
        writer = PdfWriter()
        writer.add_page(reader.pages[page_idx])
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue(), None
    except Exception as exc:
        return None, f"Could not extract source PDF page: {exc}"


def build_batches(
    records: Sequence[WorkbookRecord],
    catalogs_root: Path,
    batch_rows: int,
    include_source_page: bool,
    source_pdf_overrides: Mapping[str, Path] | None = None,
) -> list[Batch]:
    if batch_rows < 1 or batch_rows > 200:
        raise ConfigurationError("--batch-rows must be between 1 and 200.")
    overrides = source_pdf_overrides or {}
    grouped: dict[tuple[str, str | None, int | None], list[WorkbookRecord]] = {}
    for record in records:
        source_file = str(record.values.get("source_file") or "")
        source_sha256 = str(record.values.get("source_sha256") or "") or None
        grouped.setdefault((source_file, source_sha256, _page_idx(record)), []).append(
            record
        )

    batches: list[Batch] = []
    resolution_cache: dict[tuple[str, str | None], tuple[Path | None, str | None]] = {}
    for source_file, source_sha256, page_idx in sorted(
        grouped, key=lambda item: (item[0], item[1] or "", item[2] or -1)
    ):
        source_path: Path | None = None
        page_pdf: bytes | None = None
        warning: str | None = None
        if include_source_page:
            override = overrides.get(source_sha256 or "")
            if override is not None and override.is_file():
                if source_sha256 and _sha256_file(override) != source_sha256:
                    raise ConfigurationError(
                        "Source PDF override does not match the RAW SHA-256."
                    )
                source_path = override.resolve()
            else:
                resolution_key = (source_file, source_sha256)
                if resolution_key not in resolution_cache:
                    resolution_cache[resolution_key] = _resolve_source_path(
                        source_file, catalogs_root, source_sha256
                    )
                source_path, warning = resolution_cache[resolution_key]
            if source_path is not None:
                page_pdf, extraction_warning = _extract_pdf_page(source_path, page_idx)
                warning = extraction_warning or warning
        ordered = sorted(
            grouped[(source_file, source_sha256, page_idx)], key=_record_sort_key
        )
        for batch_index, start in enumerate(range(0, len(ordered), batch_rows)):
            selected = tuple(ordered[start : start + batch_rows])
            batches.append(
                Batch(
                    source_file=source_file,
                    source_sha256=source_sha256,
                    page_idx=page_idx,
                    batch_index=batch_index,
                    records=selected,
                    source_path=str(source_path) if source_path else None,
                    source_page_pdf=page_pdf,
                    source_page_sha256=(
                        _sha256_bytes(page_pdf) if page_pdf is not None else None
                    ),
                    source_warning=warning,
                )
            )
    return batches


def _model_row(record: WorkbookRecord) -> dict[str, Any]:
    payload = {column: record.values.get(column) for column in MODEL_INPUT_COLUMNS}
    return {"row_id": record.row_id, **payload}


def build_request_payload(batch: Batch, config: Config) -> dict[str, Any]:
    task = {
        "task": "row_validation",
        "source_file": batch.source_file,
        "source_sha256": batch.source_sha256,
        "page": batch.page_idx + 1 if batch.page_idx is not None else None,
        "page_idx": batch.page_idx,
        "verification_basis": batch.verification_basis,
        "source_warning": batch.source_warning,
        "rows": [_model_row(record) for record in batch.records],
    }
    content: list[dict[str, Any]] = []
    if batch.source_page_pdf is not None:
        encoded = base64.b64encode(batch.source_page_pdf).decode("ascii")
        content.append(
            {
                "type": "input_file",
                "filename": f"{Path(batch.source_file).stem}-page-{(batch.page_idx or 0) + 1}.pdf",
                # Inline PDF inputs use the data-URL form accepted by the
                # production Responses endpoint.
                "file_data": f"data:application/pdf;base64,{encoded}",
            }
        )
    content.append(
        {
            "type": "input_text",
            "text": json.dumps(task, ensure_ascii=False, separators=(",", ":")),
        }
    )
    response_schema = copy.deepcopy(RESPONSE_SCHEMA)
    rows_schema = response_schema["properties"]["rows"]
    expected_row_ids = [record.row_id for record in batch.records]
    rows_schema["minItems"] = len(expected_row_ids)
    rows_schema["maxItems"] = len(expected_row_ids)
    rows_schema["items"]["properties"]["row_id"] = {
        "type": "string",
        "enum": expected_row_ids,
    }
    return {
        "model": config.model,
        "instructions": INSTRUCTIONS,
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": config.reasoning_effort},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "catalog_parser_review",
                "strict": True,
                "schema": response_schema,
            },
        },
        "max_output_tokens": config.max_output_tokens,
        "prompt_cache_key": PROMPT_VERSION,
        "store": False,
        "metadata": {
            "application": "catalog-parser",
            "prompt_version": PROMPT_VERSION,
            "batch": str(batch.batch_index),
        },
    }


def build_validation_retry_payload(
    payload: Mapping[str, Any], attempt: int
) -> dict[str, Any]:
    retried = copy.deepcopy(dict(payload))
    retried["metadata"]["validation_attempt"] = str(attempt)
    retried["input"][0]["content"].append(
        {
            "type": "input_text",
            "text": (
                "The previous structured response failed deterministic validation. "
                "Return exactly one item for every row_id from the task, copy each row_id "
                "byte-for-byte, do not duplicate rows, and do not return any other row_id."
            ),
        }
    )
    return retried


def _sanitized_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(dict(payload))
    for item in sanitized.get("input", []):
        for content in item.get("content", []):
            file_data = content.get("file_data")
            if isinstance(file_data, str):
                raw = file_data.rsplit(",", 1)[-1]
                try:
                    decoded = base64.b64decode(raw, validate=True)
                except Exception:
                    decoded = raw.encode("utf-8")
                content["file_data"] = {
                    "omitted_from_log": True,
                    "bytes": len(decoded),
                    "sha256": _sha256_bytes(decoded),
                }
    return sanitized


def _selected_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "x-request-id",
        "openai-processing-ms",
        "openai-version",
        "retry-after",
    }
    return {
        key.casefold(): value
        for key, value in headers.items()
        if key.casefold() in allowed
    }


class ResponsesClient:
    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def create(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.config.api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not set. Add it to .env or use OPENAI_API_KEY_FILE."
            )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "guhring-parser-openai/1",
        }
        if self.config.organization:
            headers["OpenAI-Organization"] = self.config.organization
        if self.config.project:
            headers["OpenAI-Project"] = self.config.project
        url = f"{self.config.api_base.rstrip('/')}/responses"
        last_error: OpenAIAPIError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = OpenAIAPIError(f"OpenAI request failed: {exc}")
                retryable = True
            else:
                selected_headers = _selected_headers(response.headers)
                try:
                    body: Any = response.json()
                except ValueError:
                    body = {"text": response.text[:4000]}
                if 200 <= response.status_code < 300:
                    if not isinstance(body, dict):
                        raise OpenAIAPIError(
                            "OpenAI returned a non-object JSON response.",
                            status_code=response.status_code,
                            response_body=body,
                            response_headers=selected_headers,
                        )
                    return body, {
                        "status_code": response.status_code,
                        "headers": selected_headers,
                    }
                message = "OpenAI API returned HTTP " + str(response.status_code)
                if isinstance(body, dict):
                    api_message = body.get("error", {}).get("message")
                    if isinstance(api_message, str) and api_message:
                        message += f": {api_message[:1000]}"
                last_error = OpenAIAPIError(
                    message,
                    status_code=response.status_code,
                    response_body=body,
                    response_headers=selected_headers,
                )
                retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self.config.max_retries:
                assert last_error is not None
                raise last_error
            retry_after = 0.0
            if last_error.response_headers.get("retry-after"):
                try:
                    retry_after = float(last_error.response_headers["retry-after"])
                except ValueError:
                    retry_after = 0.0
            delay = min(30.0, max(retry_after, float(2**attempt)))
            time.sleep(delay)
        assert last_error is not None
        raise last_error


def _extract_output_text(response: Mapping[str, Any]) -> str:
    if response.get("status") != "completed":
        raise ResponseValidationError(
            f"OpenAI response status is {response.get('status')!r}, expected 'completed'."
        )
    top_level = response.get("output_text")
    if isinstance(top_level, str) and top_level.strip():
        return top_level
    texts: list[str] = []
    refusals: list[str] = []
    for output in response.get("output", []):
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content", []):
            if not isinstance(content, Mapping):
                continue
            if content.get("type") == "output_text" and isinstance(
                content.get("text"), str
            ):
                texts.append(content["text"])
            if content.get("type") == "refusal" and isinstance(
                content.get("refusal"), str
            ):
                refusals.append(content["refusal"])
    if refusals:
        raise ResponseValidationError(
            "OpenAI refused the request: " + " ".join(refusals)[:1000]
        )
    if not texts:
        raise ResponseValidationError(
            "OpenAI response contains no output_text content."
        )
    return "".join(texts)


def validate_structured_result(
    value: Any, expected_row_ids: Iterable[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResponseValidationError("Structured output must be a JSON object.")
    expected_keys = {"document_quality", "summary", "table_findings", "rows"}
    if set(value) != expected_keys:
        raise ResponseValidationError(
            f"Structured output keys differ from the required schema: {sorted(value)}"
        )
    if value["document_quality"] not in QUALITY_VALUES:
        raise ResponseValidationError("Invalid document_quality value.")
    if not isinstance(value["summary"], str):
        raise ResponseValidationError("summary must be a string.")
    if not isinstance(value["table_findings"], list) or not isinstance(
        value["rows"], list
    ):
        raise ResponseValidationError("table_findings and rows must be arrays.")
    expected = list(expected_row_ids)
    actual: list[str] = []
    required_row_keys = {
        "row_id",
        "verdict",
        "confidence",
        "issues",
        "processed_product_family",
        "processed_article_number",
        "processed_parameter_name",
        "processed_value",
        "evidence",
    }
    for row in value["rows"]:
        if not isinstance(row, dict) or set(row) != required_row_keys:
            raise ResponseValidationError(
                "A row review differs from the required schema."
            )
        if not isinstance(row["row_id"], str):
            raise ResponseValidationError("row_id must be a string.")
        actual.append(row["row_id"])
        if row["verdict"] not in VERDICTS:
            raise ResponseValidationError(f"Invalid verdict for {row['row_id']}.")
        confidence = row["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ResponseValidationError(f"Invalid confidence for {row['row_id']}.")
        if not 0 <= confidence <= 1:
            raise ResponseValidationError(
                f"Confidence is outside [0, 1] for {row['row_id']}."
            )
        if not isinstance(row["issues"], list) or not all(
            isinstance(issue, str) for issue in row["issues"]
        ):
            raise ResponseValidationError(
                f"issues must be strings for {row['row_id']}."
            )
        for field in (
            "processed_product_family",
            "processed_article_number",
            "processed_parameter_name",
            "processed_value",
        ):
            if row[field] is not None and not isinstance(row[field], str):
                raise ResponseValidationError(f"{field} must be string or null.")
        if not isinstance(row["evidence"], str):
            raise ResponseValidationError("evidence must be a string.")
        if row["verdict"] != "missing_context" and not row["evidence"].strip():
            raise ResponseValidationError(
                "A visual verdict requires evidence or missing_context."
            )
    actual_counts = Counter(actual)
    duplicated = sorted(row_id for row_id, count in actual_counts.items() if count > 1)
    if duplicated:
        raise ResponseValidationError(
            f"Structured output contains duplicate row_id values: {duplicated}."
        )
    if set(actual) != set(expected) or len(actual) != len(expected):
        expected_counts = Counter(expected)
        missing = sorted((expected_counts - actual_counts).elements())
        unknown = sorted((actual_counts - expected_counts).elements())
        raise ResponseValidationError(
            f"Structured output row_id mismatch; missing={missing}, unknown={unknown}."
        )
    for finding in value["table_findings"]:
        if not isinstance(finding, dict) or set(finding) != {
            "table_index",
            "severity",
            "issue",
            "evidence",
        }:
            raise ResponseValidationError(
                "A table finding differs from the required schema."
            )
        if finding["severity"] not in {"info", "warning", "error"}:
            raise ResponseValidationError("Invalid table finding severity.")
        table_index = finding["table_index"]
        if table_index is not None and (
            isinstance(table_index, bool) or not isinstance(table_index, int)
        ):
            raise ResponseValidationError(
                "table finding index must be integer or null."
            )
        if not isinstance(finding["issue"], str) or not isinstance(
            finding["evidence"], str
        ):
            raise ResponseValidationError(
                "table finding issue and evidence must be strings."
            )
    return value


def parse_structured_response(
    response: Mapping[str, Any], expected_row_ids: Iterable[str]
) -> dict[str, Any]:
    text = _extract_output_text(response)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseValidationError(
            f"Structured output is not valid JSON: {exc}"
        ) from exc
    return validate_structured_result(parsed, expected_row_ids)


def _config_identity(config: Config) -> dict[str, Any]:
    return {
        "cache_schema": CACHE_SCHEMA_VERSION,
        "output_schema": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _sha256_bytes(INSTRUCTIONS.encode("utf-8")),
        "response_schema_sha256": _sha256_json(RESPONSE_SCHEMA),
        "api_base": config.api_base.rstrip("/"),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "max_output_tokens": config.max_output_tokens,
        "include_source_page": config.include_source_page,
        "selection_manifest_sha256": (
            _sha256_file(config.selection_manifest)
            if config.selection_manifest
            else None
        ),
    }


def _batch_key(batch: Batch, config_digest: str) -> str:
    return _sha256_json(
        {
            "config_digest": config_digest,
            "source_file": batch.source_file,
            "source_sha256": batch.source_sha256,
            "page_idx": batch.page_idx,
            "source_page_sha256": batch.source_page_sha256,
            "verification_basis": batch.verification_basis,
            "rows": [_model_row(record) for record in batch.records],
        }
    )


def review_escalated_batch(
    batch: Batch, config: Config, cache: BatchCache, client: Any
) -> dict[str, Any]:
    """Validate only conflicting rows with the configured senior model."""
    ids = [record.row_id for record in batch.records]
    key = _batch_key(batch, _sha256_json(_config_identity(config)))
    hit = None if config.force else cache.load(key, ids)
    if hit:
        result, manifest = hit
        return {
            "result": result,
            "manifest": manifest,
            "cache_hit": True,
            "api_calls": 0,
            "usage": {},
            "cache_key": key,
        }
    payload = build_request_payload(batch, config)
    usage = Counter()
    for attempt in range(config.validation_retries + 1):
        request = (
            payload
            if attempt == 0
            else build_validation_retry_payload(payload, attempt)
        )
        response = None
        metadata = None
        try:
            response, metadata = client.create(request)
            usage.update(_response_usage(response))
            result = parse_structured_response(response, ids)
        except ResponseValidationError as exc:
            cache.record_failure(key, request, exc, response, metadata)
            if attempt == config.validation_retries:
                raise
            continue
        except BaseException as exc:
            cache.record_failure(key, request, exc, response, metadata)
            raise
        manifest = cache.record_success(
            key,
            request,
            response,
            metadata,
            result,
            {
                "cache_key": key,
                "response_id": response.get("id"),
                "response_model": response.get("model", config.model),
                "row_ids": ids,
                "usage": dict(usage),
                "validation_retries": attempt,
                "created_at": _utc_now().isoformat(),
                "verification_basis": batch.verification_basis,
                "stage": "escalated_row_validation",
            },
        )
        return {
            "result": result,
            "manifest": manifest,
            "cache_hit": False,
            "api_calls": attempt + 1,
            "usage": dict(usage),
            "cache_key": key,
        }
    raise ResponseValidationError("Escalation failed.")


class BatchCache:
    def __init__(self, output_root: Path) -> None:
        self.root = output_root / "cache" / f"v{CACHE_SCHEMA_VERSION}"

    def _batch_root(self, key: str) -> Path:
        return self.root / key[:2] / key

    def load(
        self,
        key: str,
        expected_row_ids: Sequence[str],
        result_validator: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        root = self._batch_root(key)
        pointer_path = root / "success.json"
        if not pointer_path.is_file():
            return None
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            relative_attempt = Path(pointer["attempt"])
            if relative_attempt.is_absolute() or ".." in relative_attempt.parts:
                return None
            attempt = (root / relative_attempt).resolve()
            if root.resolve() not in attempt.parents:
                return None
            result_path = attempt / "result.json"
            response_path = attempt / "response.json"
            request_path = attempt / "request.json"
            manifest_path = attempt / "manifest.json"
            for path, digest_key in (
                (request_path, "request_sha256"),
                (result_path, "result_sha256"),
                (response_path, "response_sha256"),
                (manifest_path, "manifest_sha256"),
            ):
                if not path.is_file() or _sha256_file(path) != pointer[digest_key]:
                    return None
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result = (result_validator or validate_structured_result)(
                result, expected_row_ids
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "success" or manifest.get("cache_key") != key:
                return None
            return result, manifest
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ProcessingError,
        ):
            return None

    def _new_attempt(self, key: str) -> tuple[Path, str]:
        relative = Path("attempts") / f"{_timestamp()}-{uuid.uuid4().hex}"
        path = self._batch_root(key) / relative
        path.mkdir(parents=True, exist_ok=False)
        return path, relative.as_posix()

    def record_success(
        self,
        key: str,
        request_payload: Mapping[str, Any],
        response_payload: Mapping[str, Any],
        response_meta: Mapping[str, Any],
        result: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt, relative = self._new_attempt(key)
        request_path = attempt / "request.json"
        response_path = attempt / "response.json"
        result_path = attempt / "result.json"
        manifest_path = attempt / "manifest.json"
        _atomic_write_json(request_path, _sanitized_request(request_payload))
        _atomic_write_json(
            response_path,
            {"http": dict(response_meta), "response": dict(response_payload)},
        )
        _atomic_write_json(result_path, dict(result))
        full_manifest = {**dict(manifest), "status": "success", "attempt": relative}
        _atomic_write_json(manifest_path, full_manifest)
        pointer = {
            "cache_schema": CACHE_SCHEMA_VERSION,
            "status": "success",
            "cache_key": key,
            "attempt": relative,
            "request_sha256": _sha256_file(request_path),
            "response_sha256": _sha256_file(response_path),
            "result_sha256": _sha256_file(result_path),
            "manifest_sha256": _sha256_file(manifest_path),
            "updated_at": _utc_now().isoformat(),
        }
        _atomic_write_json(self._batch_root(key) / "success.json", pointer)
        return full_manifest

    def record_failure(
        self,
        key: str,
        request_payload: Mapping[str, Any],
        error: BaseException,
        response_payload: Mapping[str, Any] | None = None,
        response_meta: Mapping[str, Any] | None = None,
    ) -> None:
        attempt, relative = self._new_attempt(key)
        _atomic_write_json(
            attempt / "request.json", _sanitized_request(request_payload)
        )
        if response_payload is not None:
            _atomic_write_json(
                attempt / "response.json",
                {
                    "http": dict(response_meta or {}),
                    "response": dict(response_payload),
                },
            )
        failure: dict[str, Any] = {
            "status": "failed",
            "cache_key": key,
            "attempt": relative,
            "error_type": type(error).__name__,
            "error": str(error),
            "created_at": _utc_now().isoformat(),
        }
        if isinstance(error, OpenAIAPIError):
            failure["http_status"] = error.status_code
            failure["response_headers"] = error.response_headers
            failure["response_body"] = error.response_body
        _atomic_write_json(attempt / "failure.json", failure)


def _response_usage(response: Mapping[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    values: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            values[key] = value
    return values


def _write_processed_workbook(
    path: Path,
    input_headers: Sequence[str],
    records: Sequence[WorkbookRecord],
    reviews: Mapping[str, Mapping[str, Any]],
    model: str,
) -> None:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("processed")
    headers = list(input_headers) + REVIEW_COLUMNS
    worksheet.append(headers)
    for record in records:
        joined = reviews[record.row_id]
        review = joined["review"]
        values = [record.values.get(header) for header in input_headers]
        values.extend(
            [
                record.row_id,
                review["verdict"],
                review["confidence"],
                json.dumps(review["issues"], ensure_ascii=False, separators=(",", ":")),
                review["processed_product_family"],
                review["processed_article_number"],
                review["processed_parameter_name"],
                review["processed_value"],
                review["evidence"],
                joined.get("response_model") or model,
                joined.get("response_id"),
                joined["cache_key"],
                joined["cache_hit"],
                joined["verification_basis"],
            ]
        )
        excel_row: list[WriteOnlyCell] = []
        for value in values:
            cell = WriteOnlyCell(worksheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            excel_row.append(cell)
        worksheet.append(excel_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _validate_processed_workbook(
    path: Path,
    input_headers: Sequence[str],
    records: Sequence[WorkbookRecord],
) -> dict[str, Any]:
    expected_headers = list(input_headers) + REVIEW_COLUMNS
    workbook = load_workbook(path, read_only=True, data_only=False)
    canonical_rows: list[dict[str, Any]] = []
    try:
        if workbook.sheetnames != ["processed"]:
            raise ResponseValidationError(
                f"Processed workbook sheets are unexpected: {workbook.sheetnames}"
            )
        worksheet = workbook["processed"]
        rows = worksheet.iter_rows()
        try:
            actual_headers = [cell.value for cell in next(rows)]
        except StopIteration as exc:
            raise ResponseValidationError("Processed workbook is empty.") from exc
        if actual_headers != expected_headers:
            raise ResponseValidationError(
                "Processed workbook headers do not match the schema."
            )
        count = 0
        for expected, cells in zip(records, rows, strict=False):
            count += 1
            values = {
                header: cell.value
                for header, cell in zip(expected_headers, cells, strict=False)
            }
            for header in input_headers:
                if values.get(header) != expected.values.get(header):
                    raise ResponseValidationError(
                        f"Raw value changed in processed workbook: row={expected.row_id}, column={header}."
                    )
            for header, cell in zip(expected_headers, cells, strict=False):
                if header in input_headers and cell.data_type == "f":
                    raise ResponseValidationError(
                        f"Formula found in processed raw field: row={expected.row_id}, column={header}."
                    )
            if values.get("openai_row_id") != expected.row_id:
                raise ResponseValidationError(
                    "Processed workbook row_id order is invalid."
                )
            if values.get("openai_verdict") not in VERDICTS:
                raise ResponseValidationError(
                    "Processed workbook contains an invalid verdict."
                )
            issues = values.get("openai_issues")
            try:
                decoded_issues = json.loads(issues) if isinstance(issues, str) else None
            except json.JSONDecodeError as exc:
                raise ResponseValidationError(
                    "Processed workbook issues are invalid JSON."
                ) from exc
            if not isinstance(decoded_issues, list):
                raise ResponseValidationError(
                    "Processed workbook issues must be a JSON array."
                )
            canonical_rows.append(
                {header: _json_value(values.get(header)) for header in expected_headers}
            )
        if count != len(records):
            raise ResponseValidationError(
                f"Processed workbook has {count} rows, expected {len(records)}."
            )
        try:
            next(rows)
        except StopIteration:
            pass
        else:
            raise ResponseValidationError("Processed workbook contains extra rows.")
    finally:
        workbook.close()
    return {
        "status": "passed",
        "rows": len(canonical_rows),
        "columns": len(expected_headers),
        "canonical_rows_sha256": _sha256_json(canonical_rows),
    }


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    payload = b"".join(_canonical_bytes(dict(row)) + b"\n" for row in rows)
    _atomic_write_bytes(path, payload)


def _validate_api_base(config: Config) -> None:
    parsed = urlparse(config.api_base)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigurationError("OPENAI_API_BASE must be an absolute URL.")
    if parsed.scheme == "https":
        return
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme == "http"
        and parsed.hostname in local_hosts
        and config.allow_insecure_api_base
    ):
        return
    raise ConfigurationError(
        "OPENAI_API_BASE must use HTTPS. Local HTTP mocks require --allow-insecure-api-base."
    )


def process_workbook(
    config: Config, client: ResponsesClient | None = None
) -> dict[str, Any]:
    _validate_api_base(config)
    if not config.api_key and not (config.output_root / "cache").is_dir():
        raise ConfigurationError(
            "OPENAI_API_KEY is not set. Add it to .env or use OPENAI_API_KEY_FILE."
        )
    parse_selection, selection_manifest_sha256 = load_parse_selection(
        config.selection_manifest
    )
    input_headers, records = load_workbook_records(
        config.input_path,
        config.source_filters,
        config.source_sha_filters,
        parse_selection,
    )
    batches = build_batches(
        records,
        config.catalogs_root,
        config.batch_rows,
        config.include_source_page,
        {sha: Path(path) for sha, path in config.source_pdf_overrides},
    )
    config_identity = _config_identity(config)
    config_digest = _sha256_json(config_identity)
    run_context = {
        "status": "running",
        "prompt_version": PROMPT_VERSION,
        "input": str(config.input_path),
        "input_sha256": _sha256_file(config.input_path),
        "rows": len(records),
        "documents": len(
            {str(record.values.get("source_file") or "") for record in records}
        ),
        "pages": len({(batch.source_file, batch.page_idx) for batch in batches}),
        "batches": len(batches),
        "batches_with_source_pdf": sum(
            batch.source_page_pdf is not None for batch in batches
        ),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "config_digest": config_digest,
        "source_warnings": sorted(
            {batch.source_warning for batch in batches if batch.source_warning}
        ),
        "selection_manifest": (
            str(config.selection_manifest) if config.selection_manifest else None
        ),
        "selection_manifest_sha256": selection_manifest_sha256,
        "selected_parser_pages": len(parse_selection or {}),
    }
    started = time.monotonic()
    run_id = f"{_timestamp()}-{uuid.uuid4().hex}"
    run_dir = config.output_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    cache = BatchCache(config.output_root)
    api_client = client or ResponsesClient(config)
    reviews: dict[str, dict[str, Any]] = {}
    batch_reports: list[dict[str, Any]] = []
    total_usage: Counter[str] = Counter()
    api_calls = 0
    cache_hits = 0
    validation_retries_total = 0
    try:
        for batch in batches:
            expected_ids = [record.row_id for record in batch.records]
            key = _batch_key(batch, config_digest)
            hit = None if config.force else cache.load(key, expected_ids)
            if hit is not None:
                result, cache_manifest = hit
                cache_hit = True
                cache_hits += 1
                response_id = cache_manifest.get("response_id")
                response_model = str(
                    cache_manifest.get("response_model") or config.model
                )
                usage: dict[str, int] = {}
                validation_retry_count = int(
                    cache_manifest.get("validation_retries") or 0
                )
            else:
                cache_hit = False
                payload = build_request_payload(batch, config)
                validation_retry_count = 0
                batch_usage: Counter[str] = Counter()
                while True:
                    request_payload = (
                        payload
                        if validation_retry_count == 0
                        else build_validation_retry_payload(
                            payload, validation_retry_count
                        )
                    )
                    try:
                        response, response_meta = api_client.create(request_payload)
                    except BaseException as exc:
                        cache.record_failure(key, request_payload, exc)
                        raise
                    api_calls += 1
                    response_usage = _response_usage(response)
                    total_usage.update(response_usage)
                    batch_usage.update(response_usage)
                    try:
                        result = parse_structured_response(response, expected_ids)
                    except ResponseValidationError as exc:
                        cache.record_failure(
                            key,
                            request_payload,
                            exc,
                            response,
                            response_meta,
                        )
                        if validation_retry_count >= config.validation_retries:
                            raise
                        validation_retry_count += 1
                        validation_retries_total += 1
                        continue
                    usage = dict(batch_usage)
                    response_id = response.get("id")
                    response_model = str(response.get("model") or config.model)
                    cache_manifest = cache.record_success(
                        key,
                        request_payload,
                        response,
                        response_meta,
                        result,
                        {
                            "cache_key": key,
                            "config_digest": config_digest,
                            "source_file": batch.source_file,
                            "source_sha256": batch.source_sha256,
                            "page_idx": batch.page_idx,
                            "source_path": batch.source_path,
                            "source_page_sha256": batch.source_page_sha256,
                            "verification_basis": batch.verification_basis,
                            "source_warning": batch.source_warning,
                            "row_ids": expected_ids,
                            "requested_model": config.model,
                            "response_model": response_model,
                            "response_id": response_id,
                            "validation_retries": validation_retry_count,
                            "usage": usage,
                            "created_at": _utc_now().isoformat(),
                        },
                    )
                    break
            row_reviews = {row["row_id"]: row for row in result["rows"]}
            escalated = None
            if config.escalation_model:
                conflicting = tuple(
                    record
                    for record in batch.records
                    if row_reviews[record.row_id]["verdict"] != "ok"
                )
                if conflicting:
                    senior = replace(
                        config, model=config.escalation_model, escalation_model=None
                    )
                    escalated = review_escalated_batch(
                        replace(batch, records=conflicting), senior, cache, api_client
                    )
                    api_calls += escalated["api_calls"]
                    cache_hits += int(escalated["cache_hit"])
                    total_usage.update(escalated["usage"])
                    for row in escalated["result"]["rows"]:
                        row_reviews[row["row_id"]] = row
            for row_id in expected_ids:
                reviews[row_id] = {
                    "review": row_reviews[row_id],
                    "response_id": response_id,
                    "response_model": response_model,
                    "cache_key": key,
                    "cache_hit": cache_hit,
                    "verification_basis": batch.verification_basis,
                }
            if escalated:
                for row in escalated["result"]["rows"]:
                    reviews[row["row_id"]].update(
                        response_id=escalated["manifest"].get("response_id"),
                        response_model=escalated["manifest"].get("response_model"),
                        cache_key=escalated["cache_key"],
                        cache_hit=escalated["cache_hit"],
                    )
            batch_reports.append(
                {
                    "escalation": {
                        key: value
                        for key, value in escalated.items()
                        if key != "result"
                    }
                    if escalated
                    else None,
                    "cache_key": key,
                    "cache_hit": cache_hit,
                    "source_file": batch.source_file,
                    "source_sha256": batch.source_sha256,
                    "page": batch.page_idx + 1 if batch.page_idx is not None else None,
                    "page_idx": batch.page_idx,
                    "batch_index": batch.batch_index,
                    "rows": len(batch.records),
                    "verification_basis": batch.verification_basis,
                    "source_warning": batch.source_warning,
                    "response_id": response_id,
                    "response_model": response_model,
                    "validation_retries": validation_retry_count,
                    "document_quality": result["document_quality"],
                    "summary": result["summary"],
                    "table_findings": result["table_findings"],
                    "usage": usage,
                }
            )
            print(
                "openai_process: "
                f"batch {len(batch_reports)}/{len(batches)} "
                f"rows={len(batch.records)} cache_hit={str(cache_hit).lower()} "
                f"validation_retries={validation_retry_count}",
                file=sys.stderr,
                flush=True,
            )

        if set(reviews) != {record.row_id for record in records}:
            raise ResponseValidationError(
                "Not every workbook row has an OpenAI review."
            )

        processed_xlsx = run_dir / "master_processed.xlsx"
        review_jsonl = run_dir / "review.jsonl"
        _write_processed_workbook(
            processed_xlsx, input_headers, records, reviews, config.model
        )
        processed_validation = _validate_processed_workbook(
            processed_xlsx, input_headers, records
        )
        _write_jsonl(
            review_jsonl,
            (
                {
                    "row_id": record.row_id,
                    **record.values,
                    **reviews[record.row_id],
                }
                for record in records
            ),
        )
        verdicts = Counter(joined["review"]["verdict"] for joined in reviews.values())
        report = {
            **run_context,
            "status": "success",
            "run_id": run_id,
            "created_at": _utc_now().isoformat(),
            "processing_time": round(time.monotonic() - started, 3),
            "api_calls": api_calls,
            "cache_hits": cache_hits,
            "validation_retries_for_new_api_calls": validation_retries_total,
            "forced": config.force,
            "verdicts": dict(sorted(verdicts.items())),
            "response_models": sorted(
                {
                    str(joined.get("response_model") or config.model)
                    for joined in reviews.values()
                }
            ),
            "usage_for_new_api_calls": dict(total_usage),
            "estimated_cost_for_new_api_calls": (
                (
                    total_usage["input_tokens"] * config.input_price_per_million
                    + total_usage["output_tokens"] * config.output_price_per_million
                )
                / 1e6
                if config.input_price_per_million is not None
                and config.output_price_per_million is not None
                and not config.escalation_model
                else None
            ),
            "processed_xlsx": str(processed_xlsx),
            "processed_xlsx_sha256": _sha256_file(processed_xlsx),
            "review_jsonl": str(review_jsonl),
            "review_jsonl_sha256": _sha256_file(review_jsonl),
            "processed_validation": processed_validation,
            "batch_reports": batch_reports,
        }
        _atomic_write_json(run_dir / "manifest.json", report)
        _atomic_write_json(
            config.output_root / "latest.json",
            {
                "status": "success",
                "run_id": run_id,
                "manifest": str(run_dir / "manifest.json"),
                "processed_xlsx": str(processed_xlsx),
                "review_jsonl": str(review_jsonl),
                "updated_at": _utc_now().isoformat(),
            },
        )
        _atomic_copy(processed_xlsx, config.output_root / "master_processed.xlsx")
        _atomic_copy(review_jsonl, config.output_root / "review_latest.jsonl")
        log_name = f"openai-processing-{run_id}.json"
        _atomic_write_json(config.logs_root / log_name, report)
        _atomic_write_json(config.logs_root / "openai-processing-latest.json", report)
        return report
    except BaseException as exc:
        failure = {
            **run_context,
            "status": "failed",
            "run_id": run_id,
            "created_at": _utc_now().isoformat(),
            "processing_time": round(time.monotonic() - started, 3),
            "api_calls": api_calls,
            "cache_hits": cache_hits,
            "validation_retries_for_new_api_calls": validation_retries_total,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "batch_reports": batch_reports,
        }
        _atomic_write_json(run_dir / "manifest.json", failure)
        _atomic_write_json(config.logs_root / "openai-processing-latest.json", failure)
        raise


def plan_requests(config: Config, mode: str) -> dict[str, Any]:
    """Validate actual request payloads locally; never instantiate an API client."""
    _validate_api_base(config)
    if mode == "page_completeness":
        import page_completeness as completeness

        manifest, _, pages = completeness.prepare_pages(config)
        requests = [
            (batch, completeness.build_payload(batch, config, manifest))
            for batch, _ in pages
        ]
    else:
        selections, _ = load_parse_selection(config.selection_manifest)
        _, records = load_workbook_records(
            config.input_path,
            config.source_filters,
            config.source_sha_filters,
            selections,
        )
        batches = build_batches(
            records,
            config.catalogs_root,
            config.batch_rows,
            config.include_source_page,
            {sha: Path(path) for sha, path in config.source_pdf_overrides},
        )
        requests = [(batch, build_request_payload(batch, config)) for batch in batches]
    report = {
        "status": "dry_run",
        "mode": mode,
        "api_calls": 0,
        "page_scope": "selected" if config.selected_pages_only else "catalog",
        "input": str(config.input_path),
        "input_sha256": _sha256_file(config.input_path),
        "model": config.model,
        "requests": [
            {
                "source_sha256": batch.source_sha256,
                "page_idx": batch.page_idx,
                "rows": len(batch.records),
                "source_pdf_attached": batch.source_page_pdf is not None,
                "payload_sha256": _sha256_json(payload),
                "store": payload["store"],
                "strict_schema": payload["text"]["format"]["strict"],
            }
            for batch, payload in requests
        ],
    }
    _atomic_write_json(
        config.output_root / "plans" / f"{mode}-{_timestamp()}-{uuid.uuid4().hex}.json",
        report,
    )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review MinerU master_raw.xlsx with the OpenAI Responses API."
    )
    parser.add_argument("--input", type=Path, default=Path("/parsed/master_raw.xlsx"))
    parser.add_argument(
        "--mode",
        choices=["row_validation", "page_completeness"],
        default="row_validation",
    )
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--output-root", type=Path, default=Path("/parsed/openai"))
    parser.add_argument("--logs-root", type=Path, default=Path("/logs"))
    parser.add_argument("--catalogs-root", type=Path, default=Path("/catalogs"))
    parser.add_argument(
        "--api-base", default=os.getenv("OPENAI_API_BASE", DEFAULT_API_BASE)
    )
    parser.add_argument(
        "--escalation-model", default=os.getenv("OPENAI_ESCALATION_MODEL") or None
    )
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
    )
    parser.add_argument(
        "--batch-rows", type=int, default=_env_int("OPENAI_BATCH_ROWS", 50)
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=_env_int("OPENAI_TIMEOUT_SECONDS", 600)
    )
    parser.add_argument(
        "--max-retries", type=int, default=_env_int("OPENAI_MAX_RETRIES", 4)
    )
    parser.add_argument(
        "--validation-retries",
        type=int,
        default=_env_int("OPENAI_VALIDATION_RETRIES", 2),
        help="Retry a paid response that fails deterministic structured-output validation.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=_env_int("OPENAI_MAX_OUTPUT_TOKENS", 32000),
    )
    parser.add_argument(
        "--include-source-page",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("OPENAI_INCLUDE_SOURCE_PAGE", True),
    )
    selection_default = os.getenv(
        "OPENAI_SELECTION_MANIFEST", "/catalogs/manifests/guhring-production.yaml"
    ).strip()
    parser.add_argument(
        "--selection-manifest",
        type=Path,
        default=Path(selection_default) if selection_default else None,
        help="Use only source/page/cache_key selections registered in this manifest.",
    )
    parser.add_argument(
        "--all-parser-attempts",
        action="store_true",
        help="Process every RAW parser attempt instead of the manifest-selected attempts.",
    )
    parser.add_argument("--source-file", action="append", default=[])
    parser.add_argument(
        "--source-sha256",
        action="append",
        default=[],
        help="Process rows whose source_sha256 equals this value.",
    )
    parser.add_argument(
        "--source-pdf",
        action="append",
        type=Path,
        default=[],
        help="Process rows whose source_sha256 matches this local PDF.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--selected-pages-only",
        action="store_true",
        help="Page review: inspect pinned or reviewed-empty pages only; full catalog acceptance still checks every registered page.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate PDF selection and request payloads locally without calling the API.",
    )
    parser.add_argument("--allow-insecure-api-base", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if args.timeout_seconds < 1:
        raise ConfigurationError("--timeout-seconds must be positive.")
    if args.max_retries < 0 or args.max_retries > 10:
        raise ConfigurationError("--max-retries must be between 0 and 10.")
    if args.validation_retries < 0 or args.validation_retries > 5:
        raise ConfigurationError("--validation-retries must be between 0 and 5.")
    for price in (args.input_price_per_million, args.output_price_per_million):
        if price is not None and (not math.isfinite(price) or price < 0):
            raise ConfigurationError("Token prices must be finite nonnegative values.")
    if args.max_output_tokens < 1:
        raise ConfigurationError("--max-output-tokens must be positive.")
    source_sha_filters = [value.strip().casefold() for value in args.source_sha256]
    source_pdf_overrides: list[tuple[str, str]] = []
    for source_pdf in args.source_pdf:
        if not source_pdf.is_file():
            raise ConfigurationError(f"--source-pdf does not exist: {source_pdf}")
        source_sha = _sha256_file(source_pdf)
        source_sha_filters.append(source_sha)
        source_pdf_overrides.append((source_sha, str(source_pdf.resolve())))
    for value in source_sha_filters:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ConfigurationError(f"Invalid source SHA-256 filter: {value!r}")
    return Config(
        input_path=args.input,
        output_root=args.output_root,
        logs_root=args.logs_root,
        catalogs_root=args.catalogs_root,
        api_base=args.api_base,
        api_key=_load_api_key(),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        batch_rows=args.batch_rows,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        validation_retries=args.validation_retries,
        max_output_tokens=args.max_output_tokens,
        include_source_page=args.include_source_page,
        selection_manifest=None
        if args.all_parser_attempts
        else args.selection_manifest,
        allow_insecure_api_base=args.allow_insecure_api_base,
        force=args.force,
        source_filters=tuple(args.source_file),
        source_sha_filters=tuple(source_sha_filters),
        source_pdf_overrides=tuple(source_pdf_overrides),
        escalation_model=args.escalation_model,
        input_price_per_million=args.input_price_per_million,
        output_price_per_million=args.output_price_per_million,
        organization=os.getenv("OPENAI_ORG_ID", "").strip() or None,
        project=os.getenv("OPENAI_PROJECT_ID", "").strip() or None,
        selected_pages_only=args.selected_pages_only,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        config = config_from_args(args)
        if args.dry_run:
            report = plan_requests(config, args.mode)
        elif args.mode == "page_completeness":
            from page_completeness import process_pages

            report = process_pages(
                config,
                input_price=args.input_price_per_million,
                output_price=args.output_price_per_million,
            )
        else:
            report = process_workbook(config)
    except ProcessingError as exc:
        print(f"openai_process: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("openai_process: interrupted", file=sys.stderr)
        return 130
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
