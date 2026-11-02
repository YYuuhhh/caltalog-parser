"""Host review lifecycle checks; no model process is launched."""

from __future__ import annotations

import io
import json
from pathlib import Path
import signal
import sys
import time
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import review_worker_service as service
import subscription_review as review
import subscription_worker as worker


def test_host_service_follows_start_restart_stop_and_updated_project_path(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/host/bin/" + name)
    monkeypatch.setattr(
        service.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    monkeypatch.delenv("PIPELINE_UI_PORT", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "never-write-this-key")
    first = tmp_path / "original project"
    moved = tmp_path / "moved project"
    first.mkdir()
    moved.mkdir()
    (moved / ".env").write_text("PIPELINE_UI_PORT=8761\nOPENAI_API_KEY=also-secret\n")
    service.manage("start", first)
    service.manage("start", first)
    service.manage("restart", first)
    service.manage("stop", first)
    service.manage("start", moved)
    operations = [args[2] for args in calls if args[2] in ("start", "stop", "restart")]
    assert operations == ["restart", "start", "restart", "stop", "restart"]
    unit = (tmp_path / ".config/systemd/user" / service.SERVICE).read_text()
    assert str(moved) in unit and str(first) not in unit
    assert "WorkingDirectory=" + str(moved) + "\n" in unit
    assert "http://127.0.0.1:8761" in unit
    assert "--pipeline-url" in unit and "--queue" in unit
    assert "Restart=on-failure" in unit and "KillMode=control-group" in unit
    assert "WantedBy=default.target" in unit
    assert "OPENAI_API_KEY" not in unit and "secret" not in unit


def test_unit_paths_quote_systemd_expansion_and_reject_newlines():
    assert service.unit_quote('/project "quoted"/%path/$name', command=True) == (
        '"/project \\"quoted\\"/%%path/$$name"'
    )
    with pytest.raises(ValueError, match="newlines"):
        service.unit_quote("/project\nExecStart=unexpected")


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_service_rejects_invalid_workbench_ports(tmp_path, monkeypatch, port):
    monkeypatch.setenv("PIPELINE_UI_PORT", port)
    with pytest.raises(ValueError):
        service.pipeline_url(tmp_path)


@pytest.mark.parametrize(
    "body,ready",
    [
        (b'{"status":"healthy"}', True),
        (b'{"status":"starting"}', False),
        (b"[]", False),
        (b"not JSON", False),
    ],
)
def test_worker_only_accepts_healthy_main_pipeline(monkeypatch, body, ready):
    seen = []

    def response(url, timeout):
        seen.append((url, timeout))
        return io.BytesIO(body)

    monkeypatch.setattr(worker, "urlopen", response)
    assert worker.pipeline_available("http://127.0.0.1:8761/") is ready
    assert seen == [("http://127.0.0.1:8761/health", 2)]


def test_worker_treats_connection_failure_as_sleeping(monkeypatch):
    def offline(*args, **kwargs):
        raise URLError("not running")

    monkeypatch.setattr(worker, "urlopen", offline)
    assert not worker.pipeline_available("http://127.0.0.1:7861")
    assert worker.pipeline_available(None)  # Standalone mode remains supported.


def stub_host(monkeypatch, tmp_path):
    monkeypatch.setattr(worker.shutil, "which", lambda _: "/mock/codex")
    monkeypatch.setattr(worker, "auth_status", lambda _: "chatgpt")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_worker_wakes_after_pipeline_starts_and_skips_tasks_while_offline(
    tmp_path, monkeypatch
):
    stub_host(monkeypatch, tmp_path)
    queue = tmp_path / "queue"
    folder = queue / ("f" * 32)
    folder.mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps({"task_id": "invalid-task"}))
    states = iter([False, True, True])
    monkeypatch.setattr(worker, "pipeline_available", lambda _: next(states))

    def no_model(*args, **kwargs):
        pytest.fail("Lifecycle checks must not launch model processes.")

    monkeypatch.setattr(worker.subprocess, "Popen", no_model)
    pauses = []

    class Finished(Exception):
        pass

    def poll_again(seconds):
        pauses.append(seconds)
        heartbeat = json.loads((queue / "worker.json").read_text())
        if len(pauses) == 1:
            assert not heartbeat["pipeline_available"]
            assert not (folder / "error.json").exists()
            assert not review.capability(queue)["available"]
        else:
            assert heartbeat["pipeline_available"]
            assert "Invalid task identity" in (folder / "error.json").read_text()
            raise Finished

    monkeypatch.setattr(worker.time, "sleep", poll_again)
    with pytest.raises(Finished):
        worker.run(queue, pipeline_url="http://workbench")
    assert pauses == [2, 2]
    assert not (folder / "execution.json").exists()


def test_pipeline_shutdown_terminates_review_process_group_without_leaking_child(
    tmp_path, monkeypatch
):
    stub_host(monkeypatch, tmp_path)
    queue = tmp_path / "queue"
    request = review.create_task(
        queue, "correctness", {"page": 1}, [], model="gpt-6-sol"
    )
    folder = queue / request["task_id"]
    states = iter([True, True, False])
    monkeypatch.setattr(worker, "pipeline_available", lambda _: next(states))
    monkeypatch.setattr(worker, "validate_choice", lambda *args: None)

    class Process:
        pid = 123456
        stdin = io.StringIO()

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 5

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: Process())
    killed = []
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    worker.run(queue, once=True, pipeline_url="http://workbench")
    assert killed == [(123456, signal.SIGTERM)]
    assert "main pipeline went offline" in (folder / "error.json").read_text()
    assert not (folder / "result.json").exists()
    assert not review.capability(queue)["available"]


def test_waiting_host_service_is_not_reported_as_an_available_reviewer(tmp_path):
    review.atomic_json(
        tmp_path / "worker.json",
        {
            "heartbeat": time.time(),
            "auth": "chatgpt",
            "pipeline_available": False,
        },
    )
    assert not review.capability(tmp_path)["available"]
