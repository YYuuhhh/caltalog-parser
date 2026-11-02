#!/usr/bin/env python3
"""Validate persisted MinerU output and the generated master_raw.xlsx workbook."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import load_workbook

# Running ``python /app/scripts/validate_parsing.py`` places the scripts
# directory on sys.path.  The fallback supports import-by-path unit tests.
try:
    from export_raw import (
        ALL_COLUMNS,
        REQUIRED_COLUMNS,
        SCHEMA_VERSION,
        ParseIssue,
        ParsedTable,
        ScanResult,
        ScannedDocument,
        _canonical_record_bytes,
        iter_records,
        scan_outputs,
    )
except ImportError:  # pragma: no cover - package-style invocation
    from .export_raw import (  # type: ignore
        ALL_COLUMNS,
        REQUIRED_COLUMNS,
        SCHEMA_VERSION,
        ParseIssue,
        ParsedTable,
        ScanResult,
        ScannedDocument,
        _canonical_record_bytes,
        iter_records,
        scan_outputs,
    )

try:
    from pypdf import PdfReader
except ImportError:  # pypdf is installed in the project helper image.
    PdfReader = None  # type: ignore[assignment]


REPORT_SCHEMA_VERSION = 1
TEXT_COLUMNS = {
    "source_file",
    "product_family",
    "article_number",
    "raw_parameter_name",
    "raw_value",
    "bbox",
    "parser",
    "source_sha256",
    "parser_version",
    "parser_backend",
    "header_path",
    "cell_tag",
    "bbox_scope",
    "bbox_units",
    "cache_key",
    "artifact_path",
}

OCR_CONFUSABLE_BETWEEN_DIGITS = re.compile(r"(?<=\d)[OoIlSB](?=[.,]?\d)")
REPEATED_DECIMAL_MARK = re.compile(r"\d[.,]{2,}\d")
MIXED_DECIMAL_MARKS = re.compile(r"\d+[.,]\d+[.,]\d+")
DANGLING_DECIMAL_MARK = re.compile(r"\d[.,]\s*$")
CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def suspicious_numeric_reasons(value: str) -> list[str]:
    """Return conservative OCR/format anomaly rules for a numeric-looking cell.

    This is deliberately not a number parser.  Valid technical strings such as
    ``Ø 12,5``, ``12 h6``, ``+0,02/-0,01`` and article numbers stay untouched.
    """

    if not any(character.isdigit() for character in value):
        return []
    reasons: list[str] = []
    if "�" in value:
        reasons.append("unicode_replacement_character")
    if CONTROL_CHARACTER.search(value):
        reasons.append("control_character")
    if OCR_CONFUSABLE_BETWEEN_DIGITS.search(value):
        reasons.append("ocr_confusable_between_digits")
    if REPEATED_DECIMAL_MARK.search(value):
        reasons.append("repeated_decimal_separator")
    if MIXED_DECIMAL_MARKS.search(value):
        # Do not flag conventional thousands grouping, such as 1.234.567.
        compact = value.strip()
        conventional_grouping = bool(
            re.fullmatch(r"[+-]?\d{1,3}([.,]\d{3}){2,}(?:[.,]\d+)?", compact)
        )
        if not conventional_grouping:
            reasons.append("mixed_or_multiple_decimal_separators")
    if DANGLING_DECIMAL_MARK.search(value):
        reasons.append("dangling_decimal_separator")
    if value.count("(") != value.count(")") or value.count("[") != value.count("]"):
        reasons.append("unbalanced_brackets")
    return reasons


def _data_row_indexes(table: ParsedTable) -> list[int]:
    return [
        row_index
        for row_index in range(table.rows_detected)
        if row_index not in table.header_rows
    ]


def duplicate_rows(table: ParsedTable) -> int:
    signatures: list[tuple[str | None, ...]] = []
    for row_index in _data_row_indexes(table):
        signature = tuple(
            table.grid[(row_index, column_index)].text
            if (row_index, column_index) in table.grid
            else None
            for column_index in range(table.logical_width)
        )
        if any(value not in {None, ""} for value in signature):
            signatures.append(signature)
    return sum(count - 1 for count in Counter(signatures).values() if count > 1)


def _suspicious_cells(table: ParsedTable) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for cell in table.cells:
        if cell.row_index in table.header_rows:
            continue
        reasons = suspicious_numeric_reasons(cell.text)
        if reasons:
            details.append(
                {
                    "page": table.page_idx + 1 if table.page_idx is not None else None,
                    "page_idx": table.page_idx,
                    "table_index": table.table_index,
                    "row_index": cell.row_index,
                    "column_index": cell.column_index,
                    "value": cell.text,
                    "reasons": reasons,
                }
            )
    return details


def _candidate_source_paths(source_file: str, catalogs_root: Path) -> list[Path]:
    source = Path(source_file)
    candidates: list[Path] = []
    if source.is_absolute():
        candidates.append(source)
    else:
        candidates.append(catalogs_root / source)
        candidates.append(catalogs_root / source.name)
    existing = [candidate.resolve() for candidate in candidates if candidate.is_file()]
    if existing:
        return list(dict.fromkeys(existing))

    # Fallback for manifests containing only a basename or MinerU document stem.
    target_names = {source.name.casefold()}
    if not source.suffix:
        target_names.add(f"{source.name}.pdf".casefold())
    try:
        matches = [
            path.resolve()
            for path in catalogs_root.rglob("*")
            if path.is_file() and path.name.casefold() in target_names
        ]
    except OSError:
        return []
    return list(dict.fromkeys(matches))


def _pdf_page_count(source_file: str, catalogs_root: Path) -> tuple[int | None, str | None]:
    candidates = _candidate_source_paths(source_file, catalogs_root)
    if len(candidates) > 1:
        return None, f"Multiple source PDFs match {source_file!r}; page count is ambiguous."
    if not candidates:
        return None, None
    source = candidates[0]
    if source.suffix.casefold() != ".pdf":
        return None, None
    if PdfReader is None:
        return None, "pypdf is unavailable; source PDF page count was not checked."
    try:
        return len(PdfReader(str(source)).pages), None
    except Exception as exc:
        return None, f"Could not read source PDF {source}: {exc}"


def _error_payload(issue: ParseIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "page": issue.page_idx + 1 if issue.page_idx is not None else None,
        "page_idx": issue.page_idx,
        "artifact": issue.path,
        "message": issue.message,
    }


def _document_report(document: ScannedDocument, catalogs_root: Path) -> dict[str, Any]:
    page_count, source_warning = _pdf_page_count(document.source_file, catalogs_root)
    errors = [_error_payload(issue) for issue in document.issues if issue.severity == "error"]
    warnings = [_error_payload(issue) for issue in document.issues if issue.severity != "error"]

    if source_warning:
        warnings.append(
            {
                "code": "source_page_count_unavailable",
                "page": None,
                "page_idx": None,
                "artifact": document.source_file,
                "message": source_warning,
            }
        )
    if page_count is None:
        page_count = document.pages_total
    if page_count is None and document.observed_pages:
        # This is explicitly reported as observed output pages, not inferred
        # from a maximum index (which would invent intervening blank pages).
        page_count = len(document.observed_pages)
        warnings.append(
            {
                "code": "pages_are_observed_output_pages",
                "page": None,
                "page_idx": None,
                "artifact": document.source_file,
                "message": "No source/manifest page count was available; pages is the number of distinct output pages.",
            }
        )
    if (
        document.pages_total is not None
        and page_count is not None
        and document.pages_total != page_count
    ):
        errors.append(
            {
                "code": "page_count_mismatch",
                "page": None,
                "page_idx": None,
                "artifact": document.source_file,
                "message": (
                    f"Manifest reports {document.pages_total} pages but the source reports {page_count}."
                ),
            }
        )

    tables_detected = len(document.tables)
    rows_detected = sum(table.rows_detected for table in document.tables)
    empty_cells = sum(cell.text == "" for table in document.tables for cell in table.cells)
    duplicate_count = sum(duplicate_rows(table) for table in document.tables)
    suspicious_details = [
        detail for table in document.tables for detail in _suspicious_cells(table)
    ]
    page_time = sum(
        value for key, value in document.processing_times.items() if key.startswith("page:")
    )
    aggregate_times = [
        value for key, value in document.processing_times.items() if not key.startswith("page:")
    ]
    processing_candidates = ([page_time] if page_time else []) + aggregate_times
    processing_time = round(max(processing_candidates), 6) if processing_candidates else None
    return {
        "file": document.source_file,
        "pages": page_count,
        "tables_detected": tables_detected,
        "rows_detected": rows_detected,
        "empty_cells": empty_cells,
        "duplicate_rows": duplicate_count,
        "suspicious_numeric_cells": len(suspicious_details),
        "parser_errors": errors,
        "processing_time": processing_time,
        "pages_observed": sorted(document.observed_pages),
        "source_sha256": document.source_sha256,
        "parser": document.parser_label,
        "merged_headers_detected": sum(table.has_merged_cells for table in document.tables),
        "suspicious_numeric_details": suspicious_details,
        "warnings": warnings,
    }


def _manifest_values(workbook: Any) -> dict[str, Any]:
    if "_manifest" not in workbook.sheetnames:
        return {}
    values: dict[str, Any] = {}
    rows = workbook["_manifest"].iter_rows(values_only=True)
    next(rows, None)
    for row in rows:
        if row and row[0] is not None:
            values[str(row[0])] = row[1] if len(row) > 1 else None
    return values


def validate_workbook(workbook_path: Path, scan: ScanResult) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not workbook_path.is_file():
        return [
            {
                "code": "workbook_missing",
                "message": f"Workbook does not exist: {workbook_path}",
                "artifact": str(workbook_path),
            }
        ]

    expected_count = 0
    expected_hasher = hashlib.sha256()
    for record in iter_records(scan):
        expected_hasher.update(_canonical_record_bytes(record))
        expected_count += 1

    try:
        workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    except Exception as exc:
        return [
            {
                "code": "workbook_invalid",
                "message": f"Could not open workbook: {exc}",
                "artifact": str(workbook_path),
            }
        ]

    try:
        manifest_present = "_manifest" in workbook.sheetnames
        if not manifest_present:
            errors.append(
                {
                    "code": "workbook_manifest_missing",
                    "message": "Workbook is missing the required _manifest worksheet.",
                    "artifact": str(workbook_path),
                }
            )
            manifest: dict[str, Any] = {}
        else:
            manifest_sheet = workbook["_manifest"]
            manifest_rows = manifest_sheet.iter_rows(values_only=True)
            manifest_header = list(next(manifest_rows, ()))
            if manifest_header != ["key", "value"]:
                errors.append(
                    {
                        "code": "workbook_manifest_columns_invalid",
                        "message": "_manifest must have exactly the columns ['key', 'value'].",
                        "artifact": str(workbook_path),
                    }
                )
            manifest = _manifest_values(workbook)
            required_manifest_keys = {
                "schema_version",
                "records",
                "record_sha256",
                "raw_sheets",
                "artifacts",
                "documents",
                "scan_errors",
            }
            missing_keys = sorted(required_manifest_keys.difference(manifest))
            if missing_keys:
                errors.append(
                    {
                        "code": "workbook_manifest_keys_missing",
                        "message": f"_manifest is missing required keys: {missing_keys}.",
                        "artifact": str(workbook_path),
                    }
                )
            try:
                manifest_schema = int(manifest.get("schema_version"))
            except (TypeError, ValueError):
                manifest_schema = -1
            if manifest_schema != SCHEMA_VERSION:
                errors.append(
                    {
                        "code": "workbook_manifest_schema_invalid",
                        "message": (
                            f"_manifest schema_version is {manifest.get('schema_version')!r}; "
                            f"expected {SCHEMA_VERSION}."
                        ),
                        "artifact": str(workbook_path),
                    }
                )

        discovered_raw_sheets = [
            name for name in workbook.sheetnames if name == "raw" or name.startswith("raw_")
        ]
        raw_sheets: list[str]
        try:
            raw_sheets = json.loads(str(manifest.get("raw_sheets")))
            if not isinstance(raw_sheets, list) or not all(
                isinstance(name, str) for name in raw_sheets
            ):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            if manifest_present:
                errors.append(
                    {
                        "code": "workbook_manifest_raw_sheets_invalid",
                        "message": "_manifest raw_sheets is not a JSON array of worksheet names.",
                        "artifact": str(workbook_path),
                    }
                )
            raw_sheets = discovered_raw_sheets
        else:
            if raw_sheets != discovered_raw_sheets:
                errors.append(
                    {
                        "code": "workbook_manifest_raw_sheets_mismatch",
                        "message": "_manifest raw_sheets does not match the workbook data sheets.",
                        "artifact": str(workbook_path),
                    }
                )
        if not raw_sheets:
            errors.append(
                {
                    "code": "workbook_raw_sheet_missing",
                    "message": "Workbook has no raw data worksheet.",
                    "artifact": str(workbook_path),
                }
            )

        actual_count = 0
        actual_hasher = hashlib.sha256()
        for sheet_name in raw_sheets:
            if sheet_name not in workbook.sheetnames:
                errors.append(
                    {
                        "code": "workbook_sheet_missing",
                        "message": f"Manifest references missing sheet {sheet_name!r}.",
                        "artifact": str(workbook_path),
                    }
                )
                continue
            worksheet = workbook[sheet_name]
            rows = worksheet.iter_rows()
            try:
                header = [cell.value for cell in next(rows)]
            except StopIteration:
                header = []
            if header[: len(REQUIRED_COLUMNS)] != REQUIRED_COLUMNS or header != ALL_COLUMNS:
                errors.append(
                    {
                        "code": "workbook_columns_invalid",
                        "message": f"Sheet {sheet_name!r} does not have the expected column schema.",
                        "artifact": str(workbook_path),
                    }
                )
                continue
            for cells in rows:
                if all(cell.value is None for cell in cells):
                    continue
                record: dict[str, Any] = {}
                for column, cell in zip(ALL_COLUMNS, cells):
                    if cell.data_type == "f":
                        errors.append(
                            {
                                "code": "workbook_formula_in_raw_data",
                                "message": f"Formula found at {sheet_name}!{cell.coordinate}.",
                                "artifact": str(workbook_path),
                            }
                        )
                    if column in TEXT_COLUMNS and cell.value is not None and cell.data_type not in {
                        "s",
                        "inlineStr",
                    }:
                        errors.append(
                            {
                                "code": "workbook_text_coerced",
                                "message": (
                                    f"Text field {column!r} at {sheet_name}!{cell.coordinate} "
                                    f"has XLSX type {cell.data_type!r}."
                                ),
                                "artifact": str(workbook_path),
                            }
                        )
                    value = cell.value
                    if column == "raw_value" and value is None:
                        value = ""
                    record[column] = value
                actual_hasher.update(_canonical_record_bytes(record))
                actual_count += 1

        if actual_count != expected_count:
            errors.append(
                {
                    "code": "workbook_record_count_mismatch",
                    "message": f"Workbook has {actual_count} records; parsed JSON yields {expected_count}.",
                    "artifact": str(workbook_path),
                }
            )
        if actual_hasher.hexdigest() != expected_hasher.hexdigest():
            errors.append(
                {
                    "code": "workbook_content_hash_mismatch",
                    "message": "Workbook values do not round-trip to the parsed JSON record hash.",
                    "artifact": str(workbook_path),
                }
            )
        if manifest_present:
            if str(manifest.get("record_sha256")) != actual_hasher.hexdigest():
                errors.append(
                    {
                        "code": "workbook_manifest_hash_mismatch",
                        "message": "_manifest record_sha256 does not match workbook rows.",
                        "artifact": str(workbook_path),
                    }
                )
            try:
                manifest_records = int(manifest.get("records"))
            except (TypeError, ValueError):
                manifest_records = -1
            if manifest_records != actual_count:
                errors.append(
                    {
                        "code": "workbook_manifest_count_mismatch",
                        "message": "_manifest record count does not match workbook rows.",
                        "artifact": str(workbook_path),
                    }
                )
            manifest_counts = {
                "artifacts": len(scan.artifacts),
                "documents": len(scan.documents),
                "scan_errors": scan.error_count,
            }
            for key, expected in manifest_counts.items():
                try:
                    actual = int(manifest.get(key))
                except (TypeError, ValueError):
                    actual = -1
                if actual != expected:
                    errors.append(
                        {
                            "code": f"workbook_manifest_{key}_mismatch",
                            "message": (
                                f"_manifest {key} is {manifest.get(key)!r}; expected {expected}."
                            ),
                            "artifact": str(workbook_path),
                        }
                    )
    finally:
        workbook.close()
    return errors


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _csv_content(documents: Iterable[Mapping[str, Any]]) -> str:
    import io

    output = io.StringIO(newline="")
    columns = [
        "file",
        "pages",
        "tables_detected",
        "rows_detected",
        "empty_cells",
        "duplicate_rows",
        "suspicious_numeric_cells",
        "parser_errors",
        "processing_time",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for document in documents:
        row = dict(document)
        row["parser_errors"] = json.dumps(
            document.get("parser_errors", []), ensure_ascii=False, separators=(",", ":")
        )
        writer.writerow(row)
    return output.getvalue()


def write_reports(report: Mapping[str, Any], logs_root: Path) -> dict[str, str]:
    logs_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    json_content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    csv_content = _csv_content(report.get("documents", []))
    timestamped_json = logs_root / f"validation-{stamp}.json"
    timestamped_csv = logs_root / f"validation-{stamp}.csv"
    latest_json = logs_root / "validation-latest.json"
    latest_csv = logs_root / "validation-latest.csv"
    for path, content in (
        (timestamped_json, json_content),
        (timestamped_csv, csv_content),
        (latest_json, json_content),
        (latest_csv, csv_content),
    ):
        _atomic_write_text(path, content)
    return {
        "json": str(timestamped_json),
        "csv": str(timestamped_csv),
        "latest_json": str(latest_json),
        "latest_csv": str(latest_csv),
    }


def build_report(
    parsed_root: Path, workbook_path: Path, catalogs_root: Path
) -> tuple[dict[str, Any], bool]:
    scan = scan_outputs(parsed_root)
    documents = [_document_report(document, catalogs_root) for document in scan.documents]
    workbook_errors = validate_workbook(workbook_path, scan)
    global_errors = [
        _error_payload(issue)
        for issue in scan.issues
        if issue.document is None and issue.severity == "error"
    ]
    global_errors.extend(workbook_errors)

    # Workbook integrity applies to every document in the workbook.  Include
    # the failures in each per-document parser_errors field as requested, while
    # retaining a global list for empty/undiscoverable inputs.
    if workbook_errors:
        for document in documents:
            document["parser_errors"].extend(workbook_errors)

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parsed_root": str(parsed_root),
        "workbook": str(workbook_path),
        "documents": documents,
        "global_errors": global_errors,
        "summary": {
            "documents": len(documents),
            "tables_detected": sum(item["tables_detected"] for item in documents),
            "rows_detected": sum(item["rows_detected"] for item in documents),
            "parser_errors": (
                sum(len(item["parser_errors"]) for item in documents)
                if documents
                else len(global_errors)
            ),
            "suspicious_numeric_cells": sum(
                item["suspicious_numeric_cells"] for item in documents
            ),
        },
    }
    failed = bool(global_errors) or any(item["parser_errors"] for item in documents)
    return report, failed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parsed-root",
        "--input-root",
        "--input",
        dest="parsed_root",
        type=Path,
        default=Path("/parsed"),
        help="Root searched recursively for MinerU output (default: /parsed).",
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("/parsed/master_raw.xlsx"),
        help="Workbook produced by export_raw.py (default: /parsed/master_raw.xlsx).",
    )
    parser.add_argument(
        "--logs",
        "--logs-root",
        dest="logs_root",
        type=Path,
        default=Path("/logs"),
        help="Validation report directory (default: /logs).",
    )
    parser.add_argument(
        "--catalogs-root",
        type=Path,
        default=Path("/catalogs"),
        help="Mounted source catalog root used to verify PDF page counts.",
    )
    parser.add_argument(
        "--fail-on-suspicious",
        action="store_true",
        help="Return failure when suspicious numeric cells are found.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    report, failed = build_report(
        arguments.parsed_root, arguments.workbook, arguments.catalogs_root
    )
    paths = write_reports(report, arguments.logs_root)
    output = {"summary": report["summary"], "reports": paths}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if arguments.fail_on_suspicious and report["summary"]["suspicious_numeric_cells"]:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
