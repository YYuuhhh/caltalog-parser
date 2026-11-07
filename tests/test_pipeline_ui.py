"""Workbench contract: scoped manufacturer mappings, queue, review gates and IO."""

from __future__ import annotations
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_ui as ui
import manufacturer_library as ml
import subscription_review as sr
import subscription_worker as sw
import extraction_quality as eq
import catalog_registry as registry
import prepare_runtime


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ui.ROOTS}
    for name, path in roots.items():
        path.mkdir()
        if name in ("schemas", "mappings"):
            src = Path("/" + name) if Path("/" + name).is_dir() else ROOT / name
            for file in src.glob("*"):
                if file.is_file():
                    (path / file.name).write_bytes(file.read_bytes())
    source = roots["catalogs"] / "input/catalog.pdf"
    source.parent.mkdir()
    pdf = PdfWriter()
    for _ in range(3):
        pdf.add_blank_page(width=600, height=800)
    pdf.write(source)
    monkeypatch.setattr(ui, "ROOTS", roots)
    monkeypatch.setattr(ui, "jobs", ui.Jobs())
    return roots, source, TestClient(ui.app)


def config(source, **changes):
    # Regression jobs never dispatch subscription inference.
    return ui.Settings(
        source_pdf=str(source),
        review_provider=changes.pop("review_provider", "work"),
        final_provider=changes.pop("final_provider", "work"),
        model=changes.pop("model", "gpt-6.1-sol"),
        reasoning=changes.pop("reasoning", "medium"),
        final_model=changes.pop("final_model", "gpt-6.1-sol"),
        final_reasoning=changes.pop("final_reasoning", "medium"),
        automatic_review=changes.pop("automatic_review", False),
        **changes,
    )


@pytest.mark.parametrize("endpoint", ["/health", "/api/bootstrap", "/api/library"])
def test_unwritable_catalog_mount_reports_error_and_recovers(workspace, endpoint):
    roots, source, client = workspace
    original = source.read_bytes()
    mode = source.stat().st_mode
    roots["catalogs"].chmod(0o555)
    try:
        response = client.get(endpoint)
        assert response.status_code == 503, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert (
            "UID" in response.json()["detail"]
            and "init-storage" in response.json()["detail"]
        )
        prepare_runtime.prepare(roots, os.getuid(), os.getgid())
        recovered = client.get(endpoint)
        assert recovered.status_code == 200, recovered.text
        assert source.read_bytes() == original and source.stat().st_mode == mode
    finally:
        roots["catalogs"].chmod(0o755)


@pytest.mark.parametrize("root", ["logs", "parsed"])
def test_health_requires_writable_runtime_storage(workspace, root):
    roots, _, client = workspace
    roots[root].chmod(0o555)
    try:
        response = client.get("/health")
        assert (
            response.status_code == 503
            and str(roots[root]) in response.json()["detail"]
        )
        prepare_runtime.prepare(roots, os.getuid(), os.getgid())
        assert client.get("/health").json() == {"status": "healthy"}
    finally:
        roots[root].chmod(0o755)


def test_readonly_library_database_is_repaired_without_losing_records(workspace):
    roots, _, client = workspace
    manufacturer = ui.library().add_manufacturer("Saved manufacturer")
    db = roots["catalogs"] / "library/library.sqlite"
    db.chmod(0o444)
    try:
        response = client.get("/api/library")
        assert (
            response.status_code == 503
            and "readonly" in response.json()["detail"].lower()
        )
        prepare_runtime.prepare(roots, os.getuid(), os.getgid())
        recovered = client.get("/api/library")
        assert recovered.status_code == 200
        assert any(
            m["id"] == manufacturer["id"] for m in recovered.json()["manufacturers"]
        )
    finally:
        db.chmod(0o644)


def test_unexpected_bootstrap_failure_returns_json_without_exposing_traceback(
    workspace, monkeypatch
):
    def broken_library():
        raise RuntimeError("internal fixture failure")

    monkeypatch.setattr(ui, "library", broken_library)
    with TestClient(ui.app, raise_server_exceptions=False) as client:
        response = client.get("/api/bootstrap")
    assert response.status_code == 500
    assert "pipeline-ui logs" in response.json()["detail"]
    assert "internal fixture failure" not in response.text


def test_runtime_initializer_refuses_symlinked_storage_without_touching_target(
    workspace, tmp_path
):
    roots, _, _ = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "original.txt").write_text("original data")
    mode = outside.stat().st_mode
    (roots["catalogs"] / "library").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_runtime.prepare(roots, os.getuid(), os.getgid())
    assert (outside / "original.txt").read_text() == "original data"
    assert outside.stat().st_mode == mode


def task(tmp_path, stage="correctness", **options):
    image = tmp_path / "page.png"
    image.write_bytes(b"image-test")
    return sr.create_task(
        tmp_path / "tasks", stage, {"page": 1}, [image], model="gpt-6-sol", **options
    )


def result(request, **changes):
    value = {
        "task_id": request["task_id"],
        "context_sha256": request["context_sha256"],
        "status": "passed",
        "confidence": 0.99,
        "summary": "Verified with the source evidence.",
        "checks": [
            {
                "check": c,
                "status": "passed",
                "evidence": "Page 1 table row 2.",
                "source_url": "https://manufacturer.example/catalog/product",
            }
            for c in request["required_checks"]
        ],
        "findings": [],
        "symbols": [],
        "series": [],
    }
    return {**value, **changes}


def test_full_catalog_default_ignores_stale_page_fields_and_cpu_only(workspace):
    roots, source, client = workspace
    settings = config(source, first_page=2, last_page=2)
    plan = client.post("/api/plan", json=settings.model_dump())
    assert plan.status_code == 200, plan.text
    data = plan.json()
    assert (
        data["selected_pages"] == 3
        and data["paid"] is False
        and data["reasoning"] == "medium"
    )
    assert all(s["enabled"] for s in data["stages"])
    stages = {s["id"]: s for s in data["stages"]}
    command = stages["parse"]["commands"][0]
    assert command[command.index("--start-page") + 1] == "0"
    assert command[command.index("--end-page") + 1] == "2"
    assert command[command.index("--backend") + 1] == "pipeline"
    assert {
        "correctness",
        "completeness",
        "internet",
        "mapping",
        "final",
    } <= stages.keys()
    assert not (roots["logs"] / "ui-runs").exists()
    selected = client.post(
        "/api/plan",
        json=config(
            source, page_selection=True, first_page=2, last_page=2, tables=False
        ).model_dump(),
    ).json()
    assert selected["selected_pages"] == 1
    assert (
        "--no-tables"
        in next(s for s in selected["stages"] if s["id"] == "parse")["commands"][0]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"page_selection": True, "first_page": 2, "last_page": 1},
        {"model": "gpt-5.6-sol"},
        {"extraction_model": "gpu"},
        {"review_rows": False},
        {"review_mode": "dry_run"},
        {"reasoning": "ultra"},
        {"work_mode": False},
        {"model": "chatgpt"},
        {"final_model": "chatgpt"},
        {"confidence_threshold": float("inf")},
        {"review_batch_rows": 1000},
        {"mineru_url": "http://user:password@localhost:8000"},
    ],
)
def test_invalid_or_legacy_settings_cannot_disable_review(workspace, changes):
    _, source, client = workspace
    # JSON itself forbids nonfinite floats, so exercise that one via Pydantic.
    if changes.get("confidence_threshold") == float("inf"):
        with pytest.raises(ValueError):
            config(source, **changes)
        return
    assert (
        client.post(
            "/api/runs", json={**config(source).model_dump(), **changes}
        ).status_code
        == 422
    )
    assert ui.jobs.active is None


