"""Real PDF/HTTP/ZIP/RAW round trip, without downloading OCR model weights."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

import fitz
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import baidu_extract as extract
import baidu_ocr
from baidu_cpu_runtime import MODEL, REVISION
import catalog_etl as etl
import catalog_registry as registry
import export_raw
import parse_catalogs


@pytest.fixture
def ocr_server():
    state = {"calls": 0, "cancelled": 0, "incomplete": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/ocr"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            image = base64.b64decode(data["image"])
            assert (
                image.startswith(b"\x89PNG")
                and hashlib.sha256(image).hexdigest() == data["source_sha256"]
            )
            state.update(calls=state["calls"] + 1, sha=data["source_sha256"])
            self.send(202, {"id": "a" * 32})

        def do_GET(self):
            assert self.path == "/jobs/" + "a" * 32
            self.send(
                200,
                {
                    "status": "completed",
                    "progress": {"status": "completed", "tokens": 50},
                    "result": {
                        "model": MODEL,
                        "revision": REVISION,
                        "source_sha256": state["sha"],
                        "device": "cpu",
                        "complete": not state["incomplete"],
                        "text": "Product dimensions\n| Article number | d1 |\n| --- | --- |\n| 00123 | 1,25 ±0,01 mm |",
                    },
                },
            )

        def do_DELETE(self):
            state["cancelled"] += 1
            self.send(200, {"status": "cancelled"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_cpu_extraction_roundtrip_resume_and_backend_change(tmp_path, ocr_server):
    url, service = ocr_server
    source = tmp_path / "catalog.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 50), "Product 00123")
        pdf.new_page().insert_text((40, 50), "Product 00123")
        pdf.save(source)
    manifest = registry.register(
        source,
        catalog_id="baidu-test",
        manufacturer="Gühring",
        role="TARGET",
        revision="2026",
        mapping_profile="guhring",
        families=["milling_cutters"],
        languages=["en"],
    )
    path = tmp_path / "catalog.yaml"
    registry.save_manifest(path, manifest)
    args = parse_catalogs.build_parser().parse_args(
        [
            "--manifest",
            str(path),
            "--schema",
            str(
                Path(__file__).resolve().parents[1]
                / "schemas/catalog-manifest.schema.json"
            ),
            "--output",
            str(tmp_path / "extracted"),
            "--logs-root",
            str(tmp_path / "logs"),
            "--api-url",
            url,
            "--backend",
            "baidu-cpu",
            "--progress-file",
            str(tmp_path / "progress.json"),
        ]
    )
    result = parse_catalogs.parse_manifest(args)
    assert result["requested_pages_complete"] and service["calls"] == 2
    assert (
        parse_catalogs.parse_manifest(args)["requested_pages_complete"]
        and service["calls"] == 2
    )
    saved = yaml.safe_load(path.read_text())
    artifact = Path(saved["page_states"][0]["artifact_path"])
    scan = export_raw.scan_outputs(artifact.parent / "files")
    assert not scan.error_count
    raw = tmp_path / "raw.xlsx"
    export_raw.write_workbook(scan, raw)
    _, records, _ = etl.load_raw_workbook(raw)
    assert {r.values["raw_value"] for r in records} >= {"00123", "1,25 ±0,01 mm"}
    assert all(
        r.values["parser_backend"] == "baidu-cpu"
        and r.source_sha256 == registry.digest(source)
        and r.page_idx == 0
        and not r.values["bbox"]
        for r in records
    )
    assert json.loads((tmp_path / "progress.json").read_text())["stage"] == "parse"
    old_key = saved["page_states"][0]["cache_key"]
    args.ocr_dpi = 216
    assert (
        parse_catalogs.parse_manifest(args)["requested_pages_complete"]
        and service["calls"] == 4
    )
    assert yaml.safe_load(path.read_text())["page_states"][0]["cache_key"] != old_key
    assert artifact.is_file()  # The earlier extraction remains immutable.
    args.backend = "pipeline"
    assert not parse_catalogs.matches_backend(artifact, args)


def test_incomplete_cpu_ocr_is_rejected_and_remote_worker_cancelled(
    tmp_path, ocr_server
):
    url, state = ocr_server
    state["incomplete"] = True
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNGfake-test-image")
    with pytest.raises(ValueError, match="incomplete"):
        baidu_ocr.check(image, tmp_path / "result.json", url, 5, 2048, 2)
    assert state["cancelled"] == 1 and not (tmp_path / "result.json").exists()


def test_html_and_markdown_preserve_cells_and_reject_partial_tables():
    for text in (
        "<table><tr><th>Article number</th><th>d1</th></tr><tr><td>00123</td><td>1,25</td></tr></table>",
        "| Article number | d1 |\n| --- | --- |\n| 00123 | 1,25 |",
    ):
        blocks = extract.content_blocks(text, 3)
        assert (
            len(blocks) == 1
            and blocks[0]["type"] == "table"
            and blocks[0]["page_idx"] == 3
        )
        assert "00123" in blocks[0]["table_body"] and "bbox" not in blocks[0]
    with pytest.raises(ValueError, match="incomplete table"):
        extract.content_blocks("<table><tr><td>00123</td></tr>", 0)


def test_unbounded_cli_does_not_silently_extract_only_first_page(monkeypatch, capsys):
    monkeypatch.setattr(parse_catalogs.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["parse_catalogs.py", "--backend", "baidu-cpu"])
    assert parse_catalogs.main() == 2
    assert "--manifest for full catalogs" in capsys.readouterr().err
