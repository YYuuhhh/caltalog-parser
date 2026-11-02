#!/usr/bin/env python3
"""Verify the persisted, real Gühring RAGFlow -> MinerU test run.

The test fixture is an extracted catalog page.  Its container-local page is
one, while ``guhring_catalog_test.source.json`` maps it back to physical page
1309 of the downloaded catalog.  This verifier deliberately checks source
tokens and values without translating names or coercing decimal commas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from openpyxl import load_workbook
from pypdf import PdfReader

try:
    from export_raw import ALL_COLUMNS, _canonical_record_bytes, scan_outputs
except ImportError:  # pragma: no cover - supports package-style imports in tests
    from .export_raw import (  # type: ignore
        ALL_COLUMNS,
        _canonical_record_bytes,
        scan_outputs,
    )


REPORT_SCHEMA_VERSION = 1
EXPECTED_SOURCE_PAGE = 1309
EXPECTED_PARAMETERS = ("d1", "d2", "d3", "l1", "l2", "l3")
EXPECTED_ARTICLE_NUMBERS = (
    "4174 10.016",
    "4098 20.016",
    "4098 20.025",
    "4098 25.020",
    "4098 25.025",
    "4098 32.032",
    "4098 32.040",
)
EXPECTED_DECIMAL_VALUES = ("49,5", "74,0", "0,12")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_COMMA = re.compile(r"(?<!\d)\d+,[0-9]+(?!\d)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".{uuid.uuid4().hex}.tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalise_space(value: Any) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def flatten_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from flatten_strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for child in value:
            yield from flatten_strings(child)


def exact_token(texts: Iterable[str], token: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
    return any(pattern.search(normalise_space(text)) for text in texts)


def exact_phrase(texts: Iterable[str], phrase: str) -> bool:
    expected = normalise_space(phrase)
    return any(expected in normalise_space(text) for text in texts)


def resolve_mounted_path(raw: Any, mount: str, local_root: Path) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    mount_path = Path(mount)
    if path.is_absolute():
        try:
            return local_root / path.relative_to(mount_path)
        except ValueError:
            return path
    return local_root / path


def valid_bbox(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in value):
        return False
    return value[0] <= value[2] and value[1] <= value[3]


def json_bbox(raw: Any) -> list[int | float] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return list(raw) if valid_bbox(raw) else None


def bool_text(value: Any) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


class CheckCollector:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, evidence: Any = None, detail: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "passed": bool(passed)}
        if evidence is not None:
            item["evidence"] = evidence
        if detail:
            item["detail"] = detail
        self.checks.append(item)

    @property
    def failures(self) -> list[str]:
        return [item["name"] for item in self.checks if not item["passed"]]


def _fixture_checks(
    checks: CheckCollector,
    pdf: Path,
    source_map_path: Path,
    expected_source_page: int,
) -> tuple[str | None, Mapping[str, Any] | None, int | None]:
    if not pdf.is_file() or not source_map_path.is_file():
        checks.add(
            "fixture_files_exist",
            False,
            {"pdf": str(pdf), "source_map": str(source_map_path)},
        )
        return None, None, None
    checks.add("fixture_files_exist", True, {"pdf": str(pdf), "source_map": str(source_map_path)})

    try:
        source_map = read_json(source_map_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checks.add("fixture_source_map_valid", False, detail=str(exc))
        return None, None, None
    if not isinstance(source_map, Mapping):
        checks.add("fixture_source_map_valid", False, detail="source map is not a JSON object")
        return None, None, None

    fixture_sha = sha256_file(pdf)
    mapped_sha = source_map.get("test_sha256")
    source_pages = source_map.get("source_pages")
    mapped_page = source_pages[0] if isinstance(source_pages, list) and len(source_pages) == 1 else None
    try:
        pdf_pages = len(PdfReader(str(pdf)).pages)
    except Exception as exc:
        checks.add("fixture_pdf_readable", False, detail=str(exc))
        pdf_pages = None
    else:
        checks.add("fixture_pdf_readable", pdf_pages == 1, {"pages": pdf_pages})

    mapping_ok = (
        mapped_sha == fixture_sha
        and source_map.get("test_pages") == 1
        and mapped_page == expected_source_page
        and Path(str(source_map.get("test_file", ""))).name == pdf.name
    )
    checks.add(
        "fixture_hash_and_source_page_mapping",
        mapping_ok,
        {
            "sha256": fixture_sha,
            "mapped_sha256": mapped_sha,
            "local_page": 1,
            "local_page_idx": 0,
            "original_source_page": mapped_page,
            "expected_original_source_page": expected_source_page,
        },
    )
    return fixture_sha, source_map, pdf_pages


def _ragflow_checks(
    checks: CheckCollector,
    ragflow_log: Path,
    fixture_sha: str | None,
    expected_version: str,
    expected_device: str,
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    if not ragflow_log.is_file():
        checks.add("ragflow_e2e_log_exists", False, {"path": str(ragflow_log)})
        return None, []
    checks.add("ragflow_e2e_log_exists", True, {"path": str(ragflow_log)})
    try:
        payload = read_json(ragflow_log)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checks.add("ragflow_e2e_log_valid", False, detail=str(exc))
        return None, []
    if not isinstance(payload, Mapping):
        checks.add("ragflow_e2e_log_valid", False, detail="log is not a JSON object")
        return None, []

    parser_config = payload.get("dataset_parser_config")
    parser_config = parser_config if isinstance(parser_config, Mapping) else {}
    health = payload.get("mineru_health")
    health = health if isinstance(health, Mapping) else {}
    document = payload.get("document")
    document = document if isinstance(document, Mapping) else {}
    progress = document.get("progress")
    try:
        progress_value = float(progress)
    except (TypeError, ValueError):
        progress_value = -1.0
    run_state = str(document.get("run", "")).upper()
    run_complete = progress_value >= 1 or run_state in {"3", "DONE"}
    success = payload.get("status") == "success" and payload.get("source_sha256") == fixture_sha
    checks.add(
        "ragflow_ingestion_succeeded",
        success and run_complete,
        {
            "status": payload.get("status"),
            "source_sha256": payload.get("source_sha256"),
            "document_id": payload.get("document_id"),
            "run": document.get("run"),
            "progress": progress,
        },
    )

    mineru_url = str(payload.get("mineru_url", ""))
    parsed_mineru_url = urlparse(mineru_url)
    mineru_first = (
        str(parser_config.get("layout_recognize", "")).casefold() == "mineru"
        and parser_config.get("task_page_size") == 1
        and parsed_mineru_url.scheme == "http"
        and parsed_mineru_url.hostname == "mineru"
        and parsed_mineru_url.port == 8000
        and payload.get("mineru_openapi_head") == 200
    )
    checks.add(
        "ragflow_uses_local_mineru",
        mineru_first,
        {
            "mineru_url": mineru_url,
            "layout_recognize": parser_config.get("layout_recognize"),
            "task_page_size": parser_config.get("task_page_size"),
            "openapi_head": payload.get("mineru_openapi_head"),
        },
    )

    health_ok = (
        health.get("status") == "healthy"
        and health.get("mineru_version") == expected_version
        and str(health.get("device", "")).casefold() == expected_device.casefold()
        and isinstance(health.get("model_revision"), str)
        and health.get("model_revision") not in {"", "unknown"}
        and bool(HEX_SHA256.fullmatch(str(health.get("runtime_fingerprint", ""))))
    )
    checks.add(
        "ragflow_observed_expected_mineru_runtime",
        health_ok,
        {
            "status": health.get("status"),
            "mineru_version": health.get("mineru_version"),
            "device": health.get("device"),
            "model_revision": health.get("model_revision"),
            "runtime_fingerprint": health.get("runtime_fingerprint"),
        },
    )

    listed = payload.get("mineru_cache_artifacts")
    artifacts = [item for item in listed if isinstance(item, Mapping)] if isinstance(listed, list) else []
    checks.add(
        "ragflow_logged_mineru_cache_artifact",
        bool(artifacts),
        {"count": len(artifacts)},
    )
    return payload, artifacts


def _cache_checks(
    checks: CheckCollector,
    parsed_root: Path,
    fixture_sha: str | None,
    logged_artifacts: list[Mapping[str, Any]],
    expected_version: str,
    expected_device: str,
    expected_backend: str,
) -> tuple[list[Path], set[str], list[Mapping[str, Any]]]:
    attempts: list[Path] = []
    valid_cache_keys: set[str] = set()
    manifests: list[Mapping[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    for logged in logged_artifacts:
        manifest_path = resolve_mounted_path(logged.get("manifest"), "/parsed", parsed_root)
        if manifest_path is None or not manifest_path.is_file():
            evidence.append({"manifest": str(manifest_path), "valid": False, "reason": "missing"})
            continue
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            evidence.append({"manifest": str(manifest_path), "valid": False, "reason": str(exc)})
            continue
        if not isinstance(manifest, Mapping):
            evidence.append({"manifest": str(manifest_path), "valid": False, "reason": "not an object"})
            continue

        attempt = manifest_path.parent
        page_dir = attempt.parent.parent
        pointer_path = page_dir / "success.json"
        try:
            pointer = read_json(pointer_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            evidence.append({"manifest": str(manifest_path), "valid": False, "reason": f"pointer: {exc}"})
            continue
        if not isinstance(pointer, Mapping):
            evidence.append({"manifest": str(manifest_path), "valid": False, "reason": "pointer not an object"})
            continue

        config = manifest.get("config")
        config = config if isinstance(config, Mapping) else {}
        request = config.get("request")
        request = request if isinstance(request, Mapping) else {}
        config_digest = str(manifest.get("config_digest", ""))
        calculated_config_digest = hashlib.sha256(canonical_json(config)).hexdigest()
        page = manifest.get("page")
        source_sha = str(manifest.get("source_sha256", ""))
        cache_key = str(manifest.get("cache_key", ""))
        expected_key = hashlib.sha256(
            f"guhring-cache-v1\0{source_sha}\0{page}\0{config_digest}".encode("utf-8")
        ).hexdigest()

        response = attempt / "response.zip"
        response_sha = sha256_file(response) if response.is_file() else None
        raw_archive = resolve_mounted_path(manifest.get("raw_archive"), "/parsed", parsed_root)
        raw_response = raw_archive / "response.zip" if raw_archive is not None else None
        raw_sha = sha256_file(raw_response) if raw_response is not None and raw_response.is_file() else None
        runtime_fingerprint = str(config.get("runtime_fingerprint", ""))
        backend = str(request.get("backend", "")).casefold()

        validation = {
            "manifest_status": manifest.get("status") == "success",
            "pointer_status": pointer.get("status") == "success",
            "source_sha256": fixture_sha is not None and source_sha == fixture_sha,
            "page_zero": page == 0,
            "cache_schema": config.get("cache_schema") == "v1",
            "mineru_version": config.get("mineru_version") == expected_version,
            "device": str(config.get("device_mode", "")).casefold()
            == expected_device.casefold(),
            "model_revision": isinstance(config.get("model_revision"), str)
            and config.get("model_revision") not in {"", "unknown"},
            "runtime_fingerprint": bool(HEX_SHA256.fullmatch(runtime_fingerprint)),
            "config_digest": config_digest == calculated_config_digest,
            "cache_key": cache_key == expected_key,
            "pointer_cache_key": pointer.get("cache_key") == cache_key,
            "response_sha256": pointer.get("response_sha256")
            == manifest.get("response_sha256")
            == response_sha,
            "raw_archive_sha256": raw_sha == response_sha and response_sha is not None,
            "expected_backend": backend == expected_backend.casefold(),
            "table_enabled": bool_text(request.get("table_enable")),
        }
        valid = all(validation.values())
        evidence.append(
            {
                "manifest": str(manifest_path),
                "valid": valid,
                "page": page,
                "status": manifest.get("status"),
                "cache_key": cache_key,
                "config_digest": config_digest,
                "calculated_config_digest": calculated_config_digest,
                "mineru_version": config.get("mineru_version"),
                "backend": request.get("backend"),
                "device": config.get("device_mode"),
                "model_revision": config.get("model_revision"),
                "runtime_fingerprint": runtime_fingerprint,
                "response_sha256": response_sha,
                "raw_archive": str(raw_archive) if raw_archive else None,
                "validation": validation,
            }
        )
        if valid:
            attempts.append(attempt)
            manifests.append(manifest)
            valid_cache_keys.add(cache_key)

    checks.add("cache_manifest_and_fingerprint_valid", bool(attempts), evidence)
    checks.add(
        "immutable_raw_archive_persisted",
        bool(attempts) and all(item.get("valid") and item.get("raw_archive") for item in evidence),
        {"valid_archives": len(attempts), "parsed_root": str(parsed_root)},
    )
    return attempts, valid_cache_keys, manifests


def _load_structured_artifacts(attempts: Iterable[Path]) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    strings: list[str] = []
    supported = ("_content_list.json", "_content_list_v2.json", "_middle.json", "_model.json")
    for attempt in attempts:
        extracted = attempt / "extracted"
        for path in sorted(extracted.rglob("*.json")):
            if not path.name.endswith(supported):
                continue
            try:
                payload = read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            paths.append(path)
            strings.extend(flatten_strings(payload))
    return paths, strings


def _row_texts_from_tables(tables: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for table in tables:
        for row_index in range(table.rows_detected):
            row_cells = sorted(
                (cell for cell in table.cells if cell.row_index == row_index),
                key=lambda cell: cell.column_index,
            )
            values.append(" ".join(cell.text for cell in row_cells))
    return values


def _structured_checks(
    checks: CheckCollector,
    parsed_root: Path,
    fixture_sha: str | None,
    attempts: list[Path],
) -> tuple[list[Any], list[str]]:
    artifact_paths, structured_strings = _load_structured_artifacts(attempts)
    checks.add(
        "structured_mineru_json_persisted",
        bool(artifact_paths),
        {"artifacts": [str(path) for path in artifact_paths]},
    )

    scan = scan_outputs(parsed_root)
    documents = [document for document in scan.documents if document.source_sha256 == fixture_sha]
    tables = [table for document in documents for table in document.tables]
    relevant_attempts = [path.resolve() for path in attempts]
    if relevant_attempts:
        exact_tables = []
        for table in tables:
            try:
                artifact = Path(table.artifact_path).resolve()
            except OSError:
                continue
            if any(attempt == artifact or attempt in artifact.parents for attempt in relevant_attempts):
                exact_tables.append(table)
        if exact_tables:
            tables = exact_tables

    row_texts = _row_texts_from_tables(tables)
    cell_texts = [cell.text for table in tables for cell in table.cells]
    evidence_texts = structured_strings + row_texts + cell_texts
    table_metrics = {
        "tables": len(tables),
        "rows": sum(table.rows_detected for table in tables),
        "max_columns": max((table.logical_width for table in tables), default=0),
        "pages": sorted({table.page_idx + 1 for table in tables if table.page_idx is not None}),
        "bboxes": [table.bbox for table in tables],
    }
    table_shape_ok = (
        len(tables) >= 2
        and table_metrics["rows"] >= 9
        and table_metrics["max_columns"] >= 8
        and table_metrics["pages"] == [1]
        and all(valid_bbox(table.bbox) for table in tables)
    )
    checks.add("html_tables_rows_columns_pages_and_bboxes", table_shape_ok, table_metrics)

    found_parameters = {
        token: exact_token(cell_texts + row_texts, token) for token in EXPECTED_PARAMETERS
    }
    checks.add("exact_dimension_names_preserved", all(found_parameters.values()), found_parameters)

    artifact_token_evidence = {
        "Art.-Nr.": exact_phrase(evidence_texts, "Art.-Nr."),
        "Ø": exact_phrase(evidence_texts, "Ø"),
        "d1 h6": exact_phrase(evidence_texts, "d1 h6"),
    }
    checks.add(
        "source_symbols_and_h6_tolerance_preserved",
        all(artifact_token_evidence.values()),
        artifact_token_evidence,
    )

    article_evidence = {
        article: exact_phrase(evidence_texts, article) for article in EXPECTED_ARTICLE_NUMBERS
    }
    checks.add("exact_article_numbers_preserved", all(article_evidence.values()), article_evidence)

    found_decimals = sorted(
        {match.group(0) for text in evidence_texts for match in DECIMAL_COMMA.finditer(text)}
    )
    decimal_evidence = {
        value: any(value == candidate for candidate in found_decimals)
        for value in EXPECTED_DECIMAL_VALUES
    }
    checks.add(
        "decimal_commas_preserved",
        all(decimal_evidence.values()),
        {"required": decimal_evidence, "observed_sample": found_decimals[:20]},
    )

    merged = [
        {
            "page": table.page_idx + 1 if table.page_idx is not None else None,
            "table_index": table.table_index,
            "row_index": cell.row_index,
            "column_index": cell.column_index,
            "text": cell.text,
            "rowspan": cell.rowspan,
            "colspan": cell.colspan,
        }
        for table in tables
        for cell in table.cells
        if cell.row_index in table.header_rows and (cell.rowspan > 1 or cell.colspan > 1)
    ]
    checks.add("merged_headers_preserved_as_html_spans", bool(merged), merged[:20])
    return tables, evidence_texts


def _workbook_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        raw_sheets = [name for name in workbook.sheetnames if name == "raw" or name.startswith("raw_")]
        for sheet_name in raw_sheets:
            worksheet = workbook[sheet_name]
            iterator = worksheet.iter_rows()
            try:
                header = [cell.value for cell in next(iterator)]
            except StopIteration:
                errors.append(f"{sheet_name} is empty")
                continue
            if header != ALL_COLUMNS:
                errors.append(f"{sheet_name} header does not match exporter schema")
                continue
            for cells in iterator:
                if all(cell.value is None for cell in cells):
                    continue
                record: dict[str, Any] = {}
                for column, cell in zip(ALL_COLUMNS, cells):
                    if cell.data_type == "f":
                        errors.append(f"formula found at {sheet_name}!{cell.coordinate}")
                    value = cell.value
                    if column == "raw_value" and value is None:
                        value = ""
                    record[column] = value
                rows.append(record)
        if "_manifest" not in workbook.sheetnames:
            errors.append("_manifest sheet is missing")
        else:
            for key, value, *_ in workbook["_manifest"].iter_rows(min_row=2, values_only=True):
                if key is not None:
                    manifest[str(key)] = value
    finally:
        workbook.close()
    return rows, manifest, errors


def _workbook_checks(
    checks: CheckCollector,
    workbook_path: Path,
    fixture_sha: str | None,
    expected_version: str,
    expected_backend: str,
    valid_cache_keys: set[str],
) -> list[Mapping[str, Any]]:
    if not workbook_path.is_file():
        checks.add("master_raw_workbook_exists", False, {"path": str(workbook_path)})
        return []
    checks.add("master_raw_workbook_exists", True, {"path": str(workbook_path)})
    try:
        rows, manifest, errors = _workbook_rows(workbook_path)
    except Exception as exc:
        checks.add("master_raw_workbook_round_trip_valid", False, detail=str(exc))
        return []

    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(_canonical_record_bytes(row))
    round_trip_ok = (
        not errors
        and str(manifest.get("records")) == str(len(rows))
        and manifest.get("record_sha256") == hasher.hexdigest()
    )
    checks.add(
        "master_raw_workbook_round_trip_valid",
        round_trip_ok,
        {
            "records": len(rows),
            "manifest_records": manifest.get("records"),
            "record_sha256": hasher.hexdigest(),
            "manifest_record_sha256": manifest.get("record_sha256"),
            "errors": errors,
        },
    )

    all_fixture_rows = [row for row in rows if row.get("source_sha256") == fixture_sha]
    fixture_rows = [
        row
        for row in all_fixture_rows
        if str(row.get("cache_key") or "") in valid_cache_keys
    ]
    checks.add(
        "workbook_contains_fixture_rows",
        bool(fixture_rows),
        {
            "fixture_rows": len(fixture_rows),
            "all_source_hash_rows": len(all_fixture_rows),
            "source_sha256": fixture_sha,
            "valid_cache_keys": sorted(valid_cache_keys),
        },
    )
    if not fixture_rows:
        return []

    bboxes = [json_bbox(row.get("bbox")) for row in fixture_rows]
    workbook_cache_keys = {
        str(row.get("cache_key")) for row in fixture_rows if row.get("cache_key") not in {None, ""}
    }
    provenance_ok = (
        {row.get("page") for row in fixture_rows} == {1}
        and {row.get("page_idx") for row in fixture_rows} == {0}
        and all(bbox is not None for bbox in bboxes)
        and all(str(row.get("parser_version", "")) == expected_version for row in fixture_rows)
        and all(
            str(row.get("parser_backend", "")).casefold()
            == expected_backend.casefold()
            for row in fixture_rows
        )
        and bool(valid_cache_keys)
        # Aggregate/derived MinerU manifests do not claim a page cache key.
        # When a key is present, however, it must resolve to the validated
        # immutable cache attempt, and at least one exported row must carry it.
        and bool(workbook_cache_keys)
        and workbook_cache_keys.issubset(valid_cache_keys)
    )
    checks.add(
        "workbook_page_bbox_and_parser_provenance",
        provenance_ok,
        {
            "pages": sorted({row.get("page") for row in fixture_rows}),
            "page_indexes": sorted({row.get("page_idx") for row in fixture_rows}),
            "bbox_count": sum(bbox is not None for bbox in bboxes),
            "parser_versions": sorted({str(row.get("parser_version")) for row in fixture_rows}),
            "parser_backends": sorted({str(row.get("parser_backend")) for row in fixture_rows}),
            "cache_keys": sorted(workbook_cache_keys),
        },
    )

    grouped: dict[tuple[Any, Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for row in fixture_rows:
        grouped[(row.get("page"), row.get("table_index"), row.get("row_index"))].append(row)
    workbook_texts = [
        str(value)
        for row in fixture_rows
        for value in (
            row.get("article_number"),
            row.get("raw_parameter_name"),
            row.get("raw_value"),
            row.get("header_path"),
        )
        if value is not None
    ]
    workbook_texts.extend(
        " ".join(
            str(item.get("raw_value", ""))
            for item in sorted(group, key=lambda item: int(item.get("column_index") or 0))
        )
        for group in grouped.values()
    )
    parameters = {token: exact_token(workbook_texts, token) for token in EXPECTED_PARAMETERS}
    articles = {article: exact_phrase(workbook_texts, article) for article in EXPECTED_ARTICLE_NUMBERS}
    decimal_values = {
        value: exact_phrase(workbook_texts, value) for value in EXPECTED_DECIMAL_VALUES
    }
    workbook_content_ok = (
        all(parameters.values())
        and all(articles.values())
        and all(decimal_values.values())
        and exact_phrase(workbook_texts, "Ø")
    )
    checks.add(
        "workbook_preserves_exact_guhring_table_values",
        workbook_content_ok,
        {
            "parameters": parameters,
            "article_numbers": articles,
            "decimal_values": decimal_values,
            "diameter_symbol": exact_phrase(workbook_texts, "Ø"),
        },
    )

    hierarchical_headers = []
    for row in fixture_rows:
        raw = row.get("header_path")
        if not isinstance(raw, str):
            continue
        try:
            path = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(path, list) and len(path) > 1:
            hierarchical_headers.append(path)
    checks.add(
        "workbook_retains_merged_header_paths",
        bool(hierarchical_headers),
        hierarchical_headers[:20],
    )
    return fixture_rows


def verify(
    *,
    parsed_root: Path,
    logs_root: Path,
    pdf: Path,
    source_map: Path,
    workbook: Path,
    ragflow_log: Path,
    report_path: Path,
    expected_source_page: int = EXPECTED_SOURCE_PAGE,
    expected_version: str = "3.4.5",
    expected_device: str = "cpu",
    expected_backend: str = "pipeline",
) -> dict[str, Any]:
    if expected_backend.casefold() in {"vlm-transformers", "vlm-engine"}:
        expected_backend = "vlm-engine"
    checks = CheckCollector()
    fixture_sha, mapping, pdf_pages = _fixture_checks(
        checks, pdf, source_map, expected_source_page
    )
    _, logged_artifacts = _ragflow_checks(
        checks, ragflow_log, fixture_sha, expected_version, expected_device
    )
    attempts, valid_cache_keys, cache_manifests = _cache_checks(
        checks,
        parsed_root,
        fixture_sha,
        logged_artifacts,
        expected_version,
        expected_device,
        expected_backend,
    )
    tables, _ = _structured_checks(checks, parsed_root, fixture_sha, attempts)
    fixture_rows = _workbook_checks(
        checks,
        workbook,
        fixture_sha,
        expected_version,
        expected_backend,
        valid_cache_keys,
    )

    failures = checks.failures
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "success" if not failures else "failed",
        "generated_at": utc_now(),
        "fixture": {
            "file": str(pdf),
            "sha256": fixture_sha,
            "local_pages": pdf_pages,
            "local_page": 1,
            "local_page_idx": 0,
            "original_source_page": (
                mapping.get("source_pages", [None])[0]
                if isinstance(mapping, Mapping)
                and isinstance(mapping.get("source_pages"), list)
                and mapping.get("source_pages")
                else None
            ),
        },
        "parser": {
            "name": "MinerU",
            "expected_version": expected_version,
            "expected_device": expected_device,
            "expected_backend": expected_backend,
            "valid_cache_manifests": len(cache_manifests),
            "cache_keys": sorted(valid_cache_keys),
        },
        "metrics": {
            "tables_detected": len(tables),
            "rows_detected": sum(table.rows_detected for table in tables),
            "maximum_columns": max((table.logical_width for table in tables), default=0),
            "workbook_fixture_rows": len(fixture_rows),
        },
        "checks": checks.checks,
        "failures": failures,
        "report": str(report_path),
        "logs_root": str(logs_root),
    }
    atomic_json(report_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parsed-root", type=Path, default=Path("/parsed"))
    parser.add_argument("--logs-root", type=Path, default=Path("/logs"))
    parser.add_argument(
        "--pdf", type=Path, default=Path("/catalogs/test/guhring_catalog_test.pdf")
    )
    parser.add_argument(
        "--source-map",
        type=Path,
        default=Path("/catalogs/test/guhring_catalog_test.source.json"),
    )
    parser.add_argument("--workbook", type=Path, default=Path("/parsed/master_raw.xlsx"))
    parser.add_argument(
        "--ragflow-log", type=Path, default=Path("/logs/ragflow-e2e-latest.json")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("/logs/guhring-test-verification.json")
    )
    parser.add_argument("--expected-source-page", type=int, default=EXPECTED_SOURCE_PAGE)
    parser.add_argument(
        "--expected-mineru-version",
        default=os.environ.get("MINERU_VERSION", "3.4.5"),
    )
    parser.add_argument(
        "--expected-device", default=os.environ.get("MINERU_DEVICE_MODE", "cpu")
    )
    parser.add_argument(
        "--expected-backend", default=os.environ.get("MINERU_BACKEND", "pipeline")
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Persist all quality failures but do not make them fail the surrounding workflow.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify(
            parsed_root=args.parsed_root,
            logs_root=args.logs_root,
            pdf=args.pdf,
            source_map=args.source_map,
            workbook=args.workbook,
            ragflow_log=args.ragflow_log,
            report_path=args.report,
            expected_source_page=args.expected_source_page,
            expected_version=args.expected_mineru_version,
            expected_device=args.expected_device,
            expected_backend=args.expected_backend,
        )
    except Exception as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "failed",
            "generated_at": utc_now(),
            "failures": ["verifier_internal_error"],
            "error": f"{type(exc).__name__}: {exc}",
            "report": str(args.report),
        }
        atomic_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "success" or args.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