def test_file_bounds_origin_upload_and_no_credentials(workspace, monkeypatch):
    roots, source, client = workspace
    base = config(source).model_dump()
    assert (
        client.post(
            "/api/plan", json={**base, "page_selection": True, "last_page": 4}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/plan", json={**base, "source_pdf": "/etc/passwd"}).status_code
        == 400
    )
    escape = roots["catalogs"] / "escape.pdf"
    escape.symlink_to("/etc/passwd")
    assert (
        client.post("/api/plan", json={**base, "source_pdf": str(escape)}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/runs", json=base, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/api/runs/not-an-id").status_code == 404
    before = registry.digest(source)
    uploaded = client.post("/api/upload?name=catalog.pdf", content=source.read_bytes())
    assert uploaded.status_code == 201 and uploaded.json()["pages"] == 3
    assert registry.digest(source) == before
    files = set((roots["catalogs"] / "uploads").iterdir())
    assert (
        client.post("/api/upload?name=invalid.pdf", content=b"invalid").status_code
        == 400
    )
    assert files == set((roots["catalogs"] / "uploads").iterdir())
    assert (
        client.post("/api/upload?name=../escape.pdf", content=b"invalid").status_code
        == 400
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-example-key")
    assert "secret-example-key" not in client.get("/api/bootstrap").text
    assert "openai_key_configured" not in client.get("/api/bootstrap").json()


def test_manufacturer_scopes_target_and_persistent_mapping(workspace):
    roots, source, client = workspace
    directory = client.get("/api/library").json()
    assert len(directory["manufacturers"]) >= 50
    added = client.post("/api/manufacturers", json={"name": "Fixture Tools"}).json()
    other = client.post("/api/manufacturers", json={"name": "Other Tools"}).json()
    assert (
        client.post("/api/manufacturers", json={"name": " fixture   tools "}).json()[
            "id"
        ]
        == added["id"]
    )
    assert (
        client.put(
            "/api/library/target", json={"manufacturer_id": added["id"]}
        ).status_code
        == 200
    )
    assert client.get("/api/library").json()["target_manufacturer_id"] == added["id"]
    assert (
        client.put(
            "/api/library/target", json={"manufacturer_id": "missing"}
        ).status_code
        == 400
    )
    mapping = client.get(f"/api/manufacturers/{added['id']}/mapping").json()
    assert not mapping["rules"] and mapping["manufacturer"] == "Fixture Tools"
    mapping["mapping_version"] = "0.2.0"
    assert (
        client.put(
            f"/api/manufacturers/{other['id']}/mapping", json=mapping
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/manufacturers/{added['id']}/mapping", json=mapping
        ).status_code
        == 200
    )
    assert ui.library().mapping(added["id"])["mapping_version"] == "0.2.0"
    # Reinitializing the directory must not revert a saved Gühring profile.
    guhring = ui.library().mapping("guhring")
    guhring["mapping_version"] = "9.0.0"
    ui.library().save_mapping("guhring", guhring)
    assert ui.library().mapping("guhring")["mapping_version"] == "9.0.0"
    sha = registry.digest(source)
    lib = ui.library()
    a = lib.register(added["id"], source, sha, 3, "2026")
    b = lib.register(added["id"], source, sha, 3, "2026")
    c = lib.register(other["id"], source, sha, 3, "2026")
    assert a == b and a != c
    assert len(lib.catalogs()) == 2


@pytest.mark.parametrize(
    "old_url,old_tokens,threads",
    [
        ("", None, None),
        (None, "", ""),
        ("http://baidu-ocr:8095", 2048, 0),
        ("http://127.0.0.1:8095", 4096, 6),
    ],
)
def test_batch_accepts_legacy_settings_without_null_model_parameters(
    workspace, monkeypatch, old_url, old_tokens, threads
):
    _, source, client = workspace
    seen = []
    monkeypatch.setattr(
        ui.jobs,
        "start_many",
        lambda configs: seen.extend(configs) or [{"id": "a" * 32}],
    )
    body = config(source).model_dump()
    body.pop("baidu_url")
    body.pop("baidu_output_tokens")
    body.update(
        ocr_check_model="none",
        ocr_check_url=old_url,
        ocr_check_tokens=old_tokens,
        local_cpu_threads=threads,
    )
    response = client.post("/api/batches", json={"catalogs": [body]})
    assert response.status_code == 202, response.text
    saved = seen[0].model_dump()
    assert saved["baidu_url"] == (
        old_url or ui.Settings.model_fields["baidu_url"].default
    )
    assert saved["baidu_output_tokens"] == (
        old_tokens or ui.Settings.model_fields["baidu_output_tokens"].default
    )
    assert saved["local_cpu_threads"] == (
        ui.SYSTEM_DEFAULTS["local_cpu_threads"] if threads in (None, "") else threads
    )
    assert not {"ocr_check_model", "ocr_check_url", "ocr_check_tokens"} & saved.keys()
    assert (
        client.post(
            "/api/batches", json={"catalogs": [{**body, "unknown_option": True}]}
        ).status_code
        == 422
    )
    assert len(seen) == 1


def test_current_ocr_settings_take_precedence_and_bootstrap_is_not_cached(workspace):
    _, source, client = workspace
    settings = config(
        source,
        ocr_check_url="http://127.0.0.1:8095",
        ocr_check_tokens=4096,
        baidu_url="http://baidu-ocr:8095",
        baidu_output_tokens=2048,
    )
    assert (
        settings.baidu_url == "http://baidu-ocr:8095"
        and settings.baidu_output_tokens == 2048
    )
    assert client.get("/api/bootstrap").headers["cache-control"] == "no-store"


def test_serial_queue_batch_validation_cancel_and_restart(workspace, monkeypatch):
    _, source, client = workspace
    entered, release = threading.Event(), threading.Event()
    seen = []

    def execute(self, job, config, state):
        seen.append(job)
        entered.set()
        release.wait(5)
        state["status"] = "completed"
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", execute)
    bad = client.post(
        "/api/batches",
        json={
            "catalogs": [
                config(source).model_dump(),
                {**config(source).model_dump(), "manufacturer_id": "missing"},
            ]
        },
    )
    assert bad.status_code == 400 and not seen
    response = client.post(
        "/api/batches",
        json={"catalogs": [config(source).model_dump() for _ in range(3)]},
    )
    assert response.status_code == 202
    jobs = response.json()["runs"]
    assert entered.wait(2)
    assert len(seen) == 1 and len(ui.jobs.pending) == 2
    assert (
        client.post(f"/api/runs/{jobs[1]['id']}/cancel", json={}).json()["status"]
        == "cancelled"
    )
    assert client.get(f"/api/runs/{jobs[1]['id']}").json()["status"] == "cancelled"
    release.set()
    ui.jobs.worker.join(5)
    assert seen == [jobs[0]["id"], jobs[2]["id"]]
    state = ui.jobs.read(jobs[0]["id"])
    state["status"] = "running"
    ui.jobs.save(jobs[0]["id"], state)
    assert ui.Jobs().read(jobs[0]["id"])["status"] == "interrupted"


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for a processing checkpoint.")


@pytest.mark.parametrize("action", ["resume", "stop"])
@pytest.mark.parametrize("scope", ["queue", "catalog"])
def test_queue_pause_preserves_current_work_and_holds_waiting_catalogs(
    workspace, monkeypatch, action, scope
):
    _, source, client = workspace
    entered, finish_page = threading.Event(), threading.Event()
    seen = []

    def execute(self, job, config, state):
        seen.append(job)
        state["status"] = "running"
        state["stages"][1]["status"] = "running"
        self.save(job, state)
        entered.set()
        finish_page.wait(3)
        try:
            self.checkpoint(job)
            state["status"] = "completed"
        except InterruptedError:
            state["status"] = "cancelled"
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", execute)
    response = client.post(
        "/api/batches",
        json={"catalogs": [config(source).model_dump() for _ in range(2)]},
    )
    jobs = response.json()["runs"]
    assert entered.wait(2)
    worker = ui.jobs.worker
    try:
        paused = client.post(
            "/api/queue/pause"
            if scope == "queue"
            else f"/api/runs/{jobs[0]['id']}/pause",
            json={},
        ).json()
        assert paused["status"] == "pausing"
        assert client.get(f"/api/runs/{jobs[1]['id']}").json()["status"] == (
            "paused" if scope == "queue" else "queued"
        )
        finish_page.set()
        wait_until(lambda: (ui.jobs.folder(jobs[0]["id"]) / "paused.json").exists())
        assert client.get(f"/api/runs/{jobs[0]['id']}").json()["status"] == "paused"
        assert seen == [jobs[0]["id"]]
        path = (
            f"/api/queue/{action}"
            if scope == "queue"
            else f"/api/runs/{jobs[0]['id']}/{'resume' if action == 'resume' else 'cancel'}"
        )
        assert client.post(path, json={}).status_code in (200, 202)
        worker.join(3)
        assert not worker.is_alive()
        assert seen == (
            [j["id"] for j in jobs]
            if action == "resume" or scope == "catalog"
            else [jobs[0]["id"]]
        )
        assert ui.jobs.read(jobs[0]["id"])["status"] == (
            "completed" if action == "resume" else "cancelled"
        )
        assert ui.jobs.read(jobs[1]["id"])["status"] == (
            "cancelled" if action == "stop" and scope == "queue" else "completed"
        )
        assert client.get("/api/queue").json()["queued_runs"] == []
    finally:
        finish_page.set()
        ui.jobs.stop_queue()
        worker.join(3)


def test_individual_queued_pause_skips_start_and_survives_restart(
    workspace, monkeypatch
):
    _, source, client = workspace
    seen = []

    def execute(self, job, config, state):
        seen.append(job)
        state["status"] = "completed"
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", execute)
    client.post("/api/queue/pause", json={})
    response = client.post(
        "/api/batches",
        json={"catalogs": [config(source).model_dump() for _ in range(2)]},
    )
    first, second = response.json()["runs"]
    worker = ui.jobs.worker
    try:
        assert client.post(f"/api/runs/{first['id']}/pause", json={}).status_code == 200
        client.post("/api/queue/resume", json={})
        wait_until(lambda: ui.jobs.read(second["id"])["status"] == "completed")
        assert seen == [second["id"]]
        assert ui.Jobs().read(first["id"])["status"] == "paused"
        assert (
            client.post(f"/api/runs/{first['id']}/resume", json={}).status_code == 202
        )
        worker.join(3)
        assert seen == [second["id"], first["id"]]
    finally:
        ui.jobs.stop_queue()
        worker.join(3)


def test_stopping_paused_review_cancels_task_and_resumes_saved_run(
    workspace, monkeypatch
):
    roots, source, client = workspace
    entered = threading.Event()
    request = sr.create_task(
        ui.queue(),
        "correctness",
        {"page": 1},
        [source],
        model="gpt-6-sol",
        manual_handoff=True,
    )

    def execute(self, job, config, state):
        state.update(
            status="awaiting_review",
            review_tasks=[{"id": request["task_id"], "status": "waiting"}],
        )
        self.save(job, state)
        entered.set()
        try:
            sr.wait_result(
                ui.queue(),
                request,
                self.cancelled,
                30,
                checkpoint=lambda: self.checkpoint(job),
            )
        except InterruptedError:
            state["status"] = "cancelled"
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", execute)
    job = client.post("/api/runs", json=config(source).model_dump()).json()["id"]
    worker = ui.jobs.worker
    assert entered.wait(2)
    try:
        client.post(f"/api/runs/{job}/pause", json={})
        wait_until(lambda: (ui.jobs.folder(job) / "paused.json").exists())
        assert client.post(f"/api/runs/{job}/cancel", json={}).status_code == 200
        worker.join(3)
        assert not worker.is_alive() and ui.jobs.read(job)["status"] == "cancelled"
        assert (ui.queue() / request["task_id"] / "cancelled.json").is_file()
    finally:
        ui.jobs.stop_queue()
        worker.join(3)


def test_review_timeout_excludes_time_spent_paused(tmp_path, monkeypatch):
    request = task(tmp_path)
    folder = tmp_path / "tasks" / request["task_id"]
    ticks = iter([0, 0, 1.5])
    durations = iter([2, 0])

    class Cancellation:
        def is_set(self):
            return False

        def wait(self, delay):
            sr.atomic_json(folder / "result.json", result(request))

    monkeypatch.setattr(sr.time, "monotonic", lambda: next(ticks))
    assert (
        sr.wait_result(
            tmp_path / "tasks",
            request,
            Cancellation(),
            1,
            checkpoint=lambda: next(durations),
        )["status"]
        == "passed"
    )


def test_extraction_failure_reports_parser_reason_and_page(workspace):
    roots, _, _ = workspace
    job = "e" * 32
    folder = ui.jobs.folder(job)
    folder.mkdir(parents=True)
    reason = "HTTPError: 500 Server Error: ModuleNotFoundError: No module named 'six'"
    manifest = {"page_states": [{"status": "failed", "page_idx": 2, "reason": reason}]}
    (folder / "catalog.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(RuntimeError, match="PDF page 3.*No module named 'six'"):
        ui.jobs.command(job, "parse", 0, [sys.executable, "-c", "raise SystemExit(1)"])
    assert (folder / "parse-0.stdout").is_file()


@pytest.mark.parametrize(
    "diagnostic",
    [
        "not a mapping",
        {"page_states": [{"status": "failed", "reason": "missing page"}]},
        {
            "page_states": [
                {"status": "failed", "page_idx": "invalid", "reason": "bad page"}
            ]
        },
    ],
)
def test_malformed_failure_diagnostic_preserves_command_error(workspace, diagnostic):
    _, _, _ = workspace
    job = "f" * 32
    folder = ui.jobs.folder(job)
    folder.mkdir(parents=True)
    (folder / "catalog.yaml").write_text(yaml.safe_dump(diagnostic))
    with pytest.raises(RuntimeError, match="Extract failed \\(exit 1\\)"):
        ui.jobs.command(job, "parse", 0, [sys.executable, "-c", "raise SystemExit(1)"])


@pytest.mark.parametrize("action", ["failure", "catalog_stop", "queue_stop"])
def test_stoppage_keeps_stage_page_reason_and_bounded_diagnostics(
    workspace, monkeypatch, action
):
    _, source, client = workspace
    entered = threading.Event()
    secret = "stop-diagnostic-test-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def extract(self, job, config, state, output):
        folder = self.folder(job)
        manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
        manifest["page_states"][0].update(
            status="failed", reason="Earlier page failure."
        )
        manifest["page_states"][1]["status"] = "processing"
        registry.save_manifest(folder / "catalog.yaml", manifest)
        (folder / "parse-0.stdout").write_text(
            "Old output\n" * 2000 + f"Last page error: {secret}\n"
        )
        (folder / "run.log").write_text("Saved extraction log.\n")
        entered.set()
        if action == "failure":
            raise RuntimeError(f"OCR failed on PDF page 2: {secret}")
        assert self.cancelled.wait(3)
        self.checkpoint(job)

    monkeypatch.setattr(ui.Jobs, "extract", extract)
    job = client.post("/api/runs", json=config(source).model_dump()).json()["id"]
    worker = ui.jobs.worker
    assert entered.wait(2)
    if action != "failure":
        endpoint = (
            "/api/queue/stop" if action == "queue_stop" else f"/api/runs/{job}/cancel"
        )
        assert client.post(endpoint, json={}).status_code == 200
    if worker:
        worker.join(3)
        assert not worker.is_alive()
    state = client.get(f"/api/runs/{job}").json()
    assert state["status"] == ("failed" if action == "failure" else "cancelled")
    assert state["stoppage"]["stage"] == "parse" and state["stoppage"]["page"] == 2
    assert state["stoppage"]["occurred_at"]
    assert secret not in state["stoppage"]["reason"]
    if action == "queue_stop":
        assert "queue was stopped" in state["stoppage"]["reason"]
    if action == "catalog_stop":
        assert "catalog was stopped" in state["stoppage"]["reason"]
    details = client.get(f"/api/runs/{job}/stoppage").json()
    assert details["stoppage"] == state["stoppage"]
    assert details["log_url"].endswith("/download/run.log")
    assert "Last page error" in details["diagnostics"][0]["text"]
    assert len(details["diagnostics"][0]["text"]) <= 8192
    assert secret not in json.dumps(details) and "[redacted]" in json.dumps(details)


def test_stoppage_supports_legacy_stages_without_reading_unrelated_outputs(workspace):
    _, source, client = workspace
    job = "f" * 32
    ui.jobs.save(
        job,
        {
            "id": job,
            "status": "failed",
            "source_name": source.name,
            "config": config(source).model_dump(),
            "stages": [
                {"id": "legacy_ai", "title": "Legacy model review", "status": "failed"}
            ],
            "review_tasks": [],
            "error": "Recorded legacy failure.",
        },
    )
    details = client.get(f"/api/runs/{job}/stoppage").json()
    assert details["stoppage"]["stage"] == "legacy_ai"
    assert details["stoppage"]["stage_title"] == "Legacy model review"
    assert details["stoppage"]["reason"] == "Recorded legacy failure."
    assert details["diagnostics"] == []


def test_stoppage_shows_local_response_diagnostics_without_thinking_text(workspace):
    _, source, client = workspace
    job, task_id = "f" * 32, "e" * 32
    folder = ui.queue() / task_id
    folder.mkdir(parents=True)
    sr.atomic_json(
        folder / "error.json", {"error": "Local model returned an empty final answer."}
    )
    sr.atomic_json(
        folder / "response-diagnostics.json",
        {
            "content_chars": 0,
            "thinking_chars": 1234,
            "eval_count": 516,
        },
    )
    sr.atomic_json(folder / "ollama-http.json", {"body": "x" * 16000})
    sr.atomic_json(
        folder / "ollama-response.json",
        {
            "message": {"thinking": "PRIVATE_THINKING_FIXTURE"},
        },
    )
    ui.jobs.save(
        job,
        {
            "id": job,
            "status": "failed",
            "source_name": source.name,
            "config": config(source).model_dump(),
            "stages": [
                {"id": "correctness", "title": "Page correctness", "status": "failed"}
            ],
            "review_tasks": [
                {"id": task_id, "stage": "correctness", "page": 1, "status": "failed"}
            ],
            "error": "Page correctness failed (exit 1). See the run log.",
        },
    )
    details = client.get(f"/api/runs/{job}/stoppage").json()
    diagnostic = {d["label"]: d["text"] for d in details["diagnostics"]}
    assert "empty final answer" in diagnostic["Reviewer error"]
    assert '"eval_count": 516' in diagnostic["Local model response details"]
    assert len(diagnostic["Local server HTTP response"]) <= 8192
    assert "PRIVATE_THINKING_FIXTURE" not in json.dumps(details)


def test_library_includes_unparsed_files_and_records_actual_review_models(workspace):
    _, source, client = workspace
    initial = client.get("/api/library").json()["catalogs"]
    assert len(initial) == 1 and initial[0]["status"] == "loaded"
    assert initial[0]["technical"] is None
    catalog_id = ui.library().register(
        "guhring", source, registry.digest(source), 3, "2026"
    )
    job, task_id = "a" * 32, "b" * 32
    ui.library().status(catalog_id, "needs_review", job)
    folder = ui.queue() / task_id
    folder.mkdir(parents=True)
    sr.atomic_json(
        folder / "execution.json",
        {
            "model": "qwen3-vl:32b-thinking-bf16",
            "reasoning": "medium",
            "provider": "ollama_local",
        },
    )
    ui.jobs.save(
        job,
        {
            "id": job,
            "catalog_id": catalog_id,
            "created_at": "2026-11-06T13:00:00Z",
            "completed_at": "2026-11-06T14:00:00Z",
            "status": "quality_blocked",
            "config": config(source).model_dump(),
            "stages": [],
            "review_tasks": [
                {"id": task_id, "stage": "correctness", "status": "passed"}
            ],
        },
    )
    items = client.get("/api/library").json()["catalogs"]
    assert len(items) == 1
    technical = items[0]["technical"]
    assert technical["completed_at"] == "2026-11-06T14:00:00Z"
    assert technical["checks"][0]["model"] == "qwen3-vl:32b-thinking-bf16"
    assert len(items[0]["history"]) == 1
    assert client.get("/api/system").json()["hardware"]["memory_bytes"] > 0


def test_review_binding_required_checks_sources_and_confidence(tmp_path):
    request = task(tmp_path, "final", confidence_threshold=0.95)
    assert sr.validate_result(request, result(request))["status"] == "passed"
    for changes in [
        {"task_id": "other"},
        {"context_sha256": "wrong"},
        {"confidence": 0.8},
        {"checks": []},
        {"checks": result(request)["checks"] + [result(request)["checks"][0]]},
        {
            "findings": [
                {
                    "code": "missing",
                    "severity": "blocking",
                    "message": "Missing tool",
                    "reference": "page 1",
                    "suggested_value": "",
                }
            ]
        },
    ]:
        with pytest.raises((ValueError, Exception)):
            sr.validate_result(request, result(request, **changes))
    missing_web = result(request)
    missing_web["checks"][-1]["source_url"] = ""
    with pytest.raises(ValueError):
        sr.validate_result(request, missing_web)
    request["context"].update(extracted_product_rows=1, products=[])
    with pytest.raises(ValueError):
        sr.validate_result(request, result(request))


def test_fresh_context_worker_never_uses_platform_key(tmp_path, monkeypatch):
    first = task(tmp_path)
    second = task(tmp_path, "internet")
    assert first["task_id"] != second["task_id"]
    folder = tmp_path / "tasks" / first["task_id"]
    command = sw.command("/usr/bin/codex", folder, first)
    assert "--ephemeral" in command and "--ignore-user-config" in command
    assert (
        'forced_login_method="chatgpt"' in command
        and 'model_reasoning_effort="medium"' in command
    )
    assert (
        'web_search="disabled"' in command
        and "--sandbox" in command
        and "read-only" in command
    )
    assert "resume" not in command
    assert 'web_search="live"' in sw.command(
        "/usr/bin/codex", tmp_path / "tasks" / second["task_id"], second
    )
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-send")
    assert "OPENAI_API_KEY" not in sw.environment()
    with pytest.raises(ValueError):
        sr.create_task(
            tmp_path / "tasks",
            "correctness",
            {"large": "x" * 60001},
            [],
            model="gpt-6-sol",
        )
    prompt = (folder / "prompt.txt").read_text()
    assert first["context_sha256"] in prompt and second["task_id"] not in prompt


@pytest.mark.parametrize("reasoning", ["low", "medium", "high", "xhigh", "max"])
def test_review_packet_and_cli_select_the_same_reasoning(tmp_path, reasoning):
    request = task(tmp_path, reasoning=reasoning)
    folder = tmp_path / "tasks" / request["task_id"]
    assert request["reasoning"] == reasoning
    assert f"Reasoning effort is {reasoning}." in (folder / "prompt.txt").read_text()
    assert f'model_reasoning_effort="{reasoning}"' in sw.command(
        "codex", folder, request
    )
    request["reasoning"] = 'high"; invalid'
    with pytest.raises(ValueError, match="Unsupported reasoning"):
        sw.command("codex", folder, request)


def test_review_stages_dispatch_independent_models_and_reasoning(
    workspace, monkeypatch
):
    from test_catalog_contract import fixture
    import catalog_etl as etl

    roots, _, _ = workspace
    fixture_root = roots["catalogs"] / "fixture"
    fixture_root.mkdir()
    etl_config, manifest, mapping = fixture(fixture_root)
    etl.run_etl(etl_config)
    content = json.loads(
        (fixture_root / "extract/catalog_content_list.json").read_text()
    )
    for page in manifest["page_states"]:
        artifact = fixture_root / "extract" / str(page["page_idx"])
        (artifact / "files").mkdir(parents=True)
        (artifact / "files/catalog_content_list.json").write_text(
            json.dumps([b for b in content if b["page_idx"] == page["page_idx"]])
        )
        page["artifact_path"] = str(artifact / "manifest.json")
    mapping = ui.library().save_mapping("promatool", mapping)
    job = "b" * 32
    folder = ui.jobs.folder(job)
    folder.mkdir(parents=True)
    registry.save_manifest(folder / "catalog.yaml", manifest)
    (folder / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    (fixture_root / "master_raw.xlsx").write_bytes(etl_config.raw_path.read_bytes())
    settings = config(
        fixture_root / "catalog.pdf",
        manufacturer_id="promatool",
        model="gpt-6-sol",
        final_model="gpt-6-astra",
        reasoning="low",
        final_reasoning="max",
    )
    state = {
        "review_tasks": [],
        "stages": [
            {"id": stage, "status": "running"}
            for stage in ["correctness", "completeness", "internet", "mapping", "final"]
        ],
    }
    seen = []
    seen_progress = []
    source_commands = []

    def prepare_sources(job_id, stage, index, args):
        assert "manufacturer_sources.py" in " ".join(args)
        source_commands.append(stage)
        ui.official.prepare(
            settings.source_pdf,
            "promatool",
            "PROMATOOL",
            folder / "official-sources.json",
        )

    monkeypatch.setattr(ui.jobs, "command", prepare_sources)
    monkeypatch.setattr(
        ui.official,
        "fetch",
        lambda url, origins: {
            "url": url,
            "text": "PROMATOOL D-001 d1 1,25 mm coating TiAlN",
            "content_sha256": hashlib.sha256(b"official").hexdigest(),
            "content_type": "text/html",
            "body": b"official",
            "links": [],
        },
    )

    def simulated_result(queue, request, cancelled, timeout, checkpoint=None):
        seen.append(request)
        seen_progress.append(
            (
                request["stage"],
                next(s for s in state["stages"] if s["id"] == request["stage"])[
                    "progress"
                ]["percent"],
            )
        )
        response = result(request, status="unverified")
        for check in response["checks"]:
            check["status"] = "unverified"
        return response

    monkeypatch.setattr(sr, "wait_result", simulated_result)
    for stage in ["correctness", "completeness", "internet", "mapping", "final"]:
        assert not ui.jobs.review_stage(job, stage, settings, state, fixture_root)
    assert len(seen) == 10
    assert source_commands == ["correctness"]
    assert len({r["context"]["official_sources"]["snapshot_sha256"] for r in seen}) == 1
    assert 'web_search="disabled"' in sw.command("codex", folder, seen[-1])
    for stage in state["stages"]:
        assert [percent for name, percent in seen_progress if name == stage["id"]] == [
            0,
            50,
        ]
        assert stage["progress"]["percent"] == 99
    for request in seen:
        final = request["stage"] == "final"
        assert request["model"] == ("gpt-6-astra" if final else "gpt-6-sol")
        assert request["reasoning"] == ("max" if final else "low")
        assert request["manual_handoff"]
        assert request["context"]["source_sha256"] == manifest["documents"][0]["sha256"]
        sr.validate_evidence(request, ui.queue() / request["task_id"])
        if final:
            assert request["context"]["products"]


@pytest.mark.parametrize("scope", [False, True])
def test_live_extraction_progress_counts_saved_pages_and_quality_in_scope(
    workspace, scope
):
    _, _, client = workspace
    job = "c" * 32
    ui.jobs.active = job
    folder = ui.jobs.folder(job)
    folder.mkdir(parents=True)
    state = {
        "id": job,
        "status": "running",
        "config": {"page_selection": scope, "first_page": 2, "last_page": 3},
        "stages": [{"id": "parse", "title": "Extract", "status": "running"}],
        "review_tasks": [],
    }
    ui.jobs.save(job, state)
    manifest = {
        "page_states": [
            {"page_idx": 0, "status": "failed"},
            {"page_idx": 1, "status": "parsed"},
            {"page_idx": 2, "status": "processing"},
        ]
    }
    (folder / "catalog.yaml").write_text(yaml.safe_dump(manifest))
    live = client.get(f"/api/runs/{job}").json()["stages"][0]["progress"]
    assert live["percent"] == pytest.approx(40 if scope else 80 / 3)
    assert ("1/2 pages saved" if scope else "1/3 pages saved") in live["detail"]
    assert ("failed" in live["detail"]) == (not scope)
    assert (
        "progress" not in json.loads((folder / "state.json").read_text())["stages"][0]
    )
    for page in manifest["page_states"]:
        page["status"] = "parsed"
    (folder / "catalog.yaml").write_text(yaml.safe_dump(manifest))
    state["quality"] = [{"page": 2, "confidence": 0.9}]
    ui.jobs.save(job, state)
    checking = client.get(f"/api/runs/{job}").json()["stages"][0]["progress"]
    assert checking["percent"] == pytest.approx(90 if scope else 80 + 20 / 3)
    assert "Quality check" in checking["detail"]
    state["quality"] = [{"page": page + 1, "confidence": 0.9} for page in range(3)]
    ui.jobs.save(job, state)
    assert (
        client.get(f"/api/runs/{job}").json()["stages"][0]["progress"]["percent"] == 99
    )


def test_work_handoff_rejects_wrong_tasks_and_cancelled_results(workspace):
    roots, _, client = workspace
    image = roots["logs"] / "page.png"
    image.write_bytes(b"fixture")
    request = sr.create_task(
        ui.queue(),
        "correctness",
        {"page": 1},
        [image],
        model="gpt-6-sol",
        manual_handoff=True,
    )
    id = request["task_id"]
    assert client.get(f"/api/review/tasks/{id}").status_code == 200
    assert (
        client.post(
            f"/api/review/tasks/{id}/result", json=result(request, task_id="wrong")
        ).status_code
        == 400
    )
    assert (
        client.post(f"/api/review/tasks/{id}/result", json=result(request)).status_code
        == 200
    )
    execution = json.loads((ui.queue() / id / "execution.json").read_text())
    assert (
        execution["provider"] == "chatgpt_work_manual"
        and "cannot be authenticated" in execution["provenance"]
    )
    assert (
        client.post(f"/api/review/tasks/{id}/result", json=result(request)).status_code
        == 409
    )
    auto = sr.create_task(
        ui.queue(), "correctness", {"page": 2}, [image], model="gpt-6-sol"
    )
    assert (
        client.post(
            f"/api/review/tasks/{auto['task_id']}/result", json=result(auto)
        ).status_code
        == 409
    )


def test_quality_scores_ignore_layout_scores_and_do_not_select_lost_rows():
    content = [
        {
            "type": "table",
            "table_body": "<table><tr><td>diameter 12</td><td>length 30</td></tr></table>",
        }
    ]
    low = eq.measure(
        "diameter 12 length 30 flute count 4", content, {"type": "table", "score": 0.99}
    )
    assert low["ocr_mean_confidence"] is None and low["confidence"] < 0.9
    assert eq.retry_method("auto", low, 1) == "ocr"
    assert eq.retry_method("ocr", low, 1) == "txt"
    assert not eq.keep_candidate(low, {**low, "confidence": 0.99, "table_cells": 1})
    assert eq.keep_candidate(low, {**low, "confidence": 0.99})
    scanned = eq.measure("", content, {"spans": [{"type": "text", "score": 0.8}]})
    assert scanned["native_text_coverage"] is None and scanned["confidence"] == 0.8
    assert eq.retry_method("ocr", scanned, 1) is None


@pytest.mark.parametrize(
    "retry_scores,failed_attempt,expected_attempt,error_type",
    [
        ((0.5, 0.4), None, 0, None),
        ((0.7, 0.7), None, 1, None),
        ((0.7, 0.8), None, 2, None),
        ((0.95,), None, 1, None),
        ((0.7,), 1, 0, RuntimeError),
        ((0.7, 0.8), 2, 1, InterruptedError),
        ((0.7, 0.8), 2, 1, ValueError),
    ],
)
def test_extraction_retry_preserves_selected_artifacts_and_other_pages(
    workspace, monkeypatch, retry_scores, failed_attempt, expected_attempt, error_type
):
    import fitz

    roots, source, _ = workspace
    with fitz.open(source) as pdf:
        for page in pdf:
            page.insert_text((50, 50), "diameter 12 length 30")
        pdf.saveIncr()
    job = "e" * 32
    folder = ui.jobs.folder(job)
    manifest_path = folder / "catalog.yaml"
    manifest = registry.register(
        source,
        catalog_id="retry-test",
        manufacturer="GUHRING",
        role="TARGET",
        revision="test",
        mapping_profile="guhring",
        families=["milling_cutters"],
        languages=["en"],
    )
    registry.save_manifest(manifest_path, manifest)
    page_sha = manifest["documents"][0]["sha256"]
    extracted, methods = {}, []

    def artifact(page_idx, attempt, confidence, malformed=False):
        path = roots["parsed"] / f"page-{page_idx}-attempt-{attempt}" / "response.zip"
        files = path.parent / "files"
        files.mkdir(parents=True)
        path.write_bytes(f"immutable page {page_idx} attempt {attempt}".encode())
        if not malformed:
            (files / "page_content_list.json").write_text(
                json.dumps([{"type": "text", "text": "diameter 12 length 30"}])
            )
        (files / "page_middle.json").write_text(
            json.dumps({"spans": [{"type": "text", "score": confidence}]})
        )
        state = {
            "source_sha256": page_sha,
            "page_idx": page_idx,
            "status": "parsed",
            "cache_key": f"{page_idx * 10 + attempt:064x}",
            "artifact_path": str(path),
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        extracted[page_idx, attempt] = state
        return state

    def command(job, stage, attempt, argv):
        saved = yaml.safe_load(manifest_path.read_text())
        methods.append(argv[argv.index("--method") + 1])
        if not attempt:
            saved["page_states"] = [
                artifact(idx, 0, 0.5 if idx == 0 else 0.99) for idx in range(3)
            ]
        else:
            # The parser must see only the retried page as incomplete.
            assert "artifact_path" not in saved["page_states"][0]
            assert saved["page_states"][1:] == [
                extracted[1, 0],
                extracted[2, 0],
            ]
            if attempt == failed_attempt and error_type is not ValueError:
                saved["page_states"][0] = {
                    "source_sha256": page_sha,
                    "page_idx": 0,
                    "status": "failed",
                    "reason": "Retry did not finish.",
                }
                saved["parse_selection"] = saved["parse_selection"][1:]
                registry.save_manifest(manifest_path, saved)
                raise error_type("Retry did not finish.")
            saved["page_states"][0] = artifact(
                0,
                attempt,
                retry_scores[attempt - 1],
                malformed=attempt == failed_attempt,
            )
        saved["parse_selection"] = [
            {k: p[k] for k in ("source_sha256", "page_idx", "cache_key")}
            for p in saved["page_states"]
        ]
        registry.save_manifest(manifest_path, saved)

    class Health:
        def raise_for_status(self):
            pass

        def json(self):
            return {"device": "cpu", "model_revision": "mineru-3.4.5-pipeline"}

    monkeypatch.setattr(ui.requests, "get", lambda *args, **kwargs: Health())
    monkeypatch.setattr(ui.jobs, "command", command)
    state = {"id": job}
    settings = config(source, max_quality_passes=3)
    output = roots["parsed"] / "ui-runs" / job
    if error_type:
        with pytest.raises(error_type):
            ui.jobs.extract(job, settings, state, output)
    else:
        ui.jobs.extract(job, settings, state, output)
        assert state["pages"] == {"total": 3, "parsed": 3, "failed": 0}
        assert (
            state["quality"][0]["selected_cache_key"]
            == extracted[0, expected_attempt]["cache_key"]
        )
    saved = yaml.safe_load(manifest_path.read_text())
    assert saved["page_states"] == [
        extracted[0, expected_attempt],
        extracted[1, 0],
        extracted[2, 0],
    ]
    for page, selection in zip(saved["page_states"], saved["parse_selection"]):
        assert page["cache_key"] == selection["cache_key"]
        assert (
            hashlib.sha256(Path(page["artifact_path"]).read_bytes()).hexdigest()
            == page["artifact_sha256"]
        )
        assert eq.artifacts(page)[0]
    assert methods[:2] == ["auto", "ocr"]
    if len(methods) == 3:
        assert methods[2] == "txt"
    assert not ui.queue().exists()  # Extraction tests never dispatch Work inference.


def test_missing_artifact_reference_reports_the_pdf_page():
    with pytest.raises(ValueError, match="PDF page 1 has no saved extraction artifact"):
        eq.artifacts({"page_idx": 0, "status": "parsed"})


def test_duplicate_identity_values_units_tolerances_and_missing_values(workspace):
    _, _, _ = workspace
    p = {
        "identity_status": "identified",
        "family": "drills",
        "subfamily": None,
        "item_type": "sku",
    }
    a = {
        "canonical_field": "diameter",
        "canonical_unit": "mm",
        "value_decimal": "1.250",
        "value_text": None,
        "fit_class": "h6",
    }
    signature = ml.parameter_signature(p, [a])
    assert signature == ml.parameter_signature(p, [{**a, "value_decimal": "1.25"}])
    for change in [
        {"value_decimal": "1.26"},
        {"canonical_unit": "cm"},
        {"fit_class": "h5"},
        {"tolerance_upper": ".01"},
    ]:
        assert signature != ml.parameter_signature(p, [{**a, **change}])
    assert ml.parameter_signature(p, []) is None
    assert ml.parameter_signature(p, [{**a, "value_decimal": None}]) is None
    assert ml.parameter_signature({**p, "identity_status": "provisional"}, [a]) is None


def test_mapping_proposals_stay_scoped_and_keep_evidence(workspace, tmp_path):
    _, _, _ = workspace
    mapping = ui.library().mapping("sowin")
    request = task(tmp_path, "mapping")
    response = result(
        request,
        status="issues",
        symbols=[
            {
                "symbol": "D",
                "canonical_field": "cutting_diameter",
                "description": "Diameter of cutting edges measured perpendicular to the tool axis.",
                "unit": "mm",
                "family": "milling_cutters",
                "series": ["S100"],
                "data_type": "decimal",
                "evidence": "Page 1 dimension line D.",
                "source_url": "https://manufacturer.example/product",
            }
        ],
        series=[
            {
                "article_number": "S100",
                "family": "milling_cutters",
                "item_type": "sku",
                "series_name": "End mill",
                "evidence": "Page 1 heading.",
            }
        ],
    )
    merged = sr.merge_mapping(mapping, response, "sha:page:1")
    registry.validate_manifest(
        merged, ui.ROOTS["schemas"] / "field-mapping.schema.json"
    )
    assert merged["manufacturer"] == "SOWIN" and merged["rules"][0]["series"] == [
        "S100"
    ]
    assert merged["symbol_dictionary"][0]["status"] == "proposed"
    assert merged["rules"][0]["review_required"]
    assert not mapping["rules"]
    again = sr.merge_mapping(merged, response, "sha:page:1")
    assert len(again["rules"]) == 1 and len(again["symbol_dictionary"]) == 1


def test_bounded_review_chunks_preserve_all_cells_and_reduce_context():
    records = [
        {
            "raw_parameter_name": "d1",
            "raw_value": str(i),
            "table_index": 0,
            "row_index": i,
            "column_index": 1,
            "artifact_path": "x" * 2000,
            "source_file": "y" * 1000,
        }
        for i in range(300)
    ]
    chunks = ui.bounded_chunks(records, 60, budget=6000)
    assert [r["raw_value"] for chunk in chunks for r in chunk] == [
        str(i) for i in range(300)
    ]
    assert all(
        len(chunk) <= 60 and len(json.dumps([ui.review_cell(r) for r in chunk])) < 6500
        for chunk in chunks
    )
    assert "artifact_path" not in ui.review_cell(records[0])


def test_raw_workbook_first_sheet_and_overflow_are_both_read(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    first = workbook.active
    first.title = "raw"
    first.append(["raw_parameter_name", "raw_value"])
    first.append(["d1", "1.25"])
    second = workbook.create_sheet("raw_002")
    second.append(["raw_parameter_name", "raw_value"])
    second.append(["d1", "2.50"])
    workbook.create_sheet("_manifest").append(["key", "value"])
    path = tmp_path / "raw.xlsx"
    workbook.save(path)
    assert [r["raw_value"] for r in ui.workbook_rows(path, "raw")] == ["1.25", "2.50"]


def test_review_binds_page_image_and_context(tmp_path):
    request = task(tmp_path)
    folder = tmp_path / "tasks" / request["task_id"]
    sr.validate_evidence(request, folder)
    (folder / "evidence-0.png").write_bytes(b"changed")
    with pytest.raises(ValueError):
        sr.validate_evidence(request, folder)


def test_new_series_symbol_meaning_does_not_overlap_legacy_general_rule(
    workspace, tmp_path
):
    _, _, _ = workspace
    mapping = ui.library().mapping("guhring")
    request = task(tmp_path, "mapping")
    response = result(
        request,
        status="issues",
        symbols=[
            {
                "symbol": "d1",
                "canonical_field": "cutting_diameter",
                "description": "New-series cutting diameter.",
                "unit": "mm",
                "family": "milling_cutters",
                "series": ["S100"],
                "data_type": "decimal",
                "evidence": "Page 1 drawing D.",
                "source_url": "https://manufacturer.example/product",
            }
        ],
        series=[
            {
                "article_number": "S100",
                "family": "milling_cutters",
                "item_type": "sku",
                "series_name": "End mill",
                "evidence": "Page 1 heading.",
            }
        ],
    )
    merged = sr.merge_mapping(mapping, response, "sha:page:1")
    old = next(r for r in merged["rules"] if r["id"] == "guhring-d1")
    new = next(r for r in merged["rules"] if r["canonical_field"] == "cutting_diameter")
    assert "S100" not in old["series"] and new["series"] == ["S100"]
    assert "3893" in old["series"]


@pytest.mark.parametrize(
    "evidence_model,final_model",
    [("gpt-6.1-sol", "gpt-6-astra"), ("gpt-6.1-sol", "none"), ("none", "none")],
)
def test_all_required_stages_continue_after_review_findings_with_real_etl(
    workspace, monkeypatch, evidence_model, final_model
):
    from test_catalog_contract import fixture

    roots, _, client = workspace
    fixture_root = roots["catalogs"] / "input/fixture"
    fixture_root.mkdir()
    etl_config, manifest, mapping = fixture(fixture_root)
    lib = ui.library()
    lib.save_mapping("promatool", mapping)
    source = fixture_root / "catalog.pdf"
    original_raw = (
        etl_config.raw_path.read_bytes()
        if hasattr(etl_config, "raw_path")
        else (fixture_root / "raw.xlsx").read_bytes()
    )
    seen = []

    def extract(self, job, config, state, output):
        registered = yaml.safe_load((self.folder(job) / "catalog.yaml").read_text())
        registered.update(
            page_states=manifest["page_states"],
            parse_selection=manifest["parse_selection"],
            status="parsed",
        )
        registry.save_manifest(self.folder(job) / "catalog.yaml", registered)
        state["pages"] = {"total": 2, "parsed": 2, "failed": 0}

    def review_stage(self, job, stage, config, state, output):
        seen.append(stage)
        return (
            stage != "correctness" or final_model == "none"
        )  # A blocking model finding must not skip final review.

    original_command = ui.Jobs.command

    def command(self, job, stage, number, args):
        if stage == "raw":
            output = roots["parsed"] / "ui-runs" / job / "master_raw.xlsx"
            output.write_bytes(original_raw)
        elif stage == "validate" and number == 0:
            pass  # No immutable ZIP is generated by this fixture; ETL reopens all books below.
        else:
            original_command(self, job, stage, number, args)

    monkeypatch.setattr(ui.Jobs, "extract", extract)
    monkeypatch.setattr(ui.Jobs, "review_stage", review_stage)
    monkeypatch.setattr(ui.Jobs, "command", command)
    response = client.post(
        "/api/runs",
        json=config(
            source,
            manufacturer_id="promatool",
            families="drills",
            languages="ru",
            model=evidence_model,
            final_model=final_model,
            prepare_manual_review=True,
        ).model_dump(),
    )
    assert response.status_code == 202, response.text
    job = response.json()["id"]
    for _ in range(200):
        state = client.get(f"/api/runs/{job}").json()
        if state["status"] in ("quality_blocked", "failed", "completed"):
            break
        time.sleep(0.05)
    assert state["status"] == "quality_blocked", state
    skipped = {"final"} if final_model == "none" else set()
    if evidence_model == "none":
        skipped |= {"correctness", "completeness", "internet", "mapping"}
    assert all(
        s["status"] == ("skipped" if s["id"] in skipped else "completed")
        for s in state["stages"]
    )
    assert all(
        s["quality_blocked"] and "unvalidated" in s["reason"]
        for s in state["stages"]
        if s["id"] in skipped
    )
    assert seen == (
        []
        if evidence_model == "none"
        else ["correctness", "completeness", "internet", "mapping"]
    ) + (["final"] if final_model != "none" else [])
    if evidence_model == final_model == "none":
        assert state["review_tasks"] == []
        assert not ui.queue().exists()
    assert len([f for f in state["files"] if f["filename"].endswith(".xlsx")]) == 5
    assert ui.library().catalogs()[0]["status"] == "needs_review"
    assert len(ui.library().duplicates()) == 0
    assert (fixture_root / "raw.xlsx").read_bytes() == original_raw
    packets = client.get(f"/api/runs/{job}/manual-review").json()["tools"]
    assert packets and packets[0]["parameters"] and packets[0]["pages"]
    assert not packets[0]["internet_sources"]
    assert (
        client.get(f"/api/runs/{job}/source-page/{packets[0]['pages'][0]}").status_code
        == 200
    )
    from openpyxl import load_workbook
    import io

    export = client.get("/api/manufacturers/promatool/workbook")
    book = load_workbook(io.BytesIO(export.content), read_only=True)
    assert (
        book.sheetnames == ["products", "dictionary"] and book["products"].max_row > 1
    )
    book.close()


def test_named_series_resolve_to_manufacturer_article_codes(workspace, tmp_path):
    _, _, _ = workspace
    mapping = ui.library().mapping("sowin")
    request = task(tmp_path, "mapping")
    response = result(
        request,
        status="issues",
        symbols=[
            {
                "symbol": "D",
                "canonical_field": "cutting_diameter",
                "description": "Cutting-edge diameter.",
                "unit": "mm",
                "family": "milling_cutters",
                "series": ["RF 100 Ti"],
                "data_type": "decimal",
                "evidence": "Page 1 drawing D.",
                "source_url": "https://manufacturer.example/product",
            }
        ],
        series=[
            {
                "article_number": "S100",
                "family": "milling_cutters",
                "item_type": "sku",
                "series_name": "Ratio end mills RF 100 Ti",
                "evidence": "Page 1 heading.",
            }
        ],
    )
    merged = sr.merge_mapping(mapping, response, "sha:page:1")
    assert merged["rules"][0]["series"] == ["S100"]
    response["symbols"][0]["series"] = ["Unknown series"]
    unknown = sr.merge_mapping(mapping, response, "sha:page:1")
    assert (
        not unknown["rules"] and unknown["symbol_dictionary"][0]["status"] == "proposed"
    )


def test_final_duplicate_context_contains_values_not_just_names():
    left = {
        "product_version_id": "one",
        "manufacturer": "A",
        "order_number": "N-1",
        "identity_status": "identified",
        "attributes": [
            {
                "canonical_field": "diameter",
                "value_decimal": "12",
                "canonical_unit": "mm",
                "fit_class": "h6",
            }
        ],
    }
    right = {
        **left,
        "product_version_id": "two",
        "attributes": [{**left["attributes"][0], "value_decimal": "14"}],
    }
    context = ui.duplicate_context(
        {
            "kind": "same_name_conflicting_or_incomplete_parameters",
            "left": {"catalog_id": "a", "product": left},
            "right": {"catalog_id": "b", "product": right},
        }
    )
    assert context["left"]["parameters"][0]["value_decimal"] == "12"
    assert context["right"]["parameters"][0]["value_decimal"] == "14"


@pytest.mark.parametrize("legacy_chat", [False, True])
def test_resume_keeps_completed_stage_evidence_and_rechecks_original_pdf(
    workspace, monkeypatch, legacy_chat
):
    _, source, client = workspace
    ran = []

    def fail(self, job, config, state):
        state["stages"][0]["status"] = "completed"
        state["stages"][1]["status"] = "failed"
        state.update(status="failed", error="fixture failure")
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", fail)
    first = client.post(
        "/api/runs",
        json=config(source, reasoning="low", final_reasoning="max").model_dump(),
    ).json()
    job = first["id"]
    worker = ui.jobs.worker
    if worker:
        worker.join(2)
    if legacy_chat:
        state = ui.jobs.read(job)
        state["config"].update(model="chatgpt", final_model="chatgpt", work_mode=False)
        state["config"].pop("reasoning")
        state["config"].pop("final_reasoning")
        ui.jobs.save(job, state)

    def continue_run(self, job, config, state):
        ran.append([s["status"] for s in state["stages"]])
        state["status"] = "completed"
        self.save(job, state)

    monkeypatch.setattr(ui.Jobs, "execute", continue_run)
    response = client.post(f"/api/runs/{job}/resume", json={})
    assert response.status_code == 202, response.text
    worker = ui.jobs.worker
    if worker:
        worker.join(2)
    assert ran[0][0] == "completed" and ran[0][1] == "queued"
    resumed = ui.jobs.read(job)["config"]
    assert resumed["model"] == resumed["final_model"] == "gpt-6.1-sol"
    assert "work_mode" not in resumed
    assert resumed["reasoning"] == ("medium" if legacy_chat else "low")
    assert resumed["final_reasoning"] == ("medium" if legacy_chat else "max")
    assert "error" not in ui.jobs.read(job)
    assert client.post(f"/api/runs/{job}/resume", json={}).status_code == 409


def test_manufacturer_mapping_profiles_cannot_be_shared(workspace):
    _, _, _ = workspace
    mapping = ui.library().mapping("sowin")
    mapping["mapping_profile"] = "guhring"
    with pytest.raises(ValueError):
        ui.library().save_mapping("sowin", mapping)


def test_invalid_series_mapping_is_rejected_before_processing(workspace):
    _, _, _ = workspace
    mapping = ui.library().mapping("guhring")
    mapping["rules"][0]["series"] = ["Not a known article number"]
    with pytest.raises(ValueError):
        ui.library().save_mapping("guhring", mapping)


def test_review_nan_cannot_bypass_confidence_gate(tmp_path):
    request = task(tmp_path)
    with pytest.raises(ValueError):
        sr.validate_result(request, result(request, confidence=float("nan")))


def test_uppercase_pdf_upload_survives_bootstrap(workspace):
    _, source, client = workspace
    uploaded = client.post(
        "/api/upload?name=catalog.PDF", content=source.read_bytes()
    ).json()
    assert uploaded["path"] in {
        c["path"] for c in client.get("/api/bootstrap").json()["catalogs"]
    }


def test_duplicate_parameters_do_not_include_product_names_or_order_identifiers():
    product = {
        "identity_status": "identified",
        "family": "drills",
        "subfamily": None,
        "item_type": "sku",
    }
    dimension = {
        "canonical_field": "diameter",
        "canonical_unit": "mm",
        "value_decimal": "12",
    }
    identifier = {
        "canonical_field": "order_number",
        "value_type": "identifier",
        "value_text": "A-12",
    }
    assert ml.parameter_signature(
        product, [dimension, identifier]
    ) == ml.parameter_signature(
        product, [dimension, {**identifier, "value_text": "B-12"}]
    )
    assert ml.parameter_signature(product, [identifier]) is None


def test_native_upload_detects_manufacturer_and_languages(workspace):
    import fitz

    roots, _, client = workspace
    pdf = fitz.open()
    english = "GUEHRING\nMilling cutters for high quality machining. The tools are designed for use in a wide range of applications. Read the complete technical catalog before selecting the tool."
    german = "GUEHRING\nDieser Katalog beschreibt die Werkzeuge für die Bearbeitung von Metall. Die technischen Daten und Abmessungen sind für die Auswahl der richtigen Werkzeuge erforderlich."
    for text in [english, german]:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 700), text, fontsize=17)
    content = pdf.tobytes()
    pdf.close()
    response = client.post("/api/upload?name=unknown-brand.pdf", content=content)
    assert response.status_code == 201, response.text
    detected = response.json()["detection"]
    assert detected["manufacturer"]["id"] == "guhring"
    assert {"en", "de"} <= set(detected["language"]["codes"])
    assert detected["ocr_pages"] == []
    assert response.json()["path"].startswith(str(roots["catalogs"]))


def test_real_fast_ocr_on_image_only_upload(workspace):
    import fitz

    _, _, client = workspace
    native = fitz.open()
    page = native.new_page(width=650, height=850)
    page.insert_text((50, 80), "GUEHRING", fontsize=38)
    page.insert_textbox(
        fitz.Rect(50, 130, 600, 760),
        "This catalog contains milling cutters for metal machining.\nThe tools are designed for reliable industrial applications.\nRead the product specifications and select the correct tool.\nDimensions and cutting parameters are listed in the tables.",
        fontsize=20,
    )
    raster = page.get_pixmap(dpi=160)
    scan = fitz.open()
    scan.new_page(width=650, height=850).insert_image(
        fitz.Rect(0, 0, 650, 850), stream=raster.tobytes("png")
    )
    content = scan.tobytes()
    native.close()
    scan.close()
    response = client.post("/api/upload?name=scan.pdf", content=content)
    assert response.status_code == 201, response.text
    detected = response.json()["detection"]
    assert detected["manufacturer"]["id"] == "guhring"
    assert "en" in detected["language"]["codes"]
    assert detected["ocr_pages"] == [1] and "OCR" in detected["method"]
    assert detected["elapsed_seconds"] < 20


def test_ambiguous_or_filename_only_manufacturer_is_not_assigned():
    import catalog_detection as cd

    manufacturers = [
        {"id": "guhring", "name": "Gühring"},
        {"id": "walter", "name": "Walter"},
    ]
    assert cd.identify(["GUEHRING tools and WALTER tools"], manufacturers)["id"] is None
    found = cd.identify(
        ["No visible manufacturer"], manufacturers, "guhring-catalog.pdf"
    )
    assert found["id"] is None and found["candidates"][0]["id"] == "guhring"
    assert cd.languages(["12345 d1 d2"])["codes"] == []


def test_confidence_aggregates_page_minima_and_reports_incomplete_scope():
    import workbench_outputs as wo

    state = {
        "quality": [{"page": 2, "confidence": 0.96}, {"page": 3, "confidence": 0.8}],
        "pages": {"total": 10},
        "review_tasks": [
            {"page": 2, "confidence": 0.7},
            {"page": 2, "confidence": 0.9},
            {"page": 3, "confidence": 0.95},
        ],
    }
    score = wo.confidence(state)
    assert (
        score["average"] == 0.75 and score["worst"] == 0.7 and score["worst_page"] == 2
    )
    assert score["assessed_pages"] == 2 and score["coverage"] == 0.2
    assert wo.confidence({})["average"] is None


@pytest.mark.parametrize(
    "review_provider,model,final_provider,final_model,expected",
    [
        (
            "local",
            "qwen3-vl:8b-instruct",
            "local",
            "qwen3.5:9b",
            "qwen3-vl:8b-instruct",
        ),
        ("work", "gpt-6.1-sol", "local", "qwen3.5:9b", "qwen3.5:9b"),
        ("work", "gpt-6.1-sol", "work", "gpt-6.1-sol", None),
        ("local", "none", "local", "none", None),
    ],
)
def test_first_local_model_preload_starts_before_extraction(
    workspace,
    monkeypatch,
    review_provider,
    model,
    final_provider,
    final_model,
    expected,
):
    _, source, _ = workspace
    events = []
    monkeypatch.setattr(
        ui.Jobs,
        "start_model_load",
        lambda self, job, settings, model: events.append(("load", model)),
    )

    def extract(self, job, settings, state, output):
        events.append(("extract", None))
        raise InterruptedError("End preload ordering test")

    monkeypatch.setattr(ui.Jobs, "extract", extract)
    settings = config(
        source,
        review_provider=review_provider,
        model=model,
        reasoning="none" if review_provider == "local" else "medium",
        final_provider=final_provider,
        final_model=final_model,
    )
    job = ui.jobs.start(settings)["id"]
    wait_until(lambda: ui.jobs.active is None and not ui.jobs.pending)
    assert events == ([("load", expected)] if expected else []) + [("extract", None)]
    assert ui.jobs.read(job)["status"] == "cancelled"


def test_defaults_enable_independent_local_vision_models_for_both_stages(
    workspace,
):
    _, source, client = workspace
    defaults = client.get("/api/bootstrap").json()["defaults"]
    assert defaults["review_provider"] == defaults["final_provider"] == "local"
    assert defaults["model"] == ui.SYSTEM_DEFAULTS["model"]
    assert defaults["final_model"] == ui.SYSTEM_DEFAULTS["final_model"]
    assert defaults["automatic_review"]
    assert defaults["local_preload"] and defaults["local_load_into_ram"]
    assert defaults["local_load_timeout"] == 7200
    assert defaults["reasoning"] == defaults["final_reasoning"] == "medium"
    assert defaults["local_temperature"] == 0
    assert defaults["review_timeout"] == 7200
    assert "work_mode" not in defaults
    local_config = {**defaults, "source_pdf": str(source)}
    assert client.post("/api/plan", json=local_config).status_code == 200
    assert (
        client.post(
            "/api/plan", json={**local_config, "local_use_gpu": True}
        ).status_code
        == 422
    )
    response = client.post(
        "/api/plan",
        json=config(
            source,
            model="gpt-6-sol",
            reasoning="low",
            final_model="gpt-6-astra",
            final_reasoning="max",
        ).model_dump(),
    ).json()
    assert (
        response["review_model"] == "gpt-6-sol"
        and response["final_model"] == "gpt-6-astra"
        and response["reasoning"] == "low"
        and response["final_reasoning"] == "max"
    )
    disabled = client.post(
        "/api/plan", json=config(source, final_model="none").model_dump()
    ).json()
    assert not next(s for s in disabled["stages"] if s["id"] == "final")["enabled"]
    assert next(s for s in disabled["stages"] if s["id"] == "validate")["enabled"]
    for provider in ["local", "work"]:
        disabled = client.post(
            "/api/plan",
            json=config(
                source,
                model="none",
                final_model="none",
                review_provider=provider,
                final_provider=provider,
            ).model_dump(),
        )
        assert disabled.status_code == 200, disabled.text
        assert {s["id"] for s in disabled.json()["stages"] if not s["enabled"]} == set(
            sr.CHECKS
        )
    assert config(source, automatic_review=True).automatic_review
    for model in ["gpt-6-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]:
        same = config(source, model=model, final_model=model)
        assert client.post("/api/plan", json=same.model_dump()).status_code == 200
    with pytest.raises(ValueError, match="not supported"):
        config(source, reasoning="none")


def test_manual_work_result_preserves_reasoning_without_claiming_model_authentication(
    workspace, tmp_path
):
    _, _, client = workspace
    request = sr.create_task(
        ui.queue(),
        "correctness",
        {"page": 1},
        [Path(__file__)],
        model="gpt-6-sol",
        reasoning="low",
        manual_handoff=True,
    )
    accepted = client.post(
        f"/api/review/tasks/{request['task_id']}/result", json=result(request)
    )
    assert accepted.status_code == 200
    execution = json.loads(
        (ui.queue() / request["task_id"] / "execution.json").read_text()
    )
    assert execution["provider"] == "chatgpt_work_manual"
    assert execution["reasoning"] == "low"
    assert "cannot be authenticated" in execution["provenance"]


def test_model_refresh_uses_only_metadata_and_separates_api_models(
    tmp_path, monkeypatch
):
    import refresh_models as rm
    import model_catalog as mc

    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-6.2-sol",
                        "display_name": "GPT 6.2 Sol",
                        "visibility": "list",
                        "input_modalities": ["text", "image"],
                        "supported_reasoning_levels": [
                            {"effort": "high"},
                            {"effort": "max"},
                            {"effort": "ultra"},
                        ],
                    },
                    {
                        "slug": "gpt-99-sol",
                        "visibility": "list",
                        "input_modalities": ["text"],
                        "supported_reasoning_levels": [{"effort": "high"}],
                    },
                    {
                        "slug": "gpt-6.3-luna",
                        "visibility": "list",
                        "input_modalities": ["text", "image"],
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "medium"},
                        ],
                    },
                ]
            }
        )
    )
    calls = []

    class Response:
        ok = True

        def json(self):
            return {"data": [{"id": "gpt-api-only"}]}

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer fixture-key"
        return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setattr(rm.requests, "get", get)
    output = tmp_path / "models.json"
    refreshed = rm.refresh(output, cache, api=True)
    assert (
        calls == ["https://api.openai.com/v1/models"]
        and refreshed["generation_calls"] == 0
    )
    assert mc.work_ids(tmp_path) == {"gpt-6.2-sol", "gpt-6.3-luna"}
    assert "chat_models" not in mc.catalog(tmp_path)
    assert mc.catalog(tmp_path)["work_models"][0]["reasoning_levels"] == ["high", "max"]
    mc.validate_choice(tmp_path, "gpt-6.2-sol", "max")
    mc.validate_choice(tmp_path, "gpt-6.3-luna", "low")
    with pytest.raises(ValueError, match="not supported"):
        mc.validate_choice(tmp_path, "gpt-6.2-sol", "low")
    assert "gpt-api-only" not in mc.work_ids(tmp_path)


