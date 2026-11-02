from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import export_raw  # noqa: E402
import verify_guhring_test  # noqa: E402


TABLE_ONE = """
<table><thead>
<tr><th rowspan="2">Art.-Nr.</th><th colspan="6">Abmessungen</th><th rowspan="2">Ø-Bereich</th></tr>
<tr><th>d1</th><th>d2</th><th>d3</th><th>l1</th><th>l2</th><th>l3</th></tr>
</thead><tbody>
<tr><td>4174 10.016</td><td>10</td><td>30</td><td>16</td><td>35</td><td>20</td><td>30</td><td>4-10</td></tr>
</tbody></table>
""".strip()

TABLE_TWO = """
<table><thead>
<tr><th rowspan="2">Art.-Nr.</th><th colspan="6">Abmessungen</th><th rowspan="2">Ø-Bereich</th></tr>
<tr><th>d1</th><th>d2</th><th>d3</th><th>l1</th><th>l2</th><th>l3</th></tr>
</thead><tbody>
<tr><td>4098 20.016</td><td>10</td><td>34</td><td>16</td><td>74,0</td><td>49,5</td><td>40</td><td>1-13</td></tr>
<tr><td>4098 20.025</td><td>10</td><td>34</td><td>25</td><td>74,0</td><td>49,5</td><td>60</td><td>1-13</td></tr>
<tr><td>4098 25.020</td><td>12</td><td>42</td><td>20</td><td>84,0</td><td>59,0</td><td>50</td><td>2-16</td></tr>
<tr><td>4098 25.025</td><td>12</td><td>42</td><td>25</td><td>84,0</td><td>59,0</td><td>60</td><td>2-16</td></tr>
<tr><td>4098 32.032</td><td>16</td><td>50</td><td>32</td><td>91,0</td><td>64,0</td><td>80</td><td>3-20</td></tr>
<tr><td>4098 32.040</td><td>16</td><td>50</td><td>40</td><td>91,0</td><td>64,0</td><td>80</td><td>0,12</td></tr>
</tbody></table>
""".strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class GuhringVerifierTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> dict[str, Path]:
        catalogs = root / "catalogs"
        parsed = root / "parsed"
        logs = root / "logs"
        pdf = catalogs / "test" / "guhring_catalog_test.pdf"
        pdf.parent.mkdir(parents=True)
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        with pdf.open("wb") as handle:
            writer.write(handle)
        source_sha = verify_guhring_test.sha256_file(pdf)
        source_map = pdf.with_suffix(".source.json")
        write_json(
            source_map,
            {
                "source_file": "/catalogs/input/source.pdf",
                "source_sha256": "f" * 64,
                "source_pages": [1309],
                "test_file": f"/catalogs/test/{pdf.name}",
                "test_sha256": source_sha,
                "test_pages": 1,
            },
        )

        config = {
            "cache_schema": "v1",
            "mineru_version": "3.4.5",
            "model_revision": "mineru-3.4.5-pipeline",
            "runtime_fingerprint": "a" * 64,
            "device_mode": "cpu",
            "table_merge_enable": "0",
            "request": {
                "backend": "pipeline",
                "parse_method": "auto",
                "table_enable": "true",
                "formula_enable": "true",
            },
        }
        config_digest = hashlib.sha256(verify_guhring_test.canonical_json(config)).hexdigest()
        cache_key = hashlib.sha256(
            f"guhring-cache-v1\0{source_sha}\0{0}\0{config_digest}".encode("utf-8")
        ).hexdigest()
        page_dir = parsed / "cache" / "v1" / source_sha / config_digest / "pages" / "000000"
        attempt = page_dir / "attempts" / "successful"
        extracted = attempt / "extracted" / "guhring-e2e" / "auto"
        response = attempt / "response.zip"
        response.parent.mkdir(parents=True, exist_ok=True)
        response.write_bytes(b"immutable zip bytes")
        response_sha = verify_guhring_test.sha256_file(response)
        manifest = {
            "status": "success",
            "source_file": "guhring-e2e.pdf",
            "source_sha256": source_sha,
            "page": 0,
            "cache_key": cache_key,
            "config_digest": config_digest,
            "config": config,
            "response_sha256": response_sha,
            "processing_time": 1.5,
        }
        raw_archive = parsed / "raw" / source_sha / config_digest / "page-000001" / "successful"
        manifest["raw_archive"] = str(raw_archive)
        write_json(attempt / "manifest.json", manifest)
        write_json(
            extracted / "guhring-e2e_content_list.json",
            [
                {
                    "type": "text",
                    "page_idx": 0,
                    "text": "für Werkzeugschäfte d1 h6; Art.-Nr. 4097; Schaft-Ø; 0,12",
                },
                {"type": "table", "page_idx": 0, "bbox": [10, 20, 900, 450], "table_body": TABLE_ONE},
                {"type": "table", "page_idx": 0, "bbox": [10, 460, 900, 950], "table_body": TABLE_TWO},
            ],
        )
        manifest["raw_archive"] = str(raw_archive)
        write_json(attempt / "manifest.json", manifest)
        shutil.copytree(attempt, raw_archive)
        write_json(
            page_dir / "success.json",
            {
                "status": "success",
                "attempt": "attempts/successful",
                "cache_key": cache_key,
                "response_sha256": response_sha,
                "raw_archive": str(raw_archive),
            },
        )

        workbook = parsed / "master_raw.xlsx"
        scan = export_raw.scan_outputs(parsed)
        export_raw.write_workbook(scan, workbook)

        ragflow_log = logs / "ragflow-e2e-latest.json"
        write_json(
            ragflow_log,
            {
                "status": "success",
                "source_sha256": source_sha,
                "document_id": "doc-1",
                "document": {"run": "3", "progress": 1},
                "mineru_url": "http://mineru:8000",
                "mineru_openapi_head": 200,
                "dataset_parser_config": {"layout_recognize": "MinerU", "task_page_size": 1},
                "mineru_health": {
                    "status": "healthy",
                    "mineru_version": "3.4.5",
                    "device": "cpu",
                    "model_revision": "mineru-3.4.5-pipeline",
                    "runtime_fingerprint": "a" * 64,
                },
                "mineru_cache_artifacts": [
                    {
                        "status": "success",
                        "page": 0,
                        "cache_key": cache_key,
                        "mineru_version": "3.4.5",
                        "manifest": str(attempt / "manifest.json"),
                    }
                ],
            },
        )
        return {
            "catalogs": catalogs,
            "parsed": parsed,
            "logs": logs,
            "pdf": pdf,
            "source_map": source_map,
            "workbook": workbook,
            "ragflow_log": ragflow_log,
            "attempt": attempt,
        }

    def run_verifier(self, paths: dict[str, Path]) -> dict[str, object]:
        return verify_guhring_test.verify(
            parsed_root=paths["parsed"],
            logs_root=paths["logs"],
            pdf=paths["pdf"],
            source_map=paths["source_map"],
            workbook=paths["workbook"],
            ragflow_log=paths["ragflow_log"],
            report_path=paths["logs"] / "guhring-test-verification.json",
        )

    def test_complete_persisted_fixture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            report = self.run_verifier(paths)
            self.assertEqual(report["status"], "success", report.get("failures"))
            persisted = json.loads(
                (paths["logs"] / "guhring-test-verification.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["fixture"]["original_source_page"], 1309)
            self.assertEqual(persisted["metrics"]["tables_detected"], 2)

    def test_missing_h6_is_reported_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_fixture(Path(temporary))
            artifact = next((paths["attempt"] / "extracted").rglob("*_content_list.json"))
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            payload[0]["text"] = payload[0]["text"].replace("d1 h6", "d1")
            write_json(artifact, payload)
            report = self.run_verifier(paths)
            self.assertEqual(report["status"], "failed")
            self.assertIn("source_symbols_and_h6_tolerance_preserved", report["failures"])


if __name__ == "__main__":
    unittest.main()
