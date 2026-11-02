#!/usr/bin/env python3
"""Export MinerU table output to a lossless, provenance-rich XLSX workbook.

The script intentionally treats extracted values as text.  In particular it
does not coerce decimal commas, article numbers, dimensions, tolerances, or
parameter names.  It understands MinerU's legacy content list, the v3 content
list, and middle.json layouts.  The public functions are also used by
``validate_parsing.py`` so discovery and counting have one definition.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from numbers import Real
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from lxml import etree
from lxml import html as lxml_html
from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE


SCHEMA_VERSION = 1
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_TEXT = 32_767

REQUIRED_COLUMNS = [
    "source_file",
    "page",
    "product_family",
    "article_number",
    "raw_parameter_name",
    "raw_value",
    "table_index",
    "row_index",
    "column_index",
    "bbox",
    "parser",
]

PROVENANCE_COLUMNS = [
    "page_idx",
    "source_sha256",
    "parser_version",
    "parser_backend",
    "header_path",
    "cell_tag",
    "rowspan",
    "colspan",
    "bbox_scope",
    "bbox_units",
    "cache_key",
    "artifact_path",
]

ALL_COLUMNS = REQUIRED_COLUMNS + PROVENANCE_COLUMNS
INTEGER_COLUMNS = {
    "page",
    "table_index",
    "row_index",
    "column_index",
    "page_idx",
    "rowspan",
    "colspan",
}

ARTICLE_HEADERS = {
    "art.-nr.",
    "art.-nr",
    "art. nr.",
    "artikel-nr.",
    "artikelnummer",
    "article number",
    "article no.",
    "article no",
}

GUHRING_PARAMETER_HEADERS = {
    "d1",
    "d2",
    "d3",
    "l1",
    "l2",
    "l3",
    *ARTICLE_HEADERS,
}

ARTIFACT_SUFFIXES = {
    "legacy": "_content_list.json",
    "v2": "_content_list_v2.json",
    "middle": "_middle.json",
}

ARTIFACT_PRIORITY = {"legacy": 0, "v2": 1, "middle": 2}


@dataclass
class ParseIssue:
    code: str
    message: str
    path: str | None = None
    document: str | None = None
    page_idx: int | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "document": self.document,
            "page_idx": self.page_idx,
            "severity": self.severity,
        }


@dataclass
class ArtifactMetadata:
    source_file: str
    source_sha256: str | None = None
    config_digest: str | None = None
    product_family: str | None = None
    parser_version: str | None = None
    parser_backend: str | None = None
    cache_key: str | None = None
    page_idx: int | None = None
    pages_total: int | None = None
    processing_time: float | None = None
    manifest_path: str | None = None

    @property
    def parser_label(self) -> str:
        label = "MinerU"
        if self.parser_version:
            label += f" {self.parser_version}"
        if self.parser_backend:
            label += f" ({self.parser_backend})"
        return label


@dataclass
class ArtifactCandidate:
    path: Path
    kind: str
    document_stem: str
    metadata: ArtifactMetadata


@dataclass
class TableBlock:
    page_idx: int | None
    html: str
    bbox: list[int | float] | None
    bbox_units: str | None
    source_order: int
    artifact_path: str
    cache_key: str | None = None


@dataclass
class ArtifactDocument:
    metadata: ArtifactMetadata
    observed_pages: set[int] = field(default_factory=set)
    table_blocks: list[TableBlock] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


@dataclass(eq=False)
class HtmlCell:
    row_index: int
    column_index: int
    text: str
    tag: str
    rowspan: int
    colspan: int
    in_thead: bool


@dataclass
class ParsedTable:
    page_idx: int | None
    table_index: int
    bbox: list[int | float] | None
    bbox_units: str | None
    source_order: tuple[int, int]
    rows_detected: int
    cells: list[HtmlCell]
    grid: dict[tuple[int, int], HtmlCell]
    header_rows: set[int]
    logical_width: int
    artifact_path: str
    cache_key: str | None
    raw_html_sha256: str
    has_merged_cells: bool


@dataclass
class ScannedDocument:
    source_file: str
    source_sha256: str | None = None
    config_digest: str | None = None
    product_family: str | None = None
    parser_version: str | None = None
    parser_backend: str | None = None
    cache_key: str | None = None
    pages_total: int | None = None
    processing_times: dict[str, float] = field(default_factory=dict)
    observed_pages: set[int] = field(default_factory=set)
    tables: list[ParsedTable] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def parser_label(self) -> str:
        metadata = ArtifactMetadata(
            source_file=self.source_file,
            parser_version=self.parser_version,
            parser_backend=self.parser_backend,
        )
        return metadata.parser_label


@dataclass
class ScanResult:
    documents: list[ScannedDocument]
    issues: list[ParseIssue]
    artifacts: list[str]

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)


def _json_load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    # MinerU's JSON API embeds content_list and middle_json as JSON strings.
    for _ in range(2):
        if not isinstance(payload, str):
            break
        payload = json.loads(payload)
    return payload


def _decode_embedded_json(value: Any) -> Any:
    for _ in range(2):
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if not stripped.startswith(("[", "{")):
            return value
        value = json.loads(value)
    return value


def _nested_get(payload: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        current: Any = payload
        for part in path:
            if not isinstance(current, Mapping) or part not in current:
                break
            current = current[part]
        else:
            if current is not None:
                return current
    return None


def _as_nonempty_string(value: Any) -> str | None:
    if isinstance(value, (str, Path)):
        text = str(value).strip()
        return text or None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _artifact_kind_and_stem(path: Path) -> tuple[str, str] | None:
    for kind, suffix in sorted(
        ARTIFACT_SUFFIXES.items(), key=lambda item: len(item[1]), reverse=True
    ):
        if path.name.endswith(suffix):
            return kind, path.name[: -len(suffix)]
    return None


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _success_attempt_roots(
    root: Path, pointer_paths: Sequence[Path] | None = None
) -> dict[Path, set[Path]]:
    """Resolve common success.json pointer layouts without requiring one schema."""

    resolved: dict[Path, set[Path]] = {}
    interesting_keys = {
        "attempt",
        "attempt_dir",
        "attempt_id",
        "active_attempt",
        "successful_attempt",
        "current",
        "current_attempt",
        "manifest",
        "manifest_path",
        "result_dir",
        "path",
    }

    def collect(payload: Any, values: list[tuple[str, str]]) -> None:
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if key in interesting_keys and isinstance(value, (str, Path)):
                    values.append((key, str(value)))
                elif isinstance(value, (Mapping, list)):
                    collect(value, values)
        elif isinstance(payload, list):
            for item in payload:
                collect(item, values)

    for pointer in sorted(pointer_paths if pointer_paths is not None else root.rglob("success.json")):
        try:
            payload = _json_load(pointer)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        values: list[tuple[str, str]] = []
        collect(payload, values)
        active: set[Path] = set()
        attempts_root = (pointer.parent / "attempts").resolve()
        for key, raw in values:
            raw_path = Path(raw)
            candidates = []
            if raw_path.is_absolute():
                candidates.append(raw_path)
            else:
                candidates.extend((pointer.parent / raw_path, root / raw_path))
                if key in {"attempt", "attempt_id", "active_attempt", "current_attempt"}:
                    candidates.append(pointer.parent / "attempts" / raw_path)
            for candidate in candidates:
                candidate = candidate.resolve()
                if candidate.is_file():
                    candidate = candidate.parent
                if candidate.exists() and _path_is_relative_to(candidate, attempts_root):
                    active.add(candidate)
        if active:
            resolved[pointer.parent.resolve()] = active
    return resolved


def _nearest_manifest(path: Path, root: Path) -> tuple[Path | None, Mapping[str, Any] | None]:
    current = path.parent.resolve()
    root_resolved = root.resolve()
    while _path_is_relative_to(current, root_resolved):
        for manifest_name in ("cache_manifest.json", "manifest.json"):
            manifest_path = current / manifest_name
            if manifest_path.is_file():
                try:
                    payload = _json_load(manifest_path)
                    if isinstance(payload, Mapping):
                        return manifest_path, payload
                except (OSError, ValueError, json.JSONDecodeError):
                    return manifest_path, None
        if current == root_resolved:
            break
        current = current.parent
    return None, None


def _metadata_from_manifest(
    manifest: Mapping[str, Any] | None,
    manifest_path: Path | None,
    document_stem: str,
) -> ArtifactMetadata:
    payload = manifest or {}
    source_file = _as_nonempty_string(
        _nested_get(
            payload,
            ("source_file",),
            ("source_path",),
            ("file",),
            ("input_file",),
            ("original_file",),
            ("source", "path"),
            ("source", "file"),
            ("source", "name"),
            ("document", "source_file"),
            ("document", "path"),
            ("input", "path"),
        )
    ) or document_stem
    # The API/cache records the upload basename while batch manifests may use
    # an absolute mounted path for the same bytes.  A basename keeps those
    # immutable copies associated with one source document.
    source_file = Path(source_file).name

    source_sha256 = _as_nonempty_string(
        _nested_get(
            payload,
            ("source_sha256",),
            ("file_sha256",),
            ("file_hash",),
            ("source", "sha256"),
            ("source", "hash"),
            ("document", "sha256"),
        )
    )
    config_digest = _as_nonempty_string(
        _nested_get(payload, ("config_digest",), ("config", "digest"))
    )
    if config_digest is None and isinstance(payload.get("config"), Mapping):
        config_digest = hashlib.sha256(
            json.dumps(
                payload["config"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    product_family = _as_nonempty_string(
        _nested_get(
            payload,
            ("product_family",),
            ("source", "product_family"),
            ("document", "product_family"),
        )
    )
    parser_version = _as_nonempty_string(
        _nested_get(
            payload,
            ("parser_version",),
            ("mineru_version",),
            ("parser", "version"),
            ("mineru", "version"),
            ("config", "mineru_version"),
        )
    )
    parser_backend = _as_nonempty_string(
        _nested_get(
            payload,
            ("parser_backend",),
            ("backend",),
            ("parser", "backend"),
            ("mineru", "backend"),
            ("config", "backend"),
            ("config", "request", "backend"),
        )
    )
    cache_key = _as_nonempty_string(
        _nested_get(payload, ("cache_key",), ("key", "cache_key"))
    )
    page_idx = _as_int(
        _nested_get(
            payload,
            ("source_page_idx",),
            ("page_idx",),
            ("page", "page_idx"),
            ("page",),
        )
    )
    pages_total = _as_int(
        _nested_get(
            payload,
            ("pages_total",),
            ("page_count",),
            ("source", "pages"),
            ("document", "pages"),
        )
    )
    if pages_total is None and "start_page" not in payload and "end_page" not in payload:
        pages_total = _as_int(payload.get("pages"))
    processing_time = _as_float(
        _nested_get(
            payload,
            ("processing_time",),
            ("processing_time_seconds",),
            ("elapsed_seconds",),
            ("duration_seconds",),
            ("timing", "processing_seconds"),
        )
    )
    return ArtifactMetadata(
        source_file=source_file,
        source_sha256=source_sha256,
        config_digest=config_digest,
        product_family=product_family,
        parser_version=parser_version,
        parser_backend=parser_backend,
        cache_key=cache_key,
        page_idx=page_idx,
        pages_total=pages_total,
        processing_time=processing_time,
        manifest_path=str(manifest_path) if manifest_path else None,
    )


def discover_artifacts(root: Path) -> tuple[list[ArtifactCandidate], list[ParseIssue]]:
    root = root.resolve()
    issues: list[ParseIssue] = []
    if not root.is_dir():
        return [], [
            ParseIssue("parsed_root_missing", f"Parsed root does not exist: {root}", str(root))
        ]

    # ``/parsed`` also contains derived ETL/OpenAI caches.  Their success
    # pointers have intentionally different contracts and must never be
    # interpreted as MinerU page-cache authorities.
    excluded_top_level = {"etl", "openai"}

    def belongs_to_mineru_scope(path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(root)
        except ValueError:
            return False
        return not relative.parts or relative.parts[0] not in excluded_top_level

    # MinerU's authority is a per-page pointer. ETL/review pointers can live
    # beneath any output directory, including nested integration-test runs.
    pointer_paths = sorted(
        path
        for path in root.rglob("success.json")
        if belongs_to_mineru_scope(path) and path.parent.parent.name == "pages"
    )
    active_roots_by_page = _success_attempt_roots(root, pointer_paths)
    active_roots = sorted(
        {path for paths in active_roots_by_page.values() for path in paths},
        key=str,
    )
    for pointer_path in pointer_paths:
        if pointer_path.parent.resolve() not in active_roots_by_page:
            issues.append(
                ParseIssue(
                    "active_cache_pointer_invalid",
                    "Cache success pointer does not resolve to an active attempt.",
                    str(pointer_path),
                )
            )

    # A cache success pointer is the authority for the current parse.  Raw
    # archives, derived document ZIPs, and MinerU's transient API task tree are
    # immutable/protocol copies, not additional logical parses.  When no cache
    # exists, retain support for standalone MinerU fixture/output directories.
    search_roots = active_roots if pointer_paths else [root]
    excluded_fallback_roots = {"raw", "documents", "_api_tasks", *excluded_top_level}
    candidates: list[ArtifactCandidate] = []
    paths = sorted(
        {
            path
            for search_root in search_roots
            for path in search_root.rglob("*.json")
            if not (
                not pointer_paths
                and path.is_relative_to(root)
                and path.relative_to(root).parts
                and path.relative_to(root).parts[0] in excluded_fallback_roots
            )
        },
        key=str,
    )
    for path in paths:
        kind_stem = _artifact_kind_and_stem(path)
        if kind_stem is None:
            continue
        kind, document_stem = kind_stem
        manifest_path, manifest = _nearest_manifest(path, root)
        if manifest_path and manifest is None:
            issues.append(
                ParseIssue(
                    "invalid_manifest",
                    "The nearest manifest could not be decoded.",
                    str(manifest_path),
                    document_stem,
                )
            )
        if manifest:
            status = _as_nonempty_string(manifest.get("status"))
            if status and status.casefold() not in {"success", "succeeded", "complete", "completed"}:
                continue
        metadata = _metadata_from_manifest(manifest, manifest_path, document_stem)
        candidates.append(ArtifactCandidate(path, kind, document_stem, metadata))

    # Each MinerU parse directory commonly contains all three representations.
    # Prefer the simplified legacy list, then v2, then middle.json.
    grouped: dict[tuple[Path, str], list[ArtifactCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.path.parent.resolve(), candidate.document_stem), []).append(
            candidate
        )
    selected = [
        min(group, key=lambda candidate: (ARTIFACT_PRIORITY[candidate.kind], str(candidate.path)))
        for group in grouped.values()
    ]
    selected.sort(key=lambda candidate: str(candidate.path))
    if not selected:
        issues.append(
            ParseIssue(
                "no_mineru_artifacts",
                f"No supported MinerU JSON artifacts were found under {root}.",
                str(root),
            )
        )
    return selected, issues


def _valid_bbox(
    value: Any,
    issues: list[ParseIssue],
    path: str,
    document: str,
    page_idx: int | None,
) -> list[int | float] | None:
    if value is None:
        return None
    if (
        isinstance(value, list)
        and len(value) == 4
        and all(
            isinstance(number, Real)
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            for number in value
        )
        and value[0] <= value[2]
        and value[1] <= value[3]
    ):
        return list(value)
    issues.append(
        ParseIssue(
            "invalid_bbox",
            f"Expected bbox [x0,y0,x1,y1], got {value!r}.",
            path,
            document,
            page_idx,
        )
    )
    return None


def _table_html_from_block(block: Mapping[str, Any]) -> str | None:
    for key in ("table_body", "html"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = block.get("content")
    if isinstance(content, Mapping):
        for key in ("html", "table_body"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value
    elif isinstance(content, str) and "<table" in content.casefold():
        return content
    return None


def _middle_table_html(block: Mapping[str, Any]) -> str | None:
    direct = _table_html_from_block(block)
    if direct:
        return direct
    for child in block.get("blocks", []) or []:
        if not isinstance(child, Mapping) or child.get("type") != "table_body":
            continue
        for line in child.get("lines", []) or []:
            if not isinstance(line, Mapping):
                continue
            for span in line.get("spans", []) or []:
                if isinstance(span, Mapping) and span.get("type") == "table":
                    value = span.get("html")
                    if isinstance(value, str) and value.strip():
                        return value
    return None


def _apply_single_page_override(document: ArtifactDocument) -> None:
    override = document.metadata.page_idx
    if override is None:
        return
    pages = set(document.observed_pages)
    if len(pages) <= 1:
        old_page = next(iter(pages), None)
        document.observed_pages = {override}
        for table in document.table_blocks:
            if table.page_idx == old_page or table.page_idx is None:
                table.page_idx = override


def _parse_legacy(
    payload: Any, metadata: ArtifactMetadata, path: Path
) -> list[ArtifactDocument]:
    document = ArtifactDocument(metadata=copy.copy(metadata))
    if not isinstance(payload, list) or any(isinstance(item, list) for item in payload):
        document.issues.append(
            ParseIssue(
                "unexpected_legacy_schema",
                "Legacy content list must be a flat JSON array of blocks.",
                str(path),
                metadata.source_file,
            )
        )
        return [document]
    table_order_by_page: dict[int | None, int] = {}
    for block in payload:
        if not isinstance(block, Mapping):
            continue
        page_idx = _as_int(block.get("source_page_idx"))
        if page_idx is None:
            page_idx = _as_int(block.get("page_idx"))
        if page_idx is not None:
            document.observed_pages.add(page_idx)
        if block.get("type") != "table":
            continue
        table_order = table_order_by_page.get(page_idx, 0)
        table_order_by_page[page_idx] = table_order + 1
        html = _table_html_from_block(block)
        if not html:
            document.issues.append(
                ParseIssue(
                    "table_html_missing",
                    "MinerU reported a table block without HTML table content.",
                    str(path),
                    metadata.source_file,
                    page_idx,
                )
            )
            continue
        bbox = _valid_bbox(
            block.get("bbox"), document.issues, str(path), metadata.source_file, page_idx
        )
        document.table_blocks.append(
            TableBlock(
                page_idx,
                html,
                bbox,
                "normalized_0_1000",
                table_order,
                str(path),
                metadata.cache_key,
            )
        )
    _apply_single_page_override(document)
    return [document]


def _parse_v2(payload: Any, metadata: ArtifactMetadata, path: Path) -> list[ArtifactDocument]:
    document = ArtifactDocument(metadata=copy.copy(metadata))
    if not isinstance(payload, list) or any(not isinstance(page, list) for page in payload):
        document.issues.append(
            ParseIssue(
                "unexpected_v2_schema",
                "content_list_v2 must be an array containing one block array per page.",
                str(path),
                metadata.source_file,
            )
        )
        return [document]
    for outer_page_idx, blocks in enumerate(payload):
        page_idx = outer_page_idx
        document.observed_pages.add(page_idx)
        table_order = 0
        for block in blocks:
            if not isinstance(block, Mapping) or block.get("type") != "table":
                continue
            html = _table_html_from_block(block)
            if not html:
                document.issues.append(
                    ParseIssue(
                        "table_html_missing",
                        "MinerU v2 reported a table block without content.html.",
                        str(path),
                        metadata.source_file,
                        page_idx,
                    )
                )
                continue
            bbox = _valid_bbox(
                block.get("bbox"), document.issues, str(path), metadata.source_file, page_idx
            )
            document.table_blocks.append(
                TableBlock(
                    page_idx,
                    html,
                    bbox,
                    "normalized_0_1000",
                    table_order,
                    str(path),
                    metadata.cache_key,
                )
            )
            table_order += 1
    _apply_single_page_override(document)
    return [document]


def _parse_middle(
    payload: Any, metadata: ArtifactMetadata, path: Path
) -> list[ArtifactDocument]:
    document = ArtifactDocument(metadata=copy.copy(metadata))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("pdf_info"), list):
        document.issues.append(
            ParseIssue(
                "unexpected_middle_schema",
                "middle.json must be an object with a pdf_info array.",
                str(path),
                metadata.source_file,
            )
        )
        return [document]
    version = _as_nonempty_string(payload.get("_version_name"))
    backend = _as_nonempty_string(payload.get("_backend"))
    if version:
        document.metadata.parser_version = version
    if backend:
        document.metadata.parser_backend = backend
    for outer_page_idx, page in enumerate(payload["pdf_info"]):
        if not isinstance(page, Mapping):
            continue
        page_idx = _as_int(page.get("source_page_idx"))
        if page_idx is None:
            page_idx = _as_int(page.get("page_idx"))
        if page_idx is None:
            page_idx = outer_page_idx
        document.observed_pages.add(page_idx)
        blocks = page.get("para_blocks")
        if not isinstance(blocks, list):
            blocks = page.get("tables") if isinstance(page.get("tables"), list) else []
        table_order = 0
        for block in blocks:
            if not isinstance(block, Mapping) or block.get("type") != "table":
                continue
            html = _middle_table_html(block)
            if not html:
                document.issues.append(
                    ParseIssue(
                        "table_html_missing",
                        "MinerU middle.json reported a table without a table span html value.",
                        str(path),
                        metadata.source_file,
                        page_idx,
                    )
                )
                continue
            bbox = _valid_bbox(
                block.get("bbox"), document.issues, str(path), metadata.source_file, page_idx
            )
            document.table_blocks.append(
                TableBlock(
                    page_idx,
                    html,
                    bbox,
                    "page_coordinates",
                    table_order,
                    str(path),
                    metadata.cache_key,
                )
            )
            table_order += 1
    _apply_single_page_override(document)
    return [document]


def _parse_api_envelope(
    payload: Mapping[str, Any], metadata: ArtifactMetadata, path: Path
) -> list[ArtifactDocument]:
    results = payload.get("results")
    if not isinstance(results, Mapping):
        return []
    version = _as_nonempty_string(payload.get("version"))
    backend = _as_nonempty_string(payload.get("backend"))
    documents: list[ArtifactDocument] = []
    for result_name, result in results.items():
        if not isinstance(result, Mapping):
            continue
        result_source = metadata.source_file if metadata.manifest_path else Path(str(result_name)).name
        result_metadata = replace(metadata, source_file=result_source)
        if version:
            result_metadata.parser_version = version
        if backend:
            result_metadata.parser_backend = backend
        try:
            if result.get("content_list") is not None:
                nested = _decode_embedded_json(result["content_list"])
                if isinstance(nested, list) and all(isinstance(page, list) for page in nested):
                    documents.extend(_parse_v2(nested, result_metadata, path))
                else:
                    documents.extend(_parse_legacy(nested, result_metadata, path))
            elif result.get("middle_json") is not None:
                nested = _decode_embedded_json(result["middle_json"])
                documents.extend(_parse_middle(nested, result_metadata, path))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            failed = ArtifactDocument(result_metadata)
            failed.issues.append(
                ParseIssue(
                    "embedded_json_invalid",
                    f"Could not decode MinerU API result JSON: {exc}",
                    str(path),
                    str(result_name),
                )
            )
            documents.append(failed)
    return documents


def parse_artifact(candidate: ArtifactCandidate) -> list[ArtifactDocument]:
    try:
        payload = _json_load(candidate.path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failed = ArtifactDocument(candidate.metadata)
        failed.issues.append(
            ParseIssue(
                "artifact_json_invalid",
                f"Could not decode JSON: {exc}",
                str(candidate.path),
                candidate.metadata.source_file,
            )
        )
        return [failed]

    if isinstance(payload, Mapping) and isinstance(payload.get("results"), Mapping):
        return _parse_api_envelope(payload, candidate.metadata, candidate.path)
    if candidate.kind == "middle":
        return _parse_middle(payload, candidate.metadata, candidate.path)
    if candidate.kind == "v2":
        return _parse_v2(payload, candidate.metadata, candidate.path)
    # Be tolerant when a v3 list was saved with the legacy suffix.
    if isinstance(payload, list) and all(isinstance(page, list) for page in payload):
        return _parse_v2(payload, candidate.metadata, candidate.path)
    return _parse_legacy(payload, candidate.metadata, candidate.path)


def _local_tag(element: etree._Element) -> str:
    if not isinstance(element.tag, str):
        return ""
    return etree.QName(element).localname.casefold()


BLOCK_TAGS = {
    "address",
    "article",
    "div",
    "header",
    "footer",
    "li",
    "p",
    "section",
}


def _visible_cell_text(cell: etree._Element) -> str:
    parts: list[str] = []

    def visit(node: etree._Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = _local_tag(child)
            if tag in {"script", "style"}:
                if child.tail:
                    parts.append(child.tail)
                continue
            if tag == "br":
                parts.append("\n")
            else:
                visit(child)
                if tag in BLOCK_TAGS:
                    parts.append("\n")
            if child.tail:
                parts.append(child.tail)

    visit(cell)
    raw = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in raw.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _positive_span(value: Any) -> int | None:
    if value is None or value == "":
        return 1
    parsed = _as_int(value)
    if parsed is None or parsed < 1 or parsed > 10_000:
        return None
    return parsed


def _nearest_ancestor_table(element: etree._Element) -> etree._Element | None:
    for ancestor in element.iterancestors():
        if _local_tag(ancestor) == "table":
            return ancestor
    return None


def _header_match(text: str) -> str:
    return " ".join(text.split()).casefold()


def _detect_header_rows(rows: list[list[HtmlCell]]) -> set[int]:
    if len(rows) <= 1:
        return set()
    thead_rows = {
        row_index
        for row_index, cells in enumerate(rows)
        if cells and all(cell.in_thead for cell in cells)
    }
    if thead_rows:
        return thead_rows

    leading_th_rows: set[int] = set()
    for row_index, cells in enumerate(rows):
        nonempty = [cell for cell in cells if cell.text]
        if nonempty and all(cell.tag == "th" for cell in nonempty):
            leading_th_rows.add(row_index)
        else:
            break
    if leading_th_rows:
        return leading_th_rows

    marker_rows: list[int] = []
    for row_index, cells in enumerate(rows[:6]):
        if any(_header_match(cell.text) in GUHRING_PARAMETER_HEADERS for cell in cells):
            marker_rows.append(row_index)
    if marker_rows:
        end = max(marker_rows)
        return set(range(min(end + 1, len(rows) - 1)))

    # Without structural or vocabulary evidence, treating the first row as a
    # header would discard source data and invent parameter names.  Preserve
    # every physical cell and leave header-derived fields unavailable.
    return set()


def parse_table_block(
    block: TableBlock,
    document: str,
    issues: list[ParseIssue],
) -> list[ParsedTable]:
    parser = lxml_html.HTMLParser(recover=True, remove_comments=False)
    try:
        root = lxml_html.fromstring(block.html, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        issues.append(
            ParseIssue(
                "table_html_invalid",
                f"Could not parse table HTML: {exc}",
                block.artifact_path,
                document,
                block.page_idx,
            )
        )
        return []

    all_tables: list[etree._Element] = []
    if _local_tag(root) == "table":
        all_tables.append(root)
    all_tables.extend(element for element in root.iterdescendants() if _local_tag(element) == "table")
    seen_ids: set[int] = set()
    all_tables = [
        table for table in all_tables if not (id(table) in seen_ids or seen_ids.add(id(table)))
    ]
    top_tables = [table for table in all_tables if _nearest_ancestor_table(table) is None]
    if not top_tables:
        issues.append(
            ParseIssue(
                "table_element_missing",
                "Table HTML did not contain a <table> element.",
                block.artifact_path,
                document,
                block.page_idx,
            )
        )
        return []
    if any(_nearest_ancestor_table(table) is not None for table in all_tables):
        issues.append(
            ParseIssue(
                "nested_table",
                "Nested table markup was retained inside its outer MinerU table cell.",
                block.artifact_path,
                document,
                block.page_idx,
                "warning",
            )
        )

    parsed_tables: list[ParsedTable] = []
    for nested_table_index, html_table in enumerate(top_tables):
        row_elements = [
            element
            for element in html_table.iterdescendants()
            if _local_tag(element) == "tr" and _nearest_ancestor_table(element) is html_table
        ]
        rows: list[list[HtmlCell]] = []
        grid: dict[tuple[int, int], HtmlCell] = {}
        cells_flat: list[HtmlCell] = []
        logical_width = 0
        for row_index, row_element in enumerate(row_elements):
            cells: list[HtmlCell] = []
            column_index = 0
            for cell_element in row_element:
                tag = _local_tag(cell_element)
                if tag not in {"th", "td"}:
                    continue
                while (row_index, column_index) in grid:
                    column_index += 1
                rowspan = _positive_span(cell_element.get("rowspan"))
                colspan = _positive_span(cell_element.get("colspan"))
                if rowspan is None or colspan is None:
                    issues.append(
                        ParseIssue(
                            "invalid_cell_span",
                            "Invalid rowspan/colspan was treated as 1.",
                            block.artifact_path,
                            document,
                            block.page_idx,
                            "warning",
                        )
                    )
                    rowspan = rowspan or 1
                    colspan = colspan or 1
                in_thead = any(_local_tag(parent) == "thead" for parent in cell_element.iterancestors())
                cell = HtmlCell(
                    row_index,
                    column_index,
                    _visible_cell_text(cell_element),
                    tag,
                    rowspan,
                    colspan,
                    in_thead,
                )
                cells.append(cell)
                cells_flat.append(cell)
                for target_row in range(row_index, row_index + rowspan):
                    for target_column in range(column_index, column_index + colspan):
                        coordinate = (target_row, target_column)
                        if coordinate in grid:
                            issues.append(
                                ParseIssue(
                                    "overlapping_cell_span",
                                    (
                                        f"Overlapping table span at logical cell {coordinate}; "
                                        "both physical cells were retained and the first cell "
                                        "remains authoritative for header lookup."
                                    ),
                                    block.artifact_path,
                                    document,
                                    block.page_idx,
                                    "warning",
                                )
                            )
                            continue
                        grid[coordinate] = cell
                column_index += colspan
                logical_width = max(logical_width, column_index)
            rows.append(cells)
        if not row_elements:
            issues.append(
                ParseIssue(
                    "table_rows_missing",
                    "A parsed <table> contains no <tr> rows.",
                    block.artifact_path,
                    document,
                    block.page_idx,
                )
            )
        parsed_tables.append(
            ParsedTable(
                page_idx=block.page_idx,
                table_index=-1,
                bbox=block.bbox,
                bbox_units=block.bbox_units,
                source_order=(block.source_order, nested_table_index),
                rows_detected=len(row_elements),
                cells=cells_flat,
                grid=grid,
                header_rows=_detect_header_rows(rows),
                logical_width=logical_width,
                artifact_path=block.artifact_path,
                cache_key=block.cache_key,
                raw_html_sha256=hashlib.sha256(block.html.encode("utf-8")).hexdigest(),
                has_merged_cells=any(cell.rowspan > 1 or cell.colspan > 1 for cell in cells_flat),
            )
        )
    return parsed_tables


def _merge_metadata(document: ScannedDocument, metadata: ArtifactMetadata) -> None:
    for attr in (
        "source_sha256",
        "config_digest",
        "product_family",
        "parser_version",
        "parser_backend",
        "cache_key",
        "pages_total",
    ):
        current = getattr(document, attr)
        incoming = getattr(metadata, attr)
        if current is None and incoming is not None:
            setattr(document, attr, incoming)
    if metadata.processing_time is not None and metadata.processing_time >= 0:
        if metadata.cache_key:
            timing_key = f"page:{metadata.cache_key}"
        elif metadata.manifest_path:
            timing_key = f"aggregate:{metadata.manifest_path}"
        else:
            timing_key = f"aggregate:{len(document.processing_times)}"
        document.processing_times[timing_key] = metadata.processing_time


def _document_identity(
    metadata: ArtifactMetadata, candidate: ArtifactCandidate
) -> tuple[str, str, str, str]:
    """Return an identity that cannot silently combine parser provenance."""

    source_identity = (
        f"sha256:{metadata.source_sha256}"
        if metadata.source_sha256
        else f"source:{metadata.source_file}"
    )
    # Active cache pages share a config digest.  A standalone artifact without
    # one stays isolated to its own manifest/output directory rather than being
    # merged with an unrelated file that happens to have the same basename.
    config_identity = metadata.config_digest
    if config_identity is None:
        config_identity = (
            f"manifest:{Path(metadata.manifest_path).resolve()}"
            if metadata.manifest_path
            else f"artifact:{candidate.path.parent.resolve()}"
        )
    return (
        source_identity,
        config_identity,
        (metadata.parser_backend or "").casefold(),
        metadata.parser_version or "",
    )


def scan_outputs(root: Path | str) -> ScanResult:
    root = Path(root)
    candidates, discovery_issues = discover_artifacts(root)
    documents_by_identity: dict[tuple[str, str, str, str], ScannedDocument] = {}
    all_issues = list(discovery_issues)
    artifact_paths: list[str] = []

    for candidate in candidates:
        artifact_paths.append(str(candidate.path))
        for artifact_document in parse_artifact(candidate):
            metadata = artifact_document.metadata
            identity = _document_identity(metadata, candidate)
            document = documents_by_identity.setdefault(
                identity,
                ScannedDocument(
                    source_file=metadata.source_file,
                    source_sha256=metadata.source_sha256,
                    config_digest=metadata.config_digest,
                    parser_version=metadata.parser_version,
                    parser_backend=metadata.parser_backend,
                ),
            )
            _merge_metadata(document, artifact_document.metadata)
            document.observed_pages.update(artifact_document.observed_pages)
            document.issues.extend(artifact_document.issues)
            all_issues.extend(artifact_document.issues)
            for block in artifact_document.table_blocks:
                issue_count = len(document.issues)
                parsed = parse_table_block(block, document.source_file, document.issues)
                all_issues.extend(document.issues[issue_count:])
                document.tables.extend(parsed)

    for document in documents_by_identity.values():
        ordered = sorted(
            document.tables,
            key=lambda item: (
                item.page_idx is None,
                item.page_idx if item.page_idx is not None else -1,
                item.source_order,
                item.artifact_path,
            ),
        )
        page_indexes: dict[int | None, int] = {}
        for table in ordered:
            table.table_index = page_indexes.get(table.page_idx, 0)
            page_indexes[table.page_idx] = table.table_index + 1
        document.tables = ordered

    documents = sorted(
        documents_by_identity.values(),
        key=lambda item: (
            item.source_file,
            item.source_sha256 or "",
            item.config_digest or "",
            item.parser_backend or "",
            item.parser_version or "",
        ),
    )
    return ScanResult(documents, all_issues, artifact_paths)


def _header_path(table: ParsedTable, column_index: int) -> list[str]:
    result: list[str] = []
    seen: set[int] = set()
    for row_index in sorted(table.header_rows):
        cell = table.grid.get((row_index, column_index))
        if cell is None or id(cell) in seen or not cell.text:
            continue
        seen.add(id(cell))
        result.append(cell.text)
    return result


def _article_column(table: ParsedTable) -> int | None:
    matches: list[int] = []
    for column_index in range(table.logical_width):
        if any(_header_match(value) in ARTICLE_HEADERS for value in _header_path(table, column_index)):
            matches.append(column_index)
    return matches[0] if len(matches) == 1 else None


def iter_document_records(document: ScannedDocument) -> Iterator[dict[str, Any]]:
    for table in sorted(
        document.tables,
        key=lambda item: (
            item.page_idx is None,
            item.page_idx if item.page_idx is not None else -1,
            item.table_index,
        ),
    ):
        article_column = _article_column(table)
        bbox = (
            json.dumps(table.bbox, ensure_ascii=False, separators=(",", ":"))
            if table.bbox is not None
            else None
        )
        for cell in sorted(table.cells, key=lambda item: (item.row_index, item.column_index)):
            if cell.row_index in table.header_rows:
                continue
            path = _header_path(table, cell.column_index)
            article_number: str | None = None
            if article_column is not None:
                article_cell = table.grid.get((cell.row_index, article_column))
                if article_cell is not None and article_cell.row_index not in table.header_rows:
                    article_number = article_cell.text or None
            page = table.page_idx + 1 if table.page_idx is not None and table.page_idx >= 0 else None
            yield {
                "source_file": document.source_file,
                "page": page,
                "product_family": document.product_family,
                "article_number": article_number,
                "raw_parameter_name": path[-1] if path else None,
                "raw_value": cell.text,
                "table_index": table.table_index,
                "row_index": cell.row_index,
                "column_index": cell.column_index,
                "bbox": bbox,
                "parser": document.parser_label,
                "page_idx": table.page_idx,
                "source_sha256": document.source_sha256,
                "parser_version": document.parser_version,
                "parser_backend": document.parser_backend,
                "header_path": (
                    json.dumps(path, ensure_ascii=False, separators=(",", ":")) if path else None
                ),
                "cell_tag": cell.tag,
                "rowspan": cell.rowspan,
                "colspan": cell.colspan,
                "bbox_scope": "table" if table.bbox is not None else None,
                "bbox_units": table.bbox_units,
                "cache_key": table.cache_key or document.cache_key,
                "artifact_path": table.artifact_path,
            }


def iter_records(scan: ScanResult) -> Iterator[dict[str, Any]]:
    for document in scan.documents:
        yield from iter_document_records(document)


def _canonical_record_bytes(record: Mapping[str, Any]) -> bytes:
    payload = [record.get(column) for column in ALL_COLUMNS]
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_only_value(worksheet: Any, column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in INTEGER_COLUMNS and isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value)
    if len(text) > EXCEL_MAX_TEXT:
        raise ValueError(
            f"Value for {column} exceeds Excel's {EXCEL_MAX_TEXT}-character cell limit."
        )
    if ILLEGAL_CHARACTERS_RE.search(text):
        raise ValueError(f"Value for {column} contains an XML-illegal control character.")
    cell = WriteOnlyCell(worksheet, value=text)
    # A source value beginning '=' must remain text, not become an XLSX formula.
    cell.data_type = "s"
    return cell


def _raw_sheet(workbook: Workbook, number: int) -> Any:
    title = "raw" if number == 1 else f"raw_{number:03d}"
    worksheet = workbook.create_sheet(title)
    worksheet.freeze_panes = "A2"
    worksheet.append([_write_only_value(worksheet, column, column) for column in ALL_COLUMNS])
    return worksheet


def _validate_created_workbook(
    path: Path, expected_rows: int, expected_hash: str, raw_sheets: list[str]
) -> list[str]:
    errors: list[str] = []
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        count = 0
        hasher = hashlib.sha256()
        for sheet_name in raw_sheets:
            if sheet_name not in workbook.sheetnames:
                errors.append(f"Missing worksheet {sheet_name!r}.")
                continue
            worksheet = workbook[sheet_name]
            rows = worksheet.iter_rows()
            try:
                header = [cell.value for cell in next(rows)]
            except StopIteration:
                errors.append(f"Worksheet {sheet_name!r} is empty.")
                continue
            if header != ALL_COLUMNS:
                errors.append(f"Worksheet {sheet_name!r} has an unexpected header.")
                continue
            for cells in rows:
                if all(cell.value is None for cell in cells):
                    continue
                record: dict[str, Any] = {}
                for column, cell in zip(ALL_COLUMNS, cells):
                    if cell.data_type == "f":
                        errors.append(
                            f"Formula cell found in {sheet_name}!{cell.coordinate}; raw values must be text."
                        )
                    value = cell.value
                    if column == "raw_value" and value is None:
                        value = ""
                    record[column] = value
                hasher.update(_canonical_record_bytes(record))
                count += 1
        if count != expected_rows:
            errors.append(f"Workbook has {count} records; expected {expected_rows}.")
        if hasher.hexdigest() != expected_hash:
            errors.append("Workbook row hash does not match the exported records.")
    finally:
        workbook.close()
    return errors


def write_workbook(scan: ScanResult, output: Path | str) -> dict[str, Any]:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp.xlsx", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    workbook = Workbook(write_only=True, iso_dates=True)
    workbook.properties.title = "MinerU raw table export"
    workbook.properties.creator = "guhring-parser"
    workbook.properties.created = datetime(2000, 1, 1)
    raw_sheet_names: list[str] = []
    record_hash = hashlib.sha256()
    record_count = 0
    sheet_number = 1
    rows_on_sheet = 0
    worksheet = _raw_sheet(workbook, sheet_number)
    raw_sheet_names.append(worksheet.title)
    try:
        for record in iter_records(scan):
            if rows_on_sheet >= EXCEL_MAX_ROWS - 1:
                sheet_number += 1
                worksheet = _raw_sheet(workbook, sheet_number)
                raw_sheet_names.append(worksheet.title)
                rows_on_sheet = 0
            worksheet.append(
                [_write_only_value(worksheet, column, record.get(column)) for column in ALL_COLUMNS]
            )
            record_hash.update(_canonical_record_bytes(record))
            record_count += 1
            rows_on_sheet += 1

        manifest = workbook.create_sheet("_manifest")
        manifest.append(
            [_write_only_value(manifest, "key", "key"), _write_only_value(manifest, "value", "value")]
        )
        manifest_values = {
            "schema_version": SCHEMA_VERSION,
            "records": record_count,
            "record_sha256": record_hash.hexdigest(),
            "raw_sheets": json.dumps(raw_sheet_names, separators=(",", ":")),
            "artifacts": len(scan.artifacts),
            "documents": len(scan.documents),
            "scan_errors": scan.error_count,
        }
        for key, value in manifest_values.items():
            manifest.append(
                [_write_only_value(manifest, "key", key), _write_only_value(manifest, "value", value)]
            )
        workbook.save(temporary)
        validation_errors = _validate_created_workbook(
            temporary, record_count, record_hash.hexdigest(), raw_sheet_names
        )
        if validation_errors:
            raise RuntimeError("; ".join(validation_errors))
        os.replace(temporary, output)
        return {
            "output": str(output),
            "documents": len(scan.documents),
            "artifacts": len(scan.artifacts),
            "records": record_count,
            "record_sha256": record_hash.hexdigest(),
            "raw_sheets": raw_sheet_names,
            "scan_errors": scan.error_count,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        "--input",
        dest="input_root",
        type=Path,
        default=Path("/parsed"),
        help="Root searched recursively for MinerU JSON (default: /parsed).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/parsed/master_raw.xlsx"),
        help="Destination workbook (default: /parsed/master_raw.xlsx).",
    )
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="Return success after a best-effort export even when artifacts contain errors.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    scan = scan_outputs(arguments.input_root)
    try:
        summary = write_workbook(scan, arguments.output)
    except Exception as exc:
        print(f"export_raw: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for issue in scan.issues:
        print(json.dumps(issue.as_dict(), ensure_ascii=False), file=sys.stderr)
    if scan.error_count and not arguments.allow_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