def test_stage_settings_reject_levels_not_advertised_by_their_model(workspace):
    roots, source, client = workspace
    sr.atomic_json(
        roots["catalogs"] / "library/models.json",
        {
            "work_models": [
                {"id": "gpt-6-sol", "reasoning_levels": ["low", "high"]},
                {"id": "gpt-6-astra", "reasoning_levels": ["high", "max"]},
            ]
        },
    )
    settings = config(
        source,
        model="gpt-6-sol",
        final_model="gpt-6-astra",
        reasoning="low",
        final_reasoning="max",
    )
    assert client.post("/api/plan", json=settings.model_dump()).status_code == 200
    for changes in [{"reasoning": "max"}, {"final_reasoning": "low"}]:
        response = client.post("/api/plan", json={**settings.model_dump(), **changes})
        assert response.status_code == 422 and "not supported" in response.text


def test_manual_packets_preserve_tool_pages_values_and_page_scoped_web_evidence():
    import workbench_outputs as wo

    products = [
        {
            "product_version_id": "one",
            "primary_provenance_id": "p1",
            "identity_status": "identified",
        },
        {
            "product_version_id": "two",
            "primary_provenance_id": "p2",
            "identity_status": "identified",
        },
    ]
    attrs = [
        {
            "product_version_id": "one",
            "canonical_field": "diameter",
            "value_decimal": "12.5",
            "provenance_id": "p1",
        }
    ]
    provenance = [
        {"provenance_id": "p1", "page_idx": 3},
        {"provenance_id": "p2", "page_idx": 7},
    ]
    reviews = [
        {
            "page": 4,
            "result": {
                "checks": [
                    {
                        "source_url": "https://maker.example/tool",
                        "evidence": "Official page lists diameter 12.5 mm",
                        "status": "passed",
                    }
                ],
                "findings": [],
            },
        }
    ]
    packets = wo.manual_packets(products, attrs, provenance, reviews, "catalog", "sha")
    assert (
        packets[0]["pages"] == [4]
        and packets[0]["parameters"][0]["value_decimal"] == "12.5"
    )
    assert packets[0]["internet_sources"] and not packets[1]["internet_sources"]
    assert packets[1]["pages"] == [8] and packets[0]["manual_status"] == "pending"
    assert wo.safe_cell('=HYPERLINK("bad")').startswith("'")


