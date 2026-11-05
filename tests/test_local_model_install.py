"""Installer contract against streamed HTTP, with no real weight downloads."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_model_install as installs
import pipeline_ui as ui

MODEL = "qwen3.5:9b"


@pytest.fixture
def ollama():
    state = {
        "installed": set(),
        "pulls": [],
        "mode": "success",
        "vision": True,
        "deletes": [],
        "delete_status": 200,
    }
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            assert self.path == "/api/tags"
            self.reply({"models": [{"name": name} for name in state["installed"]]})

        def do_DELETE(self):
            assert self.path == "/api/delete"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["deletes"].append(body)
            status = state["delete_status"]
            if 200 <= status < 300:
                if body["model"] not in state["installed"]:
                    status = 404
                else:
                    state["installed"].remove(body["model"])
            self.send_response(status)
            self.end_headers()
            if status >= 400:
                self.wfile.write(
                    json.dumps(
                        {
                            "error": "model not found"
                            if status == 404
                            else "disk failure"
                        }
                    ).encode()
                )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                assert body["model"] in state["installed"]
                return self.reply(
                    {
                        "capabilities": ["vision", "thinking"]
                        if state["vision"]
                        else ["completion"]
                    }
                )
            assert self.path == "/api/pull"
            assert body == {"model": MODEL, "stream": True}
            state["pulls"].append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()

            def write(value):
                self.wfile.write(json.dumps(value).encode() + b"\n")
                self.wfile.flush()

            write(
                {
                    "status": "pulling weights",
                    "digest": "first",
                    "total": 100,
                    "completed": 20,
                }
            )
            assert release.wait(5), "Test did not release download"
            if state["mode"] == "error":
                write({"error": "no space left on device"})
                return
            if state["mode"] == "incomplete":
                return
            write(
                {
                    "status": "pulling weights",
                    "digest": "first",
                    "total": 100,
                    "completed": 100,
                }
            )
            write(
                {
                    "status": "pulling projector",
                    "digest": "second",
                    "total": 200,
                    "completed": 200,
                }
            )
            state["installed"].add(body["model"])
            write({"status": "success"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def wait_for(manager, url, root, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        jobs = manager.list(url, root)
        if jobs and predicate(jobs[0]):
            return jobs[0]
        time.sleep(0.01)
    raise AssertionError(manager.list(url, root))


def test_download_progress_is_shared_deduplicated_verified_and_persisted(
    ollama, tmp_path
):
    url, server, release = ollama
    manager = installs.Installer()
    initial = manager.start(url + "/", MODEL, tmp_path)
    progress = wait_for(manager, url, tmp_path, lambda j: j["completed"] == 20)
    assert progress["status"] == "downloading" and progress["percent"] == 20
    assert manager.start(url, MODEL, tmp_path)["id"] == initial["id"]
    with pytest.raises(ValueError, match="Another model"):
        manager.start(url, "qwen3-vl:8b-instruct", tmp_path)
    release.set()
    result = wait_for(manager, url, tmp_path, lambda j: j["status"] == "installed")
    assert result["percent"] == 100
    assert result["total"] == result["completed"] == 300
    assert result["model_info"]["vision"]
    assert server["pulls"] == [{"model": MODEL, "stream": True}]
    reloaded = installs.Installer().list(url, tmp_path)
    assert reloaded == [result]
    assert manager.list("http://127.0.0.1:1", tmp_path) == []


@pytest.mark.parametrize(
    "mode,error", [("error", "no space left"), ("incomplete", "before Ollama verified")]
)
def test_failed_download_stays_unavailable_and_retry_resumes(
    ollama, tmp_path, mode, error
):
    url, server, release = ollama
    manager = installs.Installer()
    server["mode"] = mode
    release.set()
    first = manager.start(url, MODEL, tmp_path)
    failed = wait_for(manager, url, tmp_path, lambda j: j["status"] == "failed")
    assert error in failed["error"] and failed["percent"] != 100
    assert MODEL not in server["installed"]
    server["mode"] = "success"
    retry = manager.start(url, MODEL, tmp_path)
    assert retry["id"] != first["id"]
    wait_for(
        manager,
        url,
        tmp_path,
        lambda j: j["id"] == retry["id"] and j["status"] == "installed",
    )
    assert len(server["pulls"]) == 2


def test_existing_model_never_downloads_and_non_vision_cannot_complete(
    ollama, tmp_path
):
    url, server, _ = ollama
    server["installed"].add(MODEL)
    manager = installs.Installer()
    manager.start(url, MODEL, tmp_path)
    wait_for(manager, url, tmp_path, lambda j: j["status"] == "installed")
    assert server["pulls"] == []
    server["vision"] = False
    job = manager.start(url, MODEL, tmp_path)
    failed = wait_for(
        manager,
        url,
        tmp_path,
        lambda j: j["id"] == job["id"] and j["status"] == "failed",
    )
    assert "vision" in failed["error"]
    assert server["pulls"] == []


def test_restart_exposes_interrupted_download_as_retryable(ollama, tmp_path):
    url, _, _ = ollama
    state = {
        "id": "a" * 32,
        "url": url,
        "model": MODEL,
        "status": "downloading",
        "started_at": "2026-11-05",
        "completed": 30,
    }
    folder = tmp_path / state["id"]
    folder.mkdir()
    (folder / "state.json").write_text(json.dumps(state))
    result = installs.Installer().list(url, tmp_path)[0]
    assert result["status"] == "interrupted" and result["completed"] == 30
    assert "resume" in result["message"]


def test_install_api_validates_inputs_and_preserves_same_origin(
    ollama, tmp_path, monkeypatch
):
    url, server, release = ollama
    monkeypatch.setattr(ui, "ROOTS", {**ui.ROOTS, "logs": tmp_path})
    monkeypatch.setattr(ui, "model_installer", installs.Installer())
    client = TestClient(ui.app)
    for bad in [
        {"url": "https://ollama.com", "model": MODEL},
        {"url": url, "model": "qwen3:cloud"},
        {"url": url, "model": "none"},
    ]:
        assert client.post("/api/local-model-installs", json=bad).status_code == 400
    assert (
        client.post(
            "/api/local-model-installs",
            json={"url": url, "model": MODEL},
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert server["pulls"] == []
    release.set()
    response = client.post(
        "/api/local-model-installs", json={"url": url, "model": MODEL}
    )
    assert response.status_code == 202, response.text
    wait_for(
        ui.model_installer,
        url,
        tmp_path / "local-model-installs",
        lambda j: j["status"] == "installed",
    )
    status = client.get("/api/local-model-installs", params={"url": url})
    assert status.json()["installs"][0]["id"] == response.json()["id"]


@pytest.mark.parametrize("status", [200, 204])
def test_delete_installed_model_accepts_empty_response_and_can_reinstall(
    ollama, tmp_path, status
):
    url, server, release = ollama
    manager = installs.Installer()
    release.set()
    manager.start(url, MODEL, tmp_path)
    wait_for(manager, url, tmp_path, lambda j: j["status"] == "installed")
    server["installed"].add("other-model:1b")
    server["delete_status"] = status
    checks = []
    result = manager.delete(
        url + "/", MODEL, tmp_path, lambda *args: checks.append(args)
    )
    assert result == {"status": "deleted", "model": MODEL, "already_absent": False}
    assert checks == [(url, MODEL)]
    assert server["installed"] == {"other-model:1b"}
    assert server["deletes"] == [{"model": MODEL}]
    history = installs.Installer().list(url, tmp_path)
    assert all(j["status"] == "deleted" for j in history)
    manager.start(url, MODEL, tmp_path)
    wait_for(manager, url, tmp_path, lambda j: j["status"] == "installed")
    assert len(server["pulls"]) == 2


def test_delete_absent_is_idempotent_and_failure_preserves_model(ollama, tmp_path):
    url, server, _ = ollama
    manager = installs.Installer()
    assert manager.delete(url, MODEL, tmp_path, lambda *_: None)["already_absent"]
    server["installed"].add(MODEL)
    server["delete_status"] = 500
    with pytest.raises(installs.DeleteError, match="disk failure"):
        manager.delete(url, MODEL, tmp_path, lambda *_: None)
    assert MODEL in server["installed"]
    assert not manager.is_removing(url, MODEL)
    server["delete_status"] = 200
    manager.delete(url, MODEL, tmp_path, lambda *_: None)
    assert MODEL not in server["installed"]


def test_cannot_delete_during_installation(ollama, tmp_path):
    url, server, release = ollama
    manager = installs.Installer()
    manager.start(url, MODEL, tmp_path)
    wait_for(manager, url, tmp_path, lambda j: j["completed"] == 20)
    with pytest.raises(installs.Conflict, match="installation"):
        manager.delete(url, "other-model:1b", tmp_path, lambda *_: None)
    assert server["deletes"] == []
    release.set()
    wait_for(manager, url, tmp_path, lambda j: j["status"] == "installed")


@pytest.mark.parametrize("reservation", ["active", "queued", "paused"])
@pytest.mark.parametrize("stage", ["review", "final"])
def test_delete_api_protects_models_reserved_by_catalogs(
    ollama, tmp_path, monkeypatch, reservation, stage
):
    url, server, _ = ollama
    server["installed"].add(MODEL)
    manager = installs.Installer()
    jobs = ui.Jobs()
    monkeypatch.setattr(ui, "ROOTS", {**ui.ROOTS, "logs": tmp_path})
    monkeypatch.setattr(ui, "model_installer", manager)
    monkeypatch.setattr(ui, "jobs", jobs)
    job = "a" * 32
    folder = jobs.folder(job)
    folder.mkdir(parents=True)
    config = {
        "review_provider": "work",
        "final_provider": "work",
        "local_url": url + "/",
    }
    config.update(
        {
            "review_provider" if stage == "review" else "final_provider": "local",
            "model" if stage == "review" else "final_model": MODEL,
        }
    )
    (folder / "state.json").write_text(
        json.dumps(
            {"config": config, "source_name": "catalog.pdf", "status": reservation}
        )
    )
    if reservation == "active":
        jobs.active = job
    else:
        jobs.pending.append(job)
    client = TestClient(ui.app)
    response = client.request(
        "DELETE", "/api/local-models", json={"url": url, "model": MODEL}
    )
    assert response.status_code == 409 and "catalog.pdf" in response.json()["detail"]
    assert server["deletes"] == []
    assert not manager.is_removing(url, MODEL)
    # A queued Work task or a task on a different Ollama server is unrelated.
    config["local_url"] = "http://127.0.0.1:1"
    (folder / "state.json").write_text(json.dumps({"config": config}))
    response = client.request(
        "DELETE", "/api/local-models", json={"url": url, "model": MODEL}
    )
    assert response.status_code == 200


def test_deletion_reservation_blocks_racing_installs_deletes_and_run_submission(
    ollama, tmp_path, monkeypatch
):
    url, server, _ = ollama
    server["installed"].add(MODEL)
    manager = installs.Installer()
    monkeypatch.setattr(ui, "model_installer", manager)
    jobs = ui.Jobs()
    monkeypatch.setattr(ui, "ROOTS", {**ui.ROOTS, "logs": tmp_path})
    monkeypatch.setattr(ui, "validate_settings", lambda _: (None, None, None))
    config = ui.Settings(model=MODEL, local_url=url)
    checked = []

    def while_reserved(*_):
        assert manager.is_removing(url + "/", MODEL)
        with pytest.raises(installs.Conflict):
            manager.start(url, MODEL, tmp_path)
        with pytest.raises(installs.Conflict):
            manager.delete(url, MODEL, tmp_path, lambda *_: None)
        with pytest.raises(ui.HTTPException) as exc:
            jobs.check_models_not_deleting(config)
        assert exc.value.status_code == 409
        with pytest.raises(ui.HTTPException) as exc:
            jobs.start_many([config])
        assert exc.value.status_code == 409
        job = "b" * 32
        jobs.folder(job).mkdir(parents=True)
        (jobs.folder(job) / "state.json").write_text(
            json.dumps(
                {"status": "failed", "config": config.model_dump(), "stages": []}
            )
        )
        with pytest.raises(ui.HTTPException) as exc:
            jobs.resume(job)
        assert exc.value.status_code == 409
        checked.append(True)

    manager.delete(url, MODEL, tmp_path, while_reserved)
    assert checked and not manager.is_removing(url, MODEL)


def test_delete_api_validation_origin_and_server_failure(ollama, tmp_path, monkeypatch):
    url, server, _ = ollama
    monkeypatch.setattr(ui, "ROOTS", {**ui.ROOTS, "logs": tmp_path})
    monkeypatch.setattr(ui, "model_installer", installs.Installer())
    monkeypatch.setattr(ui, "jobs", ui.Jobs())
    client = TestClient(ui.app)
    for bad in [
        {"url": "https://ollama.com", "model": MODEL},
        {"url": url, "model": "qwen3:cloud"},
        {"url": url, "model": "none"},
        {"url": url, "model": "bad model"},
    ]:
        assert (
            client.request("DELETE", "/api/local-models", json=bad).status_code == 400
        )
    body = {"url": url, "model": MODEL}
    assert (
        client.request(
            "DELETE",
            "/api/local-models",
            json=body,
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert not server["deletes"]
    server["installed"].add(MODEL)
    server["delete_status"] = 500
    response = client.request("DELETE", "/api/local-models", json=body)
    assert response.status_code == 502 and "disk failure" in response.json()["detail"]
    assert MODEL in server["installed"]
