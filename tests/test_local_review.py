"""Local review routing and evidence guards, without generating regression answers."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_review as local
import subscription_review as review


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com",
        "https://ollama.com",
        "http://8.8.8.8",
        "http://u:p@localhost:11434",
        "http://localhost:11434/api",
        "file:///tmp/server",
    ],
)
def test_reject_cloud_and_invalid_local_servers(url):
    with pytest.raises(ValueError):
        local.validate_url(url)


@pytest.mark.parametrize(
    "model", ["qwen3-vl:235b-cloud", "model name", "", "$(whoami)"]
)
def test_reject_cloud_and_invalid_model_names(model):
    with pytest.raises(ValueError):
        local.validate_model(model, "medium")


def test_local_requests_are_published_with_provider_and_settings(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"original page evidence")
    config = {"url": "http://local-llm:11434", "ram_gb": 32}
    task = review.create_task(
        tmp_path / "tasks",
        "correctness",
        {"page": 3},
        [image],
        model="qwen3.5:9b",
        provider="local",
        local=config,
    )
    folder = tmp_path / "tasks" / task["task_id"]
    saved = json.loads((folder / "request.json").read_text())
    assert saved["provider"] == "local" and saved["local"] == config
    review.validate_evidence(saved, folder)
    schema = json.loads((folder / "schema.json").read_text())
    assert schema["properties"]["task_id"]["enum"] == [task["task_id"]]
    assert schema["properties"]["context_sha256"]["enum"] == [task["context_sha256"]]


def test_resume_reuses_only_exact_completed_evidence_and_model(tmp_path):
    image = tmp_path / "original.png"
    image.write_bytes(b"immutable page")
    config = {
        "url": "http://local-llm:11434",
        "source_urls": "https://manufacturer.example/tool",
        "keep_loaded": True,
    }
    context = {"page": 4, "chunk": 1, "raw_cells": [{"raw_value": "6.000"}]}
    queue = tmp_path / "tasks"
    task = review.create_task(
        queue,
        "correctness",
        context,
        [image],
        model="qwen3-vl:8b-instruct",
        reasoning="none",
        provider="local",
        local=config,
    )
    result = {
        "task_id": task["task_id"],
        "context_sha256": task["context_sha256"],
        "status": "passed",
        "confidence": 0.99,
        "summary": "Fixture: page 4 table 1 row 2 diameter is 6.000 mm.",
        "checks": [
            {
                "check": "page_correctness",
                "status": "passed",
                "evidence": "Fixture: page 4 table 1 row 2 d1=6.000 mm.",
                "source_url": "",
            }
        ],
        "findings": [],
        "symbols": [],
        "series": [],
    }
    review.atomic_json(queue / task["task_id"] / "result.json", result)
    records = [{"id": task["task_id"], "stage": "correctness", "status": "passed"}]
    options = dict(
        model=task["model"],
        reasoning="none",
        provider="local",
        local={
            **config,
            "keep_loaded": False,
            "load_timeout": 7200,
            "load_into_ram": True,
        },
        confidence_threshold=0.9,
    )
    reused = review.reusable_result(
        queue, records, "correctness", context, [image], **options
    )
    assert reused[0]["task_id"] == task["task_id"]
    assert (
        review.reusable_result(queue, [], "correctness", context, [image], **options)
        is None
    )
    assert (
        review.reusable_result(
            queue,
            records,
            "correctness",
            context,
            [image],
            **{**options, "model": "qwen3.5:9b"},
        )
        is None
    )
    assert (
        review.reusable_result(
            queue,
            records,
            "correctness",
            context,
            [image],
            **{
                **options,
                "local": {
                    **config,
                    "source_urls": "https://manufacturer.example/other",
                },
            },
        )
        is None
    )
    image.write_bytes(b"changed page")
    assert (
        review.reusable_result(
            queue, records, "correctness", context, [image], **options
        )
        is None
    )


def test_web_claims_require_retrieved_evidence():
    request = {"stage": "final", "context": {"page": 3}}
    result = {
        "status": "passed",
        "checks": [
            {
                "check": "internet",
                "status": "passed",
                "evidence": "Model memory says it matches.",
                "source_url": "https://guhring.example/invented",
            }
        ],
        "symbols": [],
        "findings": [],
    }
    local.enforce_sources(request, result, [])
    assert result["status"] == "unverified"
    assert result["checks"][0]["status"] == "unverified"
    assert result["findings"][0]["severity"] == "blocking"


def test_generic_numeric_pass_does_not_count_as_an_engineering_review():
    request = {"context": {"page": 4, "raw_cells": [{"raw_value": "6.000"}]}}
    result = {
        "status": "passed",
        "confidence": 0.9,
        "summary": "",
        "checks": [{"status": "passed", "evidence": "All values match the image."}],
        "findings": [],
    }
    local.enforce_rationale(request, result)
    assert result["status"] == "unverified" and result["confidence"] == 0
    assert result["checks"][0]["status"] == "unverified"


def test_dimension_tables_without_structured_tools_cannot_pass_final_validation():
    request = {
        "stage": "final",
        "context": {
            "page": 4,
            "products": [],
            "table_opening_rows": [{"raw_value": "d1 (e10)"}, {"raw_value": "l1"}],
        },
    }
    result = {
        "status": "passed",
        "confidence": 0.99,
        "checks": [
            {
                "check": "parameter_values",
                "status": "passed",
                "evidence": "The source has values.",
            }
        ],
        "findings": [],
    }
    local.enforce_structured_tools(request, result)
    assert result["status"] == "unverified" and result["confidence"] == 0
    assert result["checks"][0]["status"] == "unverified"
    assert result["findings"][0]["code"] == "LOCAL_MISSING_STRUCTURED_TOOLS"


def test_download_presets_fit_requested_ram_and_keep_a_separate_final_model():
    small = next(p for p in local.PRESETS if p["ram_gb"] == 32)
    large = next(p for p in local.PRESETS if p["ram_gb"] == 750)
    assert small["model"] != small["final_model"]
    assert small["weights_gb"] < 16 and large["weights_gb"] < 600
    assert all("cloud" not in p["model"] for p in local.PRESETS)


def test_real_http_adapter_sends_cpu_images_and_fresh_schema_bound_tasks(tmp_path):
    received = []
    loads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "qwen3.5:9b"}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                response = {"capabilities": ["vision", "thinking"]}
            elif body.get("messages") == []:
                loads.append(body)
                time.sleep(2)
                response = {"model": body["model"], "done": True, "done_reason": "load"}
            else:
                received.append(body)
                time.sleep(2)
                schema = body["format"]["properties"]
                names = schema["checks"]["items"]["properties"]["check"]["enum"]
                result = {
                    "task_id": schema["task_id"]["enum"][0],
                    "context_sha256": schema["context_sha256"]["enum"][0],
                    "status": "passed",
                    "confidence": 0.99,
                    "summary": "Fixture HTTP response.",
                    "checks": [
                        {
                            "check": n,
                            "status": "passed",
                            "evidence": "Fixture page/table evidence.",
                            "source_url": "",
                        }
                        for n in names
                    ],
                    "findings": [],
                    "symbols": [],
                    "series": [],
                }
                response = {
                    "model": body["model"],
                    "done": True,
                    "done_reason": "stop",
                    "message": {"content": json.dumps(result)},
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    image = tmp_path / "page.png"
    image.write_bytes(b"saved original page")
    config = {
        "url": f"http://127.0.0.1:{server.server_port}",
        "ram_gb": 32,
        "context_tokens": 32768,
        "output_tokens": 1024,
        "use_gpu": False,
        "temperature": 0.2,
    }
    try:
        ids = []
        for stage in ["correctness", "final"]:
            task = review.create_task(
                tmp_path / "tasks",
                stage,
                {"page": 4},
                [image],
                model="qwen3.5:9b",
                provider="local",
                local=config,
            )
            folder = tmp_path / "tasks" / task["task_id"]
            # Loading plus generation exceeds the review-only budget.
            task["timeout"] = 3
            review.atomic_json(folder / "request.json", task)
            outcome = local.execute(folder)
            ids.append(outcome["task_id"])
            assert outcome["status"] == (
                "passed" if stage == "correctness" else "unverified"
            )
            execution = json.loads((folder / "execution.json").read_text())
            assert (
                execution["provider"] == "ollama_local" and execution["work_calls"] == 0
            )
            assert execution["use_gpu"] is False and execution["fresh_context"]
        assert ids[0] != ids[1]
        assert len(loads) == 2
        for loaded, generated in zip(loads, received):
            assert loaded["options"] == {
                key: generated["options"][key] for key in loaded["options"]
            }
            assert loaded["options"]["use_mmap"] is False
            assert loaded["keep_alive"] == generated["keep_alive"] == -1
            assert "prompt" not in loaded and "format" not in loaded
        for payload in received:
            assert payload["options"]["num_gpu"] == 0
            assert (
                len(payload["messages"]) == 1
                and len(payload["messages"][0]["images"]) == 1
            )
        assert ids[0] not in received[1]["messages"][0]["content"]
    finally:
        server.shutdown()
        server.server_close()


def test_preload_timeout_is_separate_and_cpu_options_are_consistent():
    calls = []

    class Client:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))

            class Response:
                ok, is_redirect = True, False

                def json(self):
                    return {
                        "model": "qwen3-vl:32b-thinking-bf16",
                        "done": True,
                        "done_reason": "load",
                    }

            return Response()

    config = {
        "load_timeout": 7200,
        "context_tokens": 32768,
        "use_gpu": False,
        "load_into_ram": True,
        "keep_loaded": True,
    }
    local.load_model(
        Client(), "http://127.0.0.1:11434", "qwen3-vl:32b-thinking-bf16", config
    )
    _, request = calls[0]
    assert request["timeout"] == (10, 7200)
    assert request["json"]["messages"] == []
    assert request["json"]["options"] == {
        "num_ctx": 32768,
        "num_gpu": 0,
        "use_mmap": False,
    }
    assert local.load_options({**config, "load_into_ram": False})["use_mmap"] is True
    with pytest.raises(ValueError, match="CPU-only"):
        local.load_options({"use_gpu": True})