def test_windows_catalog_inventory_and_default_languages(workspace):
    roots, source, client = workspace
    folder = roots["catalogs"] / "imports/guhring/nested"
    folder.mkdir(parents=True)
    imported = folder / "GTC_Milling_Cutters 2018.pdf"
    imported.write_bytes(source.read_bytes())
    before = registry.digest(imported)
    data = client.get("/api/bootstrap").json()
    assert data["defaults"]["languages"] == "en,ru,zh"
    assert data["defaults"]["source_pdf"] == str(imported)
    item = next(c for c in data["catalogs"] if c["path"] == str(imported))
    assert (
        item["origin"] == "Windows · GUHRING" and item["manufacturer_hint"] == "guhring"
    )
    assert item["pages"] == 3 and registry.digest(imported) == before


def test_detection_metadata_cache_skips_pdf_hash_and_invalidates_on_change(
    workspace, monkeypatch
):
    import catalog_detection as cd
    import hashlib

    roots, source, _ = workspace
    manufacturers = ui.library().manufacturers()
    cached = cd.inspect(
        source, manufacturers, roots["catalogs"] / "cache", allow_ocr=False
    )
    assert cached["total_pages"] == 3
    original_digest = hashlib.file_digest
    calls = []

    def digest(*args, **kwargs):
        calls.append(True)
        return original_digest(*args, **kwargs)

    monkeypatch.setattr(hashlib, "file_digest", digest)
    assert cd.inspect(
        source, manufacturers, roots["catalogs"] / "cache", allow_ocr=False
    )["cached"]
    assert not calls
    with source.open("ab") as stream:
        stream.write(b"\n% changed metadata\n")
    changed = cd.inspect(
        source, manufacturers, roots["catalogs"] / "cache", allow_ocr=False
    )
    assert calls and changed["source_sha256"] != cached["source_sha256"]


