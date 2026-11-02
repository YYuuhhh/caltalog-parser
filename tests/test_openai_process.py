from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openpyxl import Workbook, load_workbook
from pypdf import PdfWriter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import openai_process  # noqa: E402


class MockResponsesHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(payload)
        content = payload["input"][0]["content"]
        text_item = next(item for item in content if item["type"] == "input_text")
        task = json.loads(text_item["text"])
        reviews = []
        for index, row in enumerate(task["rows"]):
            reviews.append(
                {
                    "row_id": row["row_id"],
                    "verdict": "ok" if index == 0 else "suspicious",
                    "confidence": 0.99 if index == 0 else 0.75,
                    "issues": [] if index == 0 else ["needs visual review"],
                    "processed_product_family": None,
                    "processed_article_number": row.get("article_number"),
                    "processed_parameter_name": row.get("raw_parameter_name"),
                    "processed_value": row.get("raw_value"),
                    "evidence": "Visible on the supplied source page.",
                }
            )
        result = {
            "document_quality": "warning",
            "summary": "One row needs review.",
            "table_findings": [],
            "rows": reviews,
        }
        response = {
            "id": f"resp-test-{len(type(self).requests)}",
            "object": "response",
            "model": "gpt-5.6-sol-test-snapshot",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("x-request-id", "req-test")
        self.end_headers()
        self.wfile.write(encoded)


class FirstResponseHasDuplicateRowId:
    def __init__(self, delegate: openai_process.ResponsesClient) -> None:
        self.delegate = delegate
        self.calls = 0

    def create(self, payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        response, metadata = self.delegate.create(payload)
        self.calls += 1
        if self.calls == 1:
            output_text = response["output"][0]["content"][0]["text"]
            structured = json.loads(output_text)
            structured["rows"][1]["row_id"] = structured["rows"][0]["row_id"]
            response["output"][0]["content"][0]["text"] = json.dumps(structured)
        return response, metadata


def write_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def write_workbook(
    path: Path, source_file: str, source_sha256: str, cache_key: str = "a" * 64
) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "raw"
    headers = [
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
        "bbox",
        "parser",
        "source_sha256",
        "cache_key",
    ]
    worksheet.append(headers)
    worksheet.append(
        [source_file, 1, 0, None, "00123.040", "d1", "Ø 12,50", 0, 1, 1, "[1,2,3,4]", "MinerU", source_sha256, cache_key]
    )
    formula_like = worksheet.cell(row=3, column=headers.index("raw_value") + 1, value="=source")
    formula_like.data_type = "s"
    values = [source_file, 1, 0, None, "00123.041", "Art.-Nr.", None, 0, 2, 1, None, "MinerU", source_sha256, cache_key]
    for column, value in enumerate(values, start=1):
        if column == headers.index("raw_value") + 1:
            continue
        worksheet.cell(row=3, column=column, value=value)
    workbook.save(path)


class OpenAIProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        MockResponsesHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockResponsesHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def config(self, root: Path, *, force: bool = False) -> openai_process.Config:
        return openai_process.Config(
            input_path=root / "parsed" / "master_raw.xlsx",
            output_root=root / "parsed" / "openai",
            logs_root=root / "logs",
            catalogs_root=root / "catalogs",
            api_base=f"http://127.0.0.1:{self.server.server_port}/v1",
            api_key="test-key",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            batch_rows=50,
            timeout_seconds=5,
            max_retries=0,
            validation_retries=2,
            max_output_tokens=32000,
            include_source_page=True,
            selection_manifest=None,
            allow_insecure_api_base=True,
            force=force,
            source_filters=(),
            source_sha_filters=(),
            source_pdf_overrides=(),
        )

    def fixture(self, root: Path) -> Path:
        source = root / "catalogs" / "test" / "Gühring.pdf"
        source.parent.mkdir(parents=True)
        write_pdf(source)
        workbook = root / "parsed" / "master_raw.xlsx"
        workbook.parent.mkdir(parents=True)
        write_workbook(
            workbook,
            "/catalogs/test/Gühring.pdf",
            openai_process._sha256_file(source),
        )
        return workbook

    def test_manifest_selection_excludes_unapproved_parser_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            source = root / "catalogs" / "test" / "Gühring.pdf"
            manifest = root / "catalogs" / "manifests" / "guhring-production.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "parse_selection": [
                            {
                                "source_sha256": openai_process._sha256_file(source),
                                "page_idx": 0,
                                "cache_key": "a" * 64,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = openai_process.process_workbook(
                replace(self.config(root), selection_manifest=manifest)
            )
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["rows"], 2)
            self.assertEqual(report["selected_parser_pages"], 1)
            self.assertEqual(report["api_calls"], 1)

    def test_end_to_end_archives_response_preserves_raw_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workbook_path = self.fixture(root)
            original_hash = openai_process._sha256_file(workbook_path)

            first = openai_process.process_workbook(self.config(root))
            self.assertEqual(first["status"], "success")
            self.assertEqual(first["api_calls"], 1)
            self.assertEqual(first["cache_hits"], 0)
            self.assertEqual(first["rows"], 2)
            self.assertEqual(first["batches_with_source_pdf"], 1)
            self.assertEqual(first["processed_validation"]["status"], "passed")
            self.assertEqual(first["processed_validation"]["rows"], 2)
            self.assertEqual(openai_process._sha256_file(workbook_path), original_hash)

            request = MockResponsesHandler.requests[0]
            self.assertFalse(request["store"])
            self.assertEqual(request["model"], "gpt-5.6-sol")
            self.assertEqual(request["text"]["format"]["type"], "json_schema")
            self.assertTrue(request["text"]["format"]["strict"])
            rows_schema = request["text"]["format"]["schema"]["properties"]["rows"]
            self.assertEqual(rows_schema["minItems"], 2)
            self.assertEqual(rows_schema["maxItems"], 2)
            self.assertEqual(
                set(rows_schema["items"]["properties"]["row_id"]["enum"]),
                {row["row_id"] for row in json.loads(next(
                    item["text"] for item in request["input"][0]["content"]
                    if item["type"] == "input_text"
                ))["rows"]},
            )
            content = request["input"][0]["content"]
            file_item = next(item for item in content if item["type"] == "input_file")
            self.assertGreater(len(file_item["file_data"]), 100)
            self.assertTrue(file_item["file_data"].startswith("data:application/pdf;base64,"))

            processed_path = root / "parsed" / "openai" / "master_processed.xlsx"
            self.assertTrue(processed_path.is_file())
            processed = load_workbook(processed_path, read_only=True, data_only=False)
            try:
                worksheet = processed["processed"]
                rows = list(worksheet.iter_rows())
                headers = [cell.value for cell in rows[0]]
                raw_index = headers.index("raw_value")
                verdict_index = headers.index("openai_verdict")
                model_index = headers.index("openai_model")
                self.assertEqual(rows[1][raw_index].value, "Ø 12,50")
                self.assertEqual(rows[2][raw_index].value, "=source")
                self.assertNotEqual(rows[2][raw_index].data_type, "f")
                self.assertEqual(rows[1][verdict_index].value, "ok")
                self.assertEqual(rows[2][verdict_index].value, "suspicious")
                self.assertEqual(rows[1][model_index].value, "gpt-5.6-sol-test-snapshot")
            finally:
                processed.close()

            second = openai_process.process_workbook(self.config(root))
            self.assertEqual(second["api_calls"], 0)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(len(MockResponsesHandler.requests), 1)
            cache_attempts = list((root / "parsed" / "openai" / "cache").rglob("response.json"))
            self.assertEqual(len(cache_attempts), 1)

            forced = openai_process.process_workbook(self.config(root, force=True))
            self.assertEqual(forced["api_calls"], 1)
            self.assertEqual(len(MockResponsesHandler.requests), 2)
            cache_attempts = list((root / "parsed" / "openai" / "cache").rglob("response.json"))
            self.assertEqual(len(cache_attempts), 2)

    def test_invalid_structured_response_is_archived_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            config = self.config(root)
            client = FirstResponseHasDuplicateRowId(
                openai_process.ResponsesClient(config)
            )

            report = openai_process.process_workbook(config, client=client)

            self.assertEqual(report["status"], "success")
            self.assertEqual(report["api_calls"], 2)
            self.assertEqual(report["validation_retries_for_new_api_calls"], 1)
            self.assertEqual(report["batch_reports"][0]["validation_retries"], 1)
            failures = list((root / "parsed" / "openai" / "cache").rglob("failure.json"))
            self.assertEqual(len(failures), 1)
            self.assertTrue((failures[0].parent / "response.json").is_file())
            self.assertEqual(len(MockResponsesHandler.requests), 2)

    def test_run_without_key_fails_before_writing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            config = replace(self.config(root), api_key=None)
            with self.assertRaises(openai_process.ConfigurationError):
                openai_process.process_workbook(config)
            self.assertFalse((root / "parsed" / "openai").exists())
            self.assertEqual(MockResponsesHandler.requests, [])

    def test_rejects_missing_or_unknown_row_ids(self) -> None:
        expected = ["row-a", "row-b"]
        result = {
            "document_quality": "warning",
            "summary": "bad",
            "table_findings": [],
            "rows": [
                {
                    "row_id": "row-a",
                    "verdict": "ok",
                    "confidence": 1.0,
                    "issues": [],
                    "processed_product_family": None,
                    "processed_article_number": None,
                    "processed_parameter_name": None,
                    "processed_value": None,
                    "evidence": "",
                },
                {
                    "row_id": "unknown",
                    "verdict": "ok",
                    "confidence": 1.0,
                    "issues": [],
                    "processed_product_family": None,
                    "processed_article_number": None,
                    "processed_parameter_name": None,
                    "processed_value": None,
                    "evidence": "",
                },
            ],
        }
        with self.assertRaises(openai_process.ResponseValidationError):
            openai_process.validate_structured_result(result, expected)


if __name__ == "__main__":
    unittest.main()
