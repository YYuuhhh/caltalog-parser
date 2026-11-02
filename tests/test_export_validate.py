from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import export_raw  # noqa: E402
import validate_parsing  # noqa: E402


MERGED_TABLE = """
<html><body><table>
  <thead>
    <tr><th rowspan="2">Art.-Nr.</th><th colspan="3">Abmessungen</th><th rowspan="2">Hinweis</th></tr>
    <tr><th>d1</th><th>d2</th><th>l1</th></tr>
  </thead>
  <tbody>
    <tr><td>00123.040</td><td>Ø 12,50</td><td>11,8 ±0,02</td><td>85</td><td>=source text</td></tr>
    <tr><td>00123.041</td><td>1O.5</td><td></td><td>85</td><td>roh</td></tr>
    <tr><td>00123.041</td><td>1O.5</td><td></td><td>85</td><td>roh</td></tr>
  </tbody>
</table></body></html>
""".strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class MinerUExportTests(unittest.TestCase):
    def test_legacy_export_preserves_symbols_headers_and_text_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parse_dir = root / "result"
            write_json(
                parse_dir / "manifest.json",
                {
                    "status": "success",
                    "source_file": "/catalogs/test/Gühring.pdf",
                    "source_sha256": "a" * 64,
                    "mineru_version": "3.0.test",
                    "backend": "pipeline",
                    "pages": 1,
                    "processing_time": 2.5,
                },
            )
            write_json(
                parse_dir / "files" / "Gühring_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [10, 20, 900, 800],
                        "table_body": MERGED_TABLE,
                    }
                ],
            )

            scan = export_raw.scan_outputs(root)
            self.assertEqual(scan.error_count, 0)
            self.assertEqual(len(scan.documents), 1)
            self.assertEqual(scan.documents[0].source_file, "Gühring.pdf")
            records = list(export_raw.iter_records(scan))
            self.assertEqual(len(records), 15)
            by_value = {record["raw_value"]: record for record in records}
            self.assertEqual(by_value["Ø 12,50"]["raw_parameter_name"], "d1")
            self.assertEqual(by_value["Ø 12,50"]["article_number"], "00123.040")
            self.assertEqual(
                json.loads(by_value["Ø 12,50"]["header_path"]), ["Abmessungen", "d1"]
            )
            self.assertEqual(by_value["11,8 ±0,02"]["raw_parameter_name"], "d2")
            self.assertEqual(by_value["=source text"]["raw_parameter_name"], "Hinweis")

            workbook_path = root / "master_raw.xlsx"
            summary = export_raw.write_workbook(scan, workbook_path)
            self.assertEqual(summary["records"], 15)
            workbook = load_workbook(workbook_path, read_only=True, data_only=False)
            try:
                worksheet = workbook["raw"]
                header = [cell.value for cell in next(worksheet.iter_rows())]
                value_column = header.index("raw_value") + 1
                formula_like = [
                    row[value_column - 1]
                    for row in worksheet.iter_rows(min_row=2)
                    if row[value_column - 1].value == "=source text"
                ]
                self.assertEqual(len(formula_like), 1)
                self.assertNotEqual(formula_like[0].data_type, "f")
            finally:
                workbook.close()

    def test_v2_page_override_and_middle_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v2_dir = root / "v2"
            write_json(
                v2_dir / "manifest.json",
                {
                    "status": "success",
                    "source_file": "v2.pdf",
                    "page": 4,
                    "mineru_version": "3.0",
                },
            )
            write_json(
                v2_dir / "v2_content_list_v2.json",
                [
                    [
                        {
                            "type": "table",
                            "bbox": [0, 0, 1000, 1000],
                            "content": {
                                "html": "<table><tr><td>Art.-Nr.</td><td>d3</td></tr><tr><td>A-01</td><td>4,2</td></tr></table>"
                            },
                        }
                    ]
                ],
            )
            middle_dir = root / "middle"
            write_json(
                middle_dir / "manifest.json",
                {"status": "success", "source_file": "middle.pdf"},
            )
            write_json(
                middle_dir / "middle_middle.json",
                {
                    "_backend": "pipeline",
                    "_version_name": "2.9",
                    "pdf_info": [
                        {
                            "page_idx": 1,
                            "para_blocks": [
                                {
                                    "type": "table",
                                    "bbox": [1, 2, 3, 4],
                                    "blocks": [
                                        {
                                            "type": "table_body",
                                            "lines": [
                                                {
                                                    "spans": [
                                                        {
                                                            "type": "table",
                                                            "html": "<table><tr><td>l2</td></tr><tr><td>77</td></tr></table>",
                                                        }
                                                    ]
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )

            scan = export_raw.scan_outputs(root)
            self.assertEqual(scan.error_count, 0)
            records = list(export_raw.iter_records(scan))
            v2_record = next(
                record
                for record in records
                if record["source_file"] == "v2.pdf" and record["raw_value"] == "4,2"
            )
            self.assertEqual(v2_record["page_idx"], 4)
            self.assertEqual(v2_record["page"], 5)
            self.assertEqual(v2_record["raw_parameter_name"], "d3")
            middle_record = next(
                record for record in records if record["source_file"] == "middle.pdf"
            )
            self.assertEqual(middle_record["page"], 2)
            self.assertEqual(middle_record["parser"], "MinerU 2.9 (pipeline)")
            self.assertEqual(middle_record["bbox_units"], "page_coordinates")

    def test_api_envelope_decodes_content_list_string(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            embedded = [
                {
                    "type": "table",
                    "page_idx": 0,
                    "table_body": "<table><tr><td>d1</td></tr><tr><td>2,5</td></tr></table>",
                }
            ]
            write_json(
                root / "api_content_list.json",
                {
                    "backend": "pipeline",
                    "version": "3.1",
                    "results": {"api.pdf": {"content_list": json.dumps(embedded)}},
                },
            )
            scan = export_raw.scan_outputs(root)
            records = list(export_raw.iter_records(scan))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_file"], "api.pdf")
            self.assertEqual(records[0]["parser"], "MinerU 3.1 (pipeline)")

    def test_success_pointer_excludes_old_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page_dir = root / "cache" / "v1" / "hash" / "config" / "pages" / "000000"
            write_json(
                page_dir / "success.json",
                {"status": "success", "attempt": "attempts/new", "cache_key": "key"},
            )
            for attempt, value in (("old", "OLD"), ("new", "NEW")):
                attempt_dir = page_dir / "attempts" / attempt
                write_json(
                    attempt_dir / "manifest.json",
                    {
                        "status": "success",
                        "source_file": "cache.pdf",
                        "source_sha256": "b" * 64,
                        "page": 0,
                    },
                )
                write_json(
                    attempt_dir / "extracted" / "cache_content_list.json",
                    [
                        {
                            "type": "table",
                            "page_idx": 0,
                            "table_body": f"<table><tr><td>d1</td></tr><tr><td>{value}</td></tr></table>",
                        }
                    ],
                )
            scan = export_raw.scan_outputs(root)
            values = [record["raw_value"] for record in export_raw.iter_records(scan)]
            self.assertEqual(values, ["NEW"])

    def test_derived_etl_success_pointer_is_outside_mineru_scan_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page_dir = root / "cache" / "v1" / "hash" / "config" / "pages" / "000000"
            write_json(
                page_dir / "success.json",
                {"status": "success", "attempt": "attempts/current", "cache_key": "key"},
            )
            attempt = page_dir / "attempts/current"
            write_json(
                attempt / "manifest.json",
                {"status": "success", "source_file": "mineru.pdf", "source_sha256": "b" * 64},
            )
            write_json(
                attempt / "extracted/mineru_content_list.json",
                [{"type": "table", "page_idx": 0, "table_body": "<table><tr><td>d1</td></tr><tr><td>7,5</td></tr></table>"}],
            )
            for directory in ("etl", "full-test-runs/run/etl", "full-test-investigation/reconstruction-draft", "custom-derived-directory"):
                write_json(
                    root / directory / "cache/v1/etl-key/success.json",
                    {"status": "success", "attempt_relpath": "cache/v1/etl-key/attempts/run"},
                )

            scan = export_raw.scan_outputs(root)
            self.assertEqual(scan.error_count, 0)
            self.assertEqual(
                [record["raw_value"] for record in export_raw.iter_records(scan)],
                ["7,5"],
            )

    def test_current_cache_excludes_derived_and_raw_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_sha = "c" * 64
            config_sha = "d" * 64
            cache_key = "e" * 64
            table = "<table><tr><td>Art.-Nr.</td><td>d1</td></tr><tr><td>007</td><td>Ø 5,5</td></tr></table>"

            page_dir = (
                root / "cache" / "v1" / source_sha / config_sha / "pages" / "000004"
            )
            attempt = page_dir / "attempts" / "new"
            write_json(
                page_dir / "success.json",
                {"status": "success", "attempt": "attempts/new", "cache_key": cache_key},
            )
            write_json(
                attempt / "manifest.json",
                {
                    "status": "success",
                    "source_file": "/catalogs/test/catalog.pdf",
                    "source_sha256": source_sha,
                    "page": 4,
                    "cache_key": cache_key,
                    "processing_time": 2.0,
                    "config": {"mineru_version": "3.4.5", "request": {"backend": "pipeline"}},
                },
            )
            write_json(
                attempt / "extracted" / "catalog_content_list.json",
                [{"type": "table", "page_idx": 0, "table_body": table}],
            )

            derived = root / "documents" / "catalog" / "request"
            write_json(
                derived / "files" / "catalog" / "auto" / "cache_manifest.json",
                {
                    "source_file": "catalog.pdf",
                    "source_sha256": source_sha,
                    "start_page": 4,
                    "end_page": 4,
                    "pages": 1,
                    "processing_time": 9.0,
                    "mineru_version": "3.4.5",
                    "config": {"request": {"backend": "pipeline"}},
                },
            )
            write_json(
                derived / "files" / "catalog" / "auto" / "catalog_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "source_page_idx": 4,
                        "table_body": table.replace("Ø 5,5", "DERIVED COPY"),
                    }
                ],
            )
            raw_attempt = (
                root
                / "raw"
                / source_sha
                / config_sha
                / "page-000005"
                / "historical"
            )
            write_json(
                raw_attempt / "manifest.json",
                {
                    "status": "success",
                    "source_file": "catalog.pdf",
                    "source_sha256": source_sha,
                    "page": 4,
                    "cache_key": "historical-key",
                    "config": {
                        "mineru_version": "3.4.5",
                        "request": {"backend": "pipeline"},
                    },
                },
            )
            write_json(
                raw_attempt / "extracted" / "catalog_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": table.replace("Ø 5,5", "HISTORICAL RAW"),
                    }
                ],
            )
            write_json(
                root / "_api_tasks" / "stale" / "catalog_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": table.replace("Ø 5,5", "TRANSIENT COPY"),
                    }
                ],
            )

            scan = export_raw.scan_outputs(root)
            self.assertEqual(len(scan.documents), 1)
            self.assertEqual(len(scan.documents[0].tables), 1)
            record = next(
                item
                for item in export_raw.iter_records(scan)
                if item["raw_parameter_name"] == "d1"
            )
            self.assertEqual(record["page"], 5)
            self.assertEqual(record["source_file"], "catalog.pdf")
            self.assertEqual(record["source_sha256"], source_sha)
            self.assertEqual(record["parser"], "MinerU 3.4.5 (pipeline)")
            exported_values = {
                item["raw_value"] for item in export_raw.iter_records(scan)
            }
            self.assertTrue(
                {"DERIVED COPY", "HISTORICAL RAW", "TRANSIENT COPY"}.isdisjoint(
                    exported_values
                )
            )

            document_report = validate_parsing._document_report(
                scan.documents[0], root / "catalogs"
            )
            self.assertEqual(document_report["processing_time"], 2.0)

    def test_document_identity_prevents_hash_and_backend_provenance_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def add_artifact(
                directory: str,
                source_sha: str,
                config_digest: str,
                backend: str,
                value: str,
            ) -> None:
                parse_dir = root / directory
                write_json(
                    parse_dir / "manifest.json",
                    {
                        "status": "success",
                        "source_file": "/catalogs/nested/same.pdf",
                        "source_sha256": source_sha,
                        "config_digest": config_digest,
                        "mineru_version": "3.4.5",
                        "backend": backend,
                    },
                )
                write_json(
                    parse_dir / "same_content_list.json",
                    [
                        {
                            "type": "table",
                            "page_idx": 0,
                            "table_body": (
                                "<table><tr><td>d1</td></tr>"
                                f"<tr><td>{value}</td></tr></table>"
                            ),
                        }
                    ],
                )

            add_artifact("a", "a" * 64, "1" * 64, "pipeline", "A")
            add_artifact("b", "b" * 64, "1" * 64, "pipeline", "B")
            add_artifact("c", "a" * 64, "2" * 64, "vlm-transformers", "C")

            scan = export_raw.scan_outputs(root)
            self.assertEqual(len(scan.documents), 3)
            by_value = {
                record["raw_value"]: record
                for record in export_raw.iter_records(scan)
            }
            self.assertEqual(by_value["A"]["source_sha256"], "a" * 64)
            self.assertEqual(by_value["A"]["parser_backend"], "pipeline")
            self.assertEqual(by_value["B"]["source_sha256"], "b" * 64)
            self.assertEqual(by_value["B"]["parser_backend"], "pipeline")
            self.assertEqual(by_value["C"]["source_sha256"], "a" * 64)
            self.assertEqual(by_value["C"]["parser_backend"], "vlm-transformers")

    def test_table_indexes_follow_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parse_dir = root / "ordered"
            write_json(
                parse_dir / "manifest.json",
                {"status": "success", "source_file": "ordered.pdf"},
            )
            write_json(
                parse_dir / "ordered_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": "<table><tr><td>d1</td></tr><tr><td>FIRST</td></tr></table>",
                    },
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": "<table><tr><td>d1</td></tr><tr><td>SECOND</td></tr></table>",
                    },
                ],
            )

            records = list(export_raw.iter_records(export_raw.scan_outputs(root)))
            order = [
                (record["table_index"], record["raw_value"])
                for record in records
            ]
            self.assertEqual(order, [(0, "FIRST"), (1, "SECOND")])

    def test_unheaded_table_keeps_first_row_without_inventing_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "plain_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": (
                            "<table><tr><td>A</td><td>B</td></tr>"
                            "<tr><td>C</td><td>D</td></tr></table>"
                        ),
                    }
                ],
            )

            records = list(export_raw.iter_records(export_raw.scan_outputs(root)))
            self.assertEqual([record["raw_value"] for record in records], ["A", "B", "C", "D"])
            self.assertTrue(all(record["raw_parameter_name"] is None for record in records))
            self.assertTrue(all(record["header_path"] is None for record in records))

    def test_blank_article_number_round_trips_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "blank_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": (
                            "<table><tr><td>Art.-Nr.</td><td>d1</td></tr>"
                            "<tr><td></td><td>5,0</td></tr></table>"
                        ),
                    }
                ],
            )
            scan = export_raw.scan_outputs(root)
            records = list(export_raw.iter_records(scan))
            self.assertTrue(all(record["article_number"] is None for record in records))
            workbook_path = root / "master_raw.xlsx"
            export_raw.write_workbook(scan, workbook_path)
            self.assertEqual(validate_parsing.validate_workbook(workbook_path, scan), [])

    def test_overlapping_parser_spans_are_reported_without_dropping_raw_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "overlap_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": (
                            "<table><tr><th>d1</th><th rowspan='2'>l1</th></tr>"
                            "<tr><td colspan='2'>source overlap</td></tr></table>"
                        ),
                    }
                ],
            )

            scan = export_raw.scan_outputs(root)
            self.assertEqual(scan.error_count, 0)
            overlap = [issue for issue in scan.issues if issue.code == "overlapping_cell_span"]
            self.assertEqual(len(overlap), 1)
            self.assertEqual(overlap[0].severity, "warning")
            self.assertIn(
                "source overlap",
                {record["raw_value"] for record in export_raw.iter_records(scan)},
            )

    def test_validation_metrics_and_workbook_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parse_dir = root / "parsed" / "run"
            write_json(
                parse_dir / "manifest.json",
                {
                    "status": "success",
                    "source_file": "metrics.pdf",
                    "pages": 1,
                    "processing_time": 3.25,
                },
            )
            write_json(
                parse_dir / "metrics_content_list.json",
                [{"type": "table", "page_idx": 0, "table_body": MERGED_TABLE}],
            )
            scan = export_raw.scan_outputs(root / "parsed")
            workbook = root / "parsed" / "master_raw.xlsx"
            export_raw.write_workbook(scan, workbook)

            report, failed = validate_parsing.build_report(
                root / "parsed", workbook, root / "catalogs"
            )
            self.assertFalse(failed)
            document = report["documents"][0]
            self.assertEqual(document["pages"], 1)
            self.assertEqual(document["tables_detected"], 1)
            self.assertEqual(document["rows_detected"], 5)
            self.assertEqual(document["empty_cells"], 2)
            self.assertEqual(document["duplicate_rows"], 1)
            self.assertEqual(document["suspicious_numeric_cells"], 2)
            self.assertEqual(document["processing_time"], 3.25)
            self.assertGreaterEqual(document["merged_headers_detected"], 1)
            self.assertEqual(document["parser_errors"], [])

    def test_workbook_manifest_is_required_and_schema_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parse_dir = root / "parsed" / "run"
            write_json(
                parse_dir / "manifest.json",
                {"status": "success", "source_file": "manifest.pdf"},
            )
            write_json(
                parse_dir / "manifest_content_list.json",
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "table_body": "<table><tr><td>d1</td></tr><tr><td>7</td></tr></table>",
                    }
                ],
            )
            scan = export_raw.scan_outputs(root / "parsed")

            invalid_schema = root / "invalid-schema.xlsx"
            export_raw.write_workbook(scan, invalid_schema)
            workbook = load_workbook(invalid_schema)
            workbook["_manifest"]["B2"] = export_raw.SCHEMA_VERSION + 1
            workbook.save(invalid_schema)
            workbook.close()
            schema_codes = {
                issue["code"]
                for issue in validate_parsing.validate_workbook(invalid_schema, scan)
            }
            self.assertIn("workbook_manifest_schema_invalid", schema_codes)

            missing_manifest = root / "missing-manifest.xlsx"
            export_raw.write_workbook(scan, missing_manifest)
            workbook = load_workbook(missing_manifest)
            workbook.remove(workbook["_manifest"])
            workbook.save(missing_manifest)
            workbook.close()
            missing_codes = {
                issue["code"]
                for issue in validate_parsing.validate_workbook(missing_manifest, scan)
            }
            self.assertIn("workbook_manifest_missing", missing_codes)


class NumericValidationTests(unittest.TestCase):
    def test_valid_technical_values_are_not_flagged(self) -> None:
        for value in ("Ø 12,50", "11,8 ±0,02", "+0,02/-0,01", "12 h6", "00123.040"):
            self.assertEqual(validate_parsing.suspicious_numeric_reasons(value), [], value)

    def test_obvious_ocr_confusable_is_flagged(self) -> None:
        self.assertIn(
            "ocr_confusable_between_digits",
            validate_parsing.suspicious_numeric_reasons("1O5"),
        )


if __name__ == "__main__":
    unittest.main()