def test_review_notes_persist_without_approval_and_reject_stale_evidence(workspace):
    roots, source, client = workspace
    job = "c" * 32
    folder = roots["logs"] / "ui-runs" / job
    folder.mkdir(parents=True)
    state = {
        "id": job,
        "status": "quality_blocked",
        "stages": [],
        "config": {},
        "review_tasks": [],
    }
    (folder / "state.json").write_text(json.dumps(state))
    manifest = {"documents": [{"file": str(source), "sha256": registry.digest(source)}]}
    (folder / "catalog.yaml").write_text(yaml.safe_dump(manifest))
    packet = {
        "product": {"product_version_id": "one"},
        "parameters": [{"value_decimal": "12.5"}],
        "pages": [1],
        "source_sha256": registry.digest(source),
        "manual_status": "pending",
    }
    (folder / "manual-review.json").write_text(json.dumps([packet]))
    current = client.get(f"/api/runs/{job}/manual-review").json()["tools"][0]
    decision = {
        "product_version_id": "one",
        "snapshot_sha256": current["snapshot_sha256"],
        "status": "checked",
        "notes": "Compare d1 with the drawing",
    }
    saved = client.post(f"/api/runs/{job}/manual-review/decision", json=decision)
    assert saved.status_code == 200, saved.text
    reopened = client.get(f"/api/runs/{job}/manual-review").json()["tools"][0]
    assert (
        reopened["decision"]["notes"] == decision["notes"]
        and reopened["decision"]["status"] == "checked"
    )
    assert json.loads((folder / "state.json").read_text()) == state
    assert json.loads((folder / "manual-review.json").read_text()) == [packet]
    assert (
        client.post(
            f"/api/runs/{job}/manual-review/decision",
            json={**decision, "product_version_id": "other"},
        ).status_code
        == 404
    )
    packet["parameters"][0]["value_decimal"] = "13"
    (folder / "manual-review.json").write_text(json.dumps([packet]))
    stale = client.get(f"/api/runs/{job}/manual-review").json()["tools"][0]
    assert (
        stale["decision"]["status"] == "pending"
        and stale["previous_decision"]["status"] == "checked"
    )
    assert (
        client.post(
            f"/api/runs/{job}/manual-review/decision", json=decision
        ).status_code
        == 409
    )
    with source.open("ab") as stream:
        stream.write(b"\n% changed source\n")
    assert (
        client.post(
            f"/api/runs/{job}/manual-review/decision",
            json={**decision, "snapshot_sha256": stale["snapshot_sha256"]},
        ).status_code
        == 409
    )


def test_work_review_packet_download_checks_original_evidence(workspace, tmp_path):
    import io
    import zipfile

    _, _, client = workspace
    image = tmp_path / "page.png"
    image.write_bytes(b"test-page-image")
    request = sr.create_task(
        ui.queue(),
        "correctness",
        {"page": 1},
        [image],
        model="gpt-6-sol",
        manual_handoff=True,
    )
    url = f"/api/review/tasks/{request['task_id']}/packet"
    response = client.get(url)
    assert (
        response.status_code == 200
        and response.headers["content-type"] == "application/zip"
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "prompt.txt",
            "schema.json",
            "request.json",
            "evidence-0.png",
        }
        assert archive.read("evidence-0.png") == image.read_bytes()
        assert json.loads(archive.read("request.json"))["manual_handoff"]
    (ui.queue() / request["task_id"] / "evidence-0.png").write_bytes(b"tampered")
    assert client.get(url).status_code == 409
