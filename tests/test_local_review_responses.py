"""Exercise model response parsing over HTTP without downloading or running weights."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_review as local
import subscription_review as review


@pytest.fixture
def ollama(tmp_path):
    state = {"style": "content", "family": "qwen3vl", "received": []}
    model = "qwen3-vl:32b-thinking-bf16"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            self.send_json({"models": [{"name": model, "digest": "fixture-digest"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                self.send_json(
                    {
                        "capabilities": ["vision", "thinking"],
                        "details": {"family": state["family"]},
                    }
                )
                return
            if body["messages"] == []:
                self.send_json({"model": model, "done": True, "done_reason": "load"})
                return
            state["received"].append(body)
            if state["style"] == "http_text":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"upstream failed\n" * 1000)
                return
            schema = body["format"]["properties"]
            result = {
                "task_id": schema["task_id"]["enum"][0],
                "context_sha256": schema["context_sha256"]["enum"][0],
                "status": "passed",
                "confidence": 0.99,
                "summary": "Fixture: page 1 title CFK matches the original image.",
                "checks": [
                    {
                        "check": "page_correctness",
                        "status": "passed",
                        "evidence": "Fixture: page 1 heading reads CFK, aligned with the source.",
                        "source_url": "",
                    }
                ],
                "findings": [],
                "symbols": [],
                "series": [],
            }
            style = state["style"]
            if style == "wrong_binding":
                result["task_id"] = "another-task"
            if style == "invalid_schema":
                result.pop("checks")
            if style == "thinking_weak":
                result["summary"] = ""
            text = json.dumps(result)
            response = {
                "model": model,
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": text},
                "eval_count": 516,
                "prompt_eval_count": 4167,
            }
            if style == "fenced":
                response["message"]["content"] = "```json\n" + text + "\n```"
            elif style == "bom":
                response["message"]["content"] = "\ufeff" + text
            elif style in (
                "thinking_json",
                "thinking_weak",
                "wrong_binding",
                "invalid_schema",
            ):
                response["message"] = {"content": "", "thinking": text}
            elif style == "thinking_prose":
                response["message"] = {
                    "content": "",
                    "thinking": "A possible result is " + text,
                }
            elif style == "empty":
                response["message"] = {"content": "", "thinking": ""}
            elif style == "invalid_json":
                response["message"]["content"] = "Here is my review:\n" + text
            elif style == "mixed_channels":
                response["message"] = {"content": "Invalid answer", "thinking": text}
            elif style == "truncated":
                response["done_reason"] = "length"
                response["message"]["content"] = text[:80]
            elif style == "wrong_model":
                response["model"] = "another-model"
            self.send_json(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    image = tmp_path / "page.png"
    image.write_bytes(b"original page fixture")

    def task(reasoning="none"):
        request = review.create_task(
            tmp_path / "tasks",
            "correctness",
            {"page": 1},
            [image],
            model=model,
            reasoning=reasoning,
            provider="local",
            local={
                "url": f"http://127.0.0.1:{server.server_port}",
                "ram_gb": 128,
                "context_tokens": 32768,
                "output_tokens": 4096,
                "use_gpu": False,
                "temperature": 0.2,
            },
        )
        return tmp_path / "tasks" / request["task_id"]

    try:
        yield state, task
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("style", ["content", "fenced", "bom", "thinking_json"])
def test_complete_answers_remain_schema_bound_and_original_response_is_saved(
    ollama, style
):
    state, task = ollama
    state["style"] = style
    folder = task()
    result = local.execute(folder)
    assert result["status"] == "passed"
    response = json.loads((folder / "ollama-response.json").read_text())
    execution = json.loads((folder / "execution.json").read_text())
    diagnostics = json.loads((folder / "response-diagnostics.json").read_text())
    expected = "thinking_json_compatibility" if style == "thinking_json" else "content"
    assert execution["answer_channel"] == diagnostics["answer_channel"] == expected
    assert execution["usage"]["eval_count"] == response["eval_count"] == 516
    payload = state["received"][0]
    assert payload["think"] is False and payload["options"]["num_gpu"] == 0
    assert len(payload["messages"]) == 1 and len(payload["messages"][0]["images"]) == 1
    assert json.dumps(payload["format"]) in payload["messages"][0]["content"]


@pytest.mark.parametrize(
    "style,reasoning,family,error",
    [
        ("empty", "none", "qwen3vl", "empty final answer"),
        ("thinking_prose", "none", "qwen3vl", "empty final answer"),
        ("thinking_json", "medium", "qwen3vl", "empty final answer"),
        ("thinking_json", "none", "other", "empty final answer"),
        ("wrong_binding", "none", "qwen3vl", "empty final answer"),
        ("invalid_schema", "none", "qwen3vl", "empty final answer"),
        ("invalid_json", "none", "qwen3vl", "invalid review JSON"),
        ("mixed_channels", "none", "qwen3vl", "invalid review JSON"),
        ("truncated", "none", "qwen3vl", "incomplete.*length"),
        ("wrong_model", "none", "qwen3vl", "different model"),
    ],
)
def test_invalid_answers_never_produce_results_and_keep_diagnostics(
    ollama, style, reasoning, family, error
):
    state, task = ollama
    state.update(style=style, family=family)
    folder = task(reasoning)
    with pytest.raises(ValueError, match=error):
        local.execute(folder)
    assert not (folder / "result.json").exists()
    assert (folder / "ollama-response.json").is_file()
    diagnostics = json.loads((folder / "response-diagnostics.json").read_text())
    assert diagnostics["eval_count"] == 516
    assert "answer_channel" not in diagnostics
    assert (
        json.loads((folder / "execution.json").read_text())["usage"]["eval_count"]
        == 516
    )


def test_non_json_http_response_records_bounded_body_and_cli_error(ollama):
    state, task = ollama
    state["style"] = "http_text"
    folder = task()
    command = subprocess.run(
        [sys.executable, str(Path(local.__file__)), "--task", str(folder)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert command.returncode == 1
    error = json.loads((folder / "error.json").read_text())["error"]
    assert "/api/chat returned HTTP 200 without valid JSON" in error
    assert "upstream failed" in error and "Expecting value" not in error
    saved = json.loads((folder / "ollama-http.json").read_text())
    assert saved["status"] == 200 and saved["truncated"] is True
    assert len(saved["body"]) == 8192
    assert not (folder / "result.json").exists()


def test_qwen_channel_compatibility_preserves_evidence_gates(ollama):
    state, task = ollama
    state["style"] = "thinking_weak"
    result = local.execute(task())
    assert result["status"] == "unverified" and result["confidence"] == 0
    assert result["findings"][0]["code"] == "LOCAL_UNGROUNDED_REVIEW"


@pytest.mark.parametrize(
    "text",
    [
        'prefix {"status":"passed"}',
        '{"status":"passed"} trailing',
        '{"status":"passed","status":"issues"}',
        '{"confidence":NaN}',
        "[]",
        "```json\n{}\n```\nExtra text",
        '{"status":',
    ],
)
def test_no_json_fragment_extraction_or_silent_repair(text):
    with pytest.raises(ValueError):
        local.json_answer(text)
