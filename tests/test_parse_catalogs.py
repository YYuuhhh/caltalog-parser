"""HTTP extraction failures retain bounded parser details, without inference."""

from __future__ import annotations

import argparse
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import parse_catalogs


@pytest.fixture
def parser_server():
    state = {"status": 500, "body": b"", "content_type": "application/json"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/file_parse"
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(state["status"])
            self.send_header("Content-Type", state["content_type"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def parse(tmp_path, url):
    source = tmp_path / "catalog.pdf"
    source.write_bytes(b"PDF upload fixture; the HTTP stub never reads the PDF")
    args = argparse.Namespace(
        api_url=url,
        output=tmp_path / "parsed",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=True,
        tables=True,
        force=False,
        start_page=0,
        end_page=0,
        timeout=5,
    )
    return parse_catalogs.parse_one(args, source)


@pytest.mark.parametrize(
    "body,content_type,truncated",
    [
        (
            json.dumps(
                {
                    "detail": "persistent parsing failed: ModuleNotFoundError: No module named 'six'"
                }
            ).encode(),
            "application/json",
            False,
        ),
        (b"ModuleNotFoundError: No module named 'six'\n", "text/plain", False),
        (
            b"ModuleNotFoundError: No module named 'six' " + b"x" * 20000,
            "text/html",
            True,
        ),
    ],
)
def test_http_failure_preserves_root_cause_in_manifest_and_bounded_response(
    parser_server, tmp_path, body, content_type, truncated
):
    url, server = parser_server
    server.update(body=body, content_type=content_type)
    result = parse(tmp_path, url)
    assert result["status"] == "error" and result["http_status"] == 500
    assert "HTTPError" in result["error"] and "No module named 'six'" in result["error"]
    assert len(result["error"]) < 2300
    path = next((tmp_path / "parsed").glob("**/manifest.json"))
    assert json.loads(path.read_text()) == result
    diagnostic = json.loads(path.with_name("error-response.json").read_text())
    assert diagnostic["http_status"] == 500
    assert diagnostic["truncated"] == truncated and len(diagnostic["body"]) <= 8192


def test_empty_error_body_still_reports_http_status(parser_server, tmp_path):
    url, _ = parser_server
    result = parse(tmp_path, url)
    assert result["status"] == "error" and result["http_status"] == 500
    assert "500 Server Error" in result["error"]


def test_successful_zip_is_unchanged(parser_server, tmp_path):
    url, server = parser_server
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("catalog_content_list.json", "[]")
    server.update(status=200, body=buffer.getvalue(), content_type="application/zip")
    result = parse(tmp_path, url)
    assert result["status"] == "success"
    output = Path(result["response_zip"])
    assert (
        json.loads((output.parent / "files/catalog_content_list.json").read_text())
        == []
    )
    assert not output.with_name("error-response.json").exists()
