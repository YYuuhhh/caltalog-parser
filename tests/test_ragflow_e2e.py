from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ragflow_e2e  # noqa: E402
import verify_vlm_accuracy  # noqa: E402


class StubClient:
    def __init__(self, payload: dict):
        self.payload = payload

    def get(self, path: str, **kwargs: object) -> dict:
        assert path == "/api/v1/models"
        assert kwargs == {"params": {"type": "ocr"}}
        return self.payload


@pytest.mark.parametrize(
    "data",
    [
        [{"provider_name": "MinerU", "name": "mineru-from-env"}],
        {"models": [{"provider_name": "MinerU", "name": "mineru-from-env"}]},
        {
            "models": {
                "model-id": {
                    "model_provider": "MinerU",
                    "model_name": "mineru-from-env",
                }
            }
        },
    ],
)
def test_ensure_mineru_provider_accepts_supported_envelopes(data: object) -> None:
    result = ragflow_e2e.ensure_mineru_provider(StubClient({"data": data}))

    assert (
        result["model"].get("name", result["model"].get("model_name"))
        == "mineru-from-env"
    )
    assert result["provisioning_retry"] is False


@pytest.mark.parametrize(
    "model",
    [
        {"provider_name": "MinerU", "name": "mineru-from-env-copy"},
        {"provider_name": "NotMinerU", "name": "mineru-from-env"},
        {"provider_name": "MinerU", "name": "other-model"},
    ],
)
def test_ensure_mineru_provider_rejects_inexact_matches(model: dict) -> None:
    with pytest.raises(RuntimeError, match="did not provision"):
        ragflow_e2e.ensure_mineru_provider(StubClient({"data": {"models": [model]}}))


class PaginatedClient:
    def __init__(self, *, stuck=False):
        self.pages = []
        self.updates = []
        self.stuck = stuck

    def get(self, path, **kwargs):
        if path == "/api/v1/datasets/target":
            return {"data": {"id": "target", "name": "wanted"}}
        page = kwargs["params"]["page"]
        self.pages.append(page)
        items = (
            [{"id": str(index), "name": f"other-{index}"} for index in range(100)]
            if page == 1 or self.stuck
            else [{"id": "target", "name": "wanted"}]
        )
        return {"data": {"docs": items} if path.endswith("/documents") else items}

    def put(self, path, **kwargs):
        self.updates.append(kwargs["json"])

    def post(self, *args, **kwargs):
        raise AssertionError("Must reuse the dataset on page two")


def test_existing_dataset_and_document_after_first_hundred_are_reused():
    client = PaginatedClient()
    assert (
        ragflow_e2e.ensure_dataset(client, "wanted", "local", "English")["id"]
        == "target"
    )
    assert client.updates[0]["language"] == "English"
    assert ragflow_e2e.find_document(client, "target", "wanted")["id"] == "target"
    assert client.pages == [1, 2, 1, 2]


@pytest.mark.parametrize("operation", ["dataset", "document"])
def test_nonadvancing_pagination_fails_without_duplicate_upload(operation):
    client = PaginatedClient(stuck=True)
    with pytest.raises(RuntimeError, match="pagination did not advance"):
        if operation == "dataset":
            ragflow_e2e.ensure_dataset(client, "wanted", "local")
        else:
            ragflow_e2e.find_document(client, "target", "wanted")


def test_done_document_with_parser_error_is_not_successful():
    class FailedDocument:
        def get(self, *args, **kwargs):
            return {
                "data": {
                    "docs": [
                        {
                            "run": "DONE",
                            "progress": 1,
                            "progress_msg": "[ERROR]MinerU not found.\nNo chunk built.",
                        }
                    ]
                }
            }

    with pytest.raises(RuntimeError, match="MinerU not found"):
        ragflow_e2e.poll_document(FailedDocument(), "dataset", "document", 1)


def test_explicit_failed_vlm_run_never_uses_historical_success(tmp_path):
    source = "a" * 64
    historical = tmp_path / "ragflow-e2e-runs/old.json"
    historical.parent.mkdir()
    historical.write_text(
        json.dumps(
            {
                "status": "success",
                "source_sha256": source,
                "completed_at": "2026-08-25T00:00:00Z",
            }
        )
    )
    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps({"status": "failed", "source_sha256": source}))
    with pytest.raises(ValueError, match="selected RAGFlow report"):
        verify_vlm_accuracy.selected_report(tmp_path, source, failed)


def test_explicit_vlm_report_is_bound_to_tested_pdf(tmp_path):
    report = tmp_path / "ingestion.json"
    report.write_text(json.dumps({"status": "success", "source_sha256": "a" * 64}))
    with pytest.raises(ValueError, match="tested PDF"):
        verify_vlm_accuracy.selected_report(tmp_path, "b" * 64, report)
    assert verify_vlm_accuracy.selected_report(tmp_path, "a" * 64, report)[0] == report


@pytest.mark.parametrize("backend", ["pipeline", "vlm-transformers"])
def test_profile_switch_updates_all_generated_instances_and_preserves_custom(backend):
    class InstancesClient:
        def __init__(self):
            self.keys = {
                "mineru-from-env": {"MINERU_BACKEND": "pipeline"},
                "mineru-from-env(2)": {"MINERU_BACKEND": "vlm-transformers"},
                "custom-mineru": {"MINERU_BACKEND": "custom"},
                "mineru-from-env-copy": {"MINERU_BACKEND": "custom"},
            }
            self.updated = []

        def get(self, path):
            if path.endswith("/instances"):
                return {"data": [{"instance_name": name} for name in self.keys]}
            name = path.split("/instances/")[1].split("/")[0]
            assert name not in {"custom-mineru", "mineru-from-env-copy"}
            if path.endswith("/models"):
                return {"data": [{"name": "mineru-from-env", "model_type": ["ocr"]}]}
            return {"data": {"api_key": json.dumps(self.keys[name])}}

        def put(self, path, **kwargs):
            payload = kwargs["json"]
            name = payload["instance_name"]
            assert path.endswith("/" + name)
            self.keys[name] = payload["api_key"]
            self.updated.append(name)

    client = InstancesClient()
    result = ragflow_e2e.sync_mineru_provider_config(
        client, mineru_url="http://mineru:8000/", backend=backend
    )
    assert result["updated"] is True
    assert client.updated == ["mineru-from-env", "mineru-from-env(2)"]
    assert all(
        client.keys[name]["MINERU_BACKEND"] == backend for name in client.updated
    )
    assert client.keys["custom-mineru"] == {"MINERU_BACKEND": "custom"}
    assert (
        ragflow_e2e.sync_mineru_provider_config(
            client, mineru_url="http://mineru:8000", backend=backend
        )["updated"]
        is False
    )
    assert len(client.updated) == 2
