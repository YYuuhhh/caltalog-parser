#!/usr/bin/env python3
"""Strict verification of the real page-1128 VLM parse and page-1309 spans."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlparse

from openpyxl import load_workbook
from pypdf import PdfReader

from export_raw import ALL_COLUMNS, scan_outputs
from validate_parsing import validate_workbook


PARAMETERS = ("d1", "d2", "d3", "l1", "l2", "l3")
ARTICLE_ROWS = (
    ("20", "6", "24,0", "50,5", "2", "27,0", "4665 6.020"),
    ("20", "8", "24,0", "50,5", "2", "27,0", "4665 8.020"),
    ("20", "10", "24,0", "50,5", "2", "31,0", "4665 10.020"),
    ("20", "12", "24,0", "50,5", "2", "32,5", "4665 12.020"),
    ("20", "16", "24,0", "50,5", "2", "35,0", "4665 16.020"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} is not a JSON object")
    return value


def flatten_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from flatten_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_strings(child)


def exact_token(texts: Iterable[str], token: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
    return any(pattern.search(text) for text in texts)


def valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in value
        )
        and value[0] <= value[2]
        and value[1] <= value[3]
    )


def mounted_path(value: Any, parsed_root: Path) -> Path:
    path = Path(str(value))
    return parsed_root / path.relative_to("/parsed")


def latest_matching_report(
    logs_root: Path, source_sha: str
) -> tuple[Path, Mapping[str, Any]]:
    candidates = sorted((logs_root / "ragflow-e2e-runs").glob("*.json"), reverse=True)
    latest = logs_root / "ragflow-e2e-latest.json"
    if latest.is_file():
        candidates.append(latest)
    matches: list[tuple[str, Path, Mapping[str, Any]]] = []
    for path in candidates:
        try:
            report = read_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            report.get("status") == "success"
            and report.get("source_sha256") == source_sha
        ):
            matches.append((str(report.get("completed_at", "")), path, report))
    if not matches:
        raise FileNotFoundError(
            f"no successful RAGFlow run report found for {source_sha}"
        )
    _, path, report = max(matches, key=lambda item: item[0])
    return path, report


def selected_report(logs_root: Path, source_sha: str, path: Path | None):
    if path is None:
        return latest_matching_report(logs_root, source_sha)
    report = read_object(path)
    if report.get("status") != "success" or report.get("source_sha256") != source_sha:
        raise ValueError(
            "The selected RAGFlow report must be successful and match the tested PDF."
        )
    return path, report


def workbook_rows(path: Path) -> list[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    rows: list[dict[str, Any]] = []
    try:
        for name in workbook.sheetnames:
            if name != "raw" and not name.startswith("raw_"):
                continue
            iterator = workbook[name].iter_rows()
            header = [cell.value for cell in next(iterator)]
            if header != ALL_COLUMNS:
                raise ValueError(f"unexpected columns in worksheet {name}")
            for cells in iterator:
                if all(cell.value is None for cell in cells):
                    continue
                if any(cell.data_type == "f" for cell in cells):
                    raise ValueError(f"formula found in raw worksheet {name}")
                rows.append({column: cell.value for column, cell in zip(header, cells)})
    finally:
        workbook.close()
    return rows


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", type=Path, default=Path("/catalogs/test/guhring_vlm_test.pdf")
    )
    parser.add_argument(
        "--source-map",
        type=Path,
        default=Path("/catalogs/test/guhring_vlm_test.source.json"),
    )
    parser.add_argument("--parsed-root", type=Path, default=Path("/parsed"))
    parser.add_argument("--logs-root", type=Path, default=Path("/logs"))
    parser.add_argument(
        "--ragflow-log",
        type=Path,
        help="Verify this exact ingestion run; never fall back to historical reports.",
    )
    parser.add_argument(
        "--workbook", type=Path, default=Path("/parsed/master_raw.xlsx")
    )
    parser.add_argument(
        "--cpu-merged-report",
        type=Path,
        default=Path("/logs/guhring-test-verification.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/logs/guhring-vlm-accuracy-verification.json"),
    )
    args = parser.parse_args()

    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    source_sha = sha256_file(args.pdf)
    source_map = read_object(args.source_map)
    checks["fixture_is_real_pinned_page_1128"] = (
        len(PdfReader(str(args.pdf)).pages) == 1
        and source_map.get("source_pages") == [1128]
        and source_map.get("test_sha256") == source_sha
        and source_map.get("source_sha256")
        == "8f700e0d591cb37b4ca60cc5c72f542c4efbb1a901b746e16057a8c3c1957b02"
    )

    ragflow_path, ragflow = selected_report(
        args.logs_root, source_sha, args.ragflow_log
    )
    parser_config = ragflow.get("dataset_parser_config") or {}
    health = ragflow.get("mineru_health") or {}
    checks["ragflow_to_local_vlm_succeeded"] = (
        str((ragflow.get("document") or {}).get("run", "")).upper() == "DONE"
        and str(parser_config.get("layout_recognize", "")).casefold() == "mineru"
        and parser_config.get("task_page_size") == 1
        and urlparse(str(ragflow.get("mineru_url", ""))).hostname == "mineru"
        and urlparse(str(ragflow.get("mineru_url", ""))).port == 8000
        and health.get("status") == "healthy"
        and health.get("mineru_version") == "3.4.5"
        and str(health.get("device", "")).casefold() == "cuda"
    )

    artifacts = [
        item
        for item in (ragflow.get("mineru_cache_artifacts") or [])
        if isinstance(item, Mapping)
        and str(item.get("backend", "")).casefold() == "vlm-engine"
        and item.get("page") == 0
    ]
    checks["ragflow_selected_one_vlm_cache_page"] = len(artifacts) == 1
    if not artifacts:
        raise RuntimeError("RAGFlow report contains no page-0 VLM cache artifact")
    logged = artifacts[0]
    manifest_path = mounted_path(logged.get("manifest"), args.parsed_root)
    manifest = read_object(manifest_path)
    attempt = manifest_path.parent
    page_dir = attempt.parent.parent
    pointer = read_object(page_dir / "success.json")
    config = manifest.get("config") or {}
    request = config.get("request") or {}
    response_sha = sha256_file(attempt / "response.zip")
    raw_response = (
        mounted_path(manifest.get("raw_archive"), args.parsed_root) / "response.zip"
    )
    config_digest = hashlib.sha256(
        json.dumps(
            config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    cache_key = hashlib.sha256(
        f"guhring-cache-v1\0{source_sha}\0{manifest.get('page')}\0{config_digest}".encode()
    ).hexdigest()
    checks["cache_identity_and_immutable_raw_are_valid"] = (
        manifest.get("status") == "success"
        and manifest.get("source_sha256") == source_sha
        and manifest.get("page") == 0
        and config.get("mineru_version") == "3.4.5"
        and config.get("device_mode") == "cuda"
        and request.get("backend") == "vlm-engine"
        and manifest.get("config_digest") == config_digest
        and manifest.get("cache_key") == cache_key
        and pointer.get("cache_key") == cache_key
        and page_dir / str(pointer.get("attempt")) == attempt
        and response_sha
        == sha256_file(raw_response)
        == manifest.get("response_sha256")
        == pointer.get("response_sha256")
    )

    structured_paths = sorted((attempt / "extracted").rglob("*.json"))
    markdown_paths = sorted((attempt / "extracted").rglob("*.md"))
    texts: list[str] = []
    for path in structured_paths:
        texts.extend(flatten_strings(json.loads(path.read_text(encoding="utf-8"))))
    for path in markdown_paths:
        texts.extend(path.read_text(encoding="utf-8").splitlines())
    required_suffixes = (
        "_content_list.json",
        "_content_list_v2.json",
        "_middle.json",
        "_model.json",
    )
    checks["json_and_markdown_outputs_persisted"] = all(
        any(path.name.endswith(suffix) for path in structured_paths)
        for suffix in required_suffixes
    ) and bool(markdown_paths)
    parameter_evidence = {token: exact_token(texts, token) for token in PARAMETERS}
    checks["exact_d_and_l_parameter_names_preserved"] = all(parameter_evidence.values())
    token_evidence = {
        "Art.-Nr.": exact_token(texts, "Art.-Nr."),
        "Artikel-Nr.": exact_token(texts, "Artikel-Nr."),
        "Ø": exact_token(texts, "Ø"),
        "h6": exact_token(texts, "h6"),
        "leq_5_micrometres": any("\\leq 5\\mu \\mathrm{m}" in text for text in texts),
    }
    checks["symbols_and_tolerances_preserved"] = all(token_evidence.values())
    article_evidence = {
        row[-1]: any(row[-1] in text for text in texts) for row in ARTICLE_ROWS
    }
    checks["all_expected_article_rows_preserved"] = all(article_evidence.values())
    checks["decimal_commas_preserved"] = all(
        any(value in text for text in texts)
        for value in ("24,0", "50,5", "27,0", "32,5")
    )

    scan = scan_outputs(args.parsed_root)
    documents = [
        item
        for item in scan.documents
        if item.source_sha256 == source_sha
        and item.config_digest == config_digest
        and item.parser_backend == "vlm-engine"
    ]
    tables = [table for document in documents for table in document.tables]
    table_rows = {
        tuple(
            cell.text
            for cell in sorted(
                (cell for cell in table.cells if cell.row_index == row_index),
                key=lambda cell: cell.column_index,
            )
        )
        for table in tables
        for row_index in range(table.rows_detected)
    }
    checks["html_tables_rows_columns_pages_and_bboxes"] = (
        len(tables) >= 2
        and sum(table.rows_detected for table in tables) >= 9
        and max((table.logical_width for table in tables), default=0) >= 7
        and {table.page_idx for table in tables} == {0}
        and all(valid_bbox(table.bbox) for table in tables)
        and all(len(table.raw_html_sha256) == 64 for table in tables)
    )
    checks["table_row_cell_order_preserved"] = all(
        row in table_rows for row in ARTICLE_ROWS
    )

    workbook_errors = validate_workbook(args.workbook, scan)
    rows = [
        row
        for row in workbook_rows(args.workbook)
        if row.get("source_sha256") == source_sha and row.get("cache_key") == cache_key
    ]
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row.get("table_index"), row.get("row_index")), []).append(
            row
        )
    workbook_table_rows = {
        tuple(
            str(item.get("raw_value") or "")
            for item in sorted(group, key=lambda x: x["column_index"])
        )
        for group in grouped.values()
    }
    checks["master_raw_xlsx_round_trips_without_normalization"] = (
        not workbook_errors
        and len(rows) == 48
        and all(row.get("page") == 1 and row.get("page_idx") == 0 for row in rows)
        and all(row.get("bbox") for row in rows)
        and all(row.get("parser_backend") == "vlm-engine" for row in rows)
        and all(row in workbook_table_rows for row in ARTICLE_ROWS)
        and all(row.get("article_number") is None for row in rows)
        and all(row.get("product_family") is None for row in rows)
    )

    merged_report = read_object(args.cpu_merged_report)
    merged_checks = {
        item.get("name"): bool(item.get("passed"))
        for item in (merged_report.get("checks") or [])
        if isinstance(item, Mapping)
    }
    checks["merged_headers_verified_on_real_cpu_fixture"] = all(
        merged_checks.get(name, False)
        for name in (
            "ragflow_ingestion_succeeded",
            "ragflow_uses_local_mineru",
            "immutable_raw_archive_persisted",
            "html_tables_rows_columns_pages_and_bboxes",
            "merged_headers_preserved_as_html_spans",
        )
    )

    evidence.update(
        {
            "fixture_sha256": source_sha,
            "source_page": 1128,
            "ragflow_report": str(ragflow_path),
            "ragflow_run_id": ragflow.get("run_id"),
            "cache_key": cache_key,
            "config_digest": config_digest,
            "response_sha256": response_sha,
            "raw_archive": str(raw_response.parent),
            "structured_artifacts": [str(path) for path in structured_paths],
            "markdown_artifacts": [str(path) for path in markdown_paths],
            "parameters": parameter_evidence,
            "tokens": token_evidence,
            "article_rows": article_evidence,
            "tables_detected": len(tables),
            "rows_detected": sum(table.rows_detected for table in tables),
            "maximum_columns": max(
                (table.logical_width for table in tables), default=0
            ),
            "workbook_rows": len(rows),
            "workbook_errors": workbook_errors,
            "merged_header_report": str(args.cpu_merged_report),
            "merged_header_fixture_page": 1309,
            "merged_header_note": (
                "The CPU pipeline preserved real HTML rowspan/colspan on page 1309; "
                "the VLM page-1128 fixture verifies exact ambiguous l-label accuracy."
            ),
        }
    )
    failures = [name for name, passed in checks.items() if not passed]
    report = {
        "status": "success" if not failures else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "failures": failures,
        "evidence": evidence,
    }
    atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
