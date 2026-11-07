"""CPU port safety, service lifecycle and source-bound OCR evidence contracts."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

Image = pytest.importorskip(
    "PIL.Image",
    reason="Optional Baidu CPU image dependencies are not installed in the tools-only runtime.",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import baidu_cpu_runtime as runtime
import baidu_cpu_service as service
import baidu_ocr as client


def test_cpu_patch_refuses_changed_upstream_code():
    with pytest.raises(ValueError, match="unreviewed revision"):
        runtime.port_source("modeling_unlimitedocr.py", "unreviewed GPU code")


def test_service_starts_without_weights_and_never_downloads_implicitly(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(
        runtime, "prepare", lambda root: pytest.fail("Implicit download")
    )
    with TestClient(service.app_factory()) as api:
        value = api.get("/health").json()
        assert value["device"] == "cpu" and value["experimental"]
        assert not value["installed"] and value["revision"] == runtime.REVISION
        data = {
            "image": base64.b64encode(b"not installed").decode(),
            "source_sha256": "a" * 64,
        }
        assert api.post("/ocr", json=data).status_code == 409
        assert api.get("/jobs/../../etc/passwd").status_code == 404


def test_service_rejects_image_binding_and_limits_before_starting_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "verify", lambda root: {})
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Invalid image dispatched"),
    )
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(image, format="PNG")
    with TestClient(service.app_factory()) as api:
        data = {
            "image": base64.b64encode(image.getvalue()).decode(),
            "source_sha256": "a" * 64,
        }
        assert api.post("/ocr", json=data).status_code == 400
        data["source_sha256"] = hashlib.sha256(image.getvalue()).hexdigest()
        assert api.post("/ocr", json={**data, "max_tokens": 999999}).status_code == 422


def test_cpu_ocr_is_a_pinned_extraction_model_not_an_ollama_approval_model():
    assert client.OPTION == "baidu/Unlimited-OCR-cpu"
    assert runtime.REVISION and len(runtime.REVISION) == 40
    assert set(runtime.HASHES) == {"modeling_unlimitedocr.py", "modeling_deepseekv2.py"}


def test_cpu_worker_dispatch_and_stop_are_isolated_and_cancellable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "ACTIVE", {})
    monkeypatch.setattr(runtime, "verify", lambda root: {})
    calls, killed = [], []

    class Process:
        pid, done = 987654321, False

        def poll(self):
            return 0 if self.done else None

        def wait(self, timeout):
            self.done = True
            return 0

    def start(args, **options):
        calls.append((args, options))
        return Process()

    monkeypatch.setattr(service.subprocess, "Popen", start)
    monkeypatch.setattr(
        service.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(image, format="PNG")
    with TestClient(service.app_factory()) as api:
        data = {
            "image": base64.b64encode(image.getvalue()).decode(),
            "source_sha256": hashlib.sha256(image.getvalue()).hexdigest(),
        }
        response = api.post("/ocr", json=data)
        assert response.status_code == 202
        job = response.json()["id"]
        assert calls[0][1]["start_new_session"] is True
        assert api.get("/jobs/" + job).json()["status"] == "running"
        assert api.post("/ocr", json=data).status_code == 409
        assert api.delete("/jobs/" + job).json()["status"] == "cancelled"
        assert killed == [(Process.pid, service.signal.SIGTERM)]
        assert api.get("/jobs/" + job).json()["status"] == "failed"
