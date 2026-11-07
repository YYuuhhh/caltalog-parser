"""Cold-load lifecycle through real HTTP/subprocesses, without model weights."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_ui as ui

MODEL = "qwen3-vl:32b-thinking-bf16"


@pytest.fixture
def loader(tmp_path, monkeypatch):
    release = threading.Event()
    state = {"calls": [], "fail": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                result, status = {"capabilities": ["vision", "thinking"]}, 200
            else:
                assert self.path == "/api/chat" and body["messages"] == []
                assert "prompt" not in body and "format" not in body
                state["calls"].append(body)
                assert release.wait(10), "Test did not release the cold-load fixture"
                result, status = (
                    ({"error": "timed out waiting for llama-server to start"}, 500)
                    if state["fail"]
                    else (
                        {"model": body["model"], "done": True, "done_reason": "load"},
                        200,
                    )
                )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(result).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # Cancellation closes the loader's HTTP connection.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        ui,
        "ROOTS",
        {**ui.ROOTS, "logs": tmp_path / "logs", "parsed": tmp_path / "parsed"},
    )
    jobs = ui.Jobs()
    job = "d" * 32
    jobs.active = job
    settings = ui.Settings(
        model=MODEL,
        final_model=MODEL,
        reasoning="medium",
        local_url=f"http://127.0.0.1:{server.server_port}",
        local_ram_gb=128,
    )
    jobs.save(
        job,
        {
            "id": job,
            "status": "running",
            "config": settings.model_dump(),
            "review_tasks": [],
            "stages": [{"id": "parse", "title": "Extract", "status": "running"}],
        },
    )
    try:
        yield jobs, job, settings, state, release
    finally:
        jobs.stop_model_load(job)
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def wait_for(predicate):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Preload did not reach expected state")


@pytest.mark.parametrize("threads", [0, 2])
def test_preload_runs_concurrently_deduplicates_and_reports_ready(loader, threads):
    jobs, job, settings, server, release = loader
    settings = settings.model_copy(update={"local_cpu_threads": threads})
    start = time.monotonic()
    jobs.start_model_load(job, settings, MODEL)
    assert time.monotonic() - start < 1
    process = jobs.model_loader["process"]
    wait_for(lambda: server["calls"])
    assert process.poll() is None
    assert jobs.read(job)["model_load"]["status"] == "loading"
    jobs.start_model_load(job, settings, MODEL)
    assert jobs.model_loader["process"] is process
    with pytest.raises(RuntimeError, match="one model"):
        jobs.start_model_load(job, settings, "qwen3.5:9b")
    assert len(server["calls"]) == 1
    expected = {
        "num_ctx": settings.local_context_tokens,
        "num_gpu": 0,
        "use_mmap": False,
    }
    if threads:
        expected["num_thread"] = threads
    assert server["calls"][0]["options"] == expected
    assert server["calls"][0]["keep_alive"] == -1
    release.set()
    jobs.wait_model_load(job, MODEL)
    assert jobs.read(job)["model_load"]["status"] == "ready"
    jobs.stop_model_load(job)
    assert process.poll() == 0 and jobs.model_loader is None


def test_preload_failure_is_saved_and_review_gets_actionable_cause(loader):
    jobs, job, settings, server, release = loader
    server["fail"] = True
    release.set()
    jobs.start_model_load(job, settings, MODEL)
    with pytest.raises(RuntimeError, match="OLLAMA_LOAD_TIMEOUT"):
        jobs.wait_model_load(job, MODEL)
    assert jobs.read(job)["model_load"]["status"] == "failed"
    assert "timed out waiting for llama-server" in jobs.read(job)["model_load"]["error"]


def test_stop_catalog_terminates_background_load(loader):
    jobs, job, settings, server, _ = loader
    jobs.start_model_load(job, settings, MODEL)
    process = jobs.model_loader["process"]
    wait_for(lambda: server["calls"])
    assert jobs.cancel(job)["status"] == "stopping"
    wait_for(lambda: process.poll() is not None)
    jobs.stop_model_load(job)
    assert jobs.read(job)["model_load"]["status"] == "stopped"
    assert process.returncode < 0


@pytest.mark.parametrize(
    "change,model",
    [
        ({"local_preload": False}, MODEL),
        ({"local_keep_loaded": False}, MODEL),
        ({}, "none"),
    ],
)
def test_disabled_preload_and_no_model_do_not_start_a_process(loader, change, model):
    jobs, job, settings, server, _ = loader
    jobs.start_model_load(job, settings.model_copy(update=change), model)
    assert jobs.model_loader is None and server["calls"] == []
