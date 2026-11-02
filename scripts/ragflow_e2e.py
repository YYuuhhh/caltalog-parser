#!/usr/bin/env python3
"""Create/update a MinerU-first RAGFlow dataset and run a real PDF ingestion."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding


PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/
z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp
2Q2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOOU
EfaS318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVKRNL
JpL8w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK6ol
N4RevSfVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs2wID
AQAB
-----END PUBLIC KEY-----
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encrypt_password(password: str) -> str:
    public_key = serialization.load_pem_public_key(PUBLIC_KEY)
    plaintext = base64.b64encode(password.encode("utf-8"))
    encrypted = public_key.encrypt(plaintext, padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def persist_report(report: Mapping[str, Any]) -> None:
    """Keep immutable per-run evidence in addition to the convenient latest file."""

    run_id = str(report.get("run_id", ""))
    if len(run_id) != 32 or any(
        character not in "0123456789abcdef" for character in run_id
    ):
        raise ValueError(f"invalid run id for report path: {run_id!r}")
    atomic_json(Path("/logs/ragflow-e2e-runs") / f"{run_id}.json", report)
    atomic_json(Path("/logs/ragflow-e2e-latest.json"), report)


class RAGFlowClient:
    def __init__(self, base_url: str, email: str, password: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.authorization = ""

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"email": self.email, "password": encrypt_password(self.password)},
            timeout=self.timeout,
        )
        payload = self._payload(response)
        authorization = response.headers.get("Authorization") or response.headers.get(
            "authorization"
        )
        if not authorization:
            raise RuntimeError(
                f"RAGFlow login returned no Authorization header: {payload}"
            )
        self.authorization = authorization

    def _headers(self) -> dict[str, str]:
        if not self.authorization:
            return {}
        value = self.authorization
        if not value.lower().startswith("bearer "):
            value = f"Bearer {value}"
        return {"Authorization": value}

    def _payload(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"RAGFlow returned HTTP {response.status_code} with non-JSON body: {response.text[:500]}"
            ) from exc
        if response.status_code >= 400 or payload.get("code") not in (None, 0):
            raise RuntimeError(
                f"RAGFlow request failed (HTTP {response.status_code}): {payload}"
            )
        return payload

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        return self._payload(response)

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        return self._payload(response)

    def put(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.put(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        return self._payload(response)

    def patch(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.patch(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        return self._payload(response)


def wait_http(url: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return {"status": response.text[:500]}
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(3)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def desired_parser_config() -> dict[str, Any]:
    return {
        "layout_recognize": "MinerU",
        "task_page_size": 1,
        "chunk_token_num": 512,
        "delimiter": "\n!?;。；！？",
        "html4excel": False,
        "raptor": {"use_raptor": False},
        "graphrag": {"use_graphrag": False},
    }


def ensure_mineru_provider(client: RAGFlowClient) -> dict[str, Any]:
    """Provision and verify RAGFlow's env-backed local MinerU OCR model.

    RAGFlow v0.27.0 commits the provider and instance on its first lazy
    provisioning call, then raises because Peewee returns an integer from the
    insert.  The supported models endpoint is idempotent, so one exact retry
    completes the model row without sacrificing a document ingestion job.
    """
    first_error: RuntimeError | None = None
    try:
        payload = client.get("/api/v1/models", params={"type": "ocr"})
    except RuntimeError as exc:
        first_error = exc
        payload = client.get("/api/v1/models", params={"type": "ocr"})
    data = payload.get("data") or []
    if isinstance(data, dict):
        models: Any = data.get("models") or []
    else:
        models = data
    # Some API envelopes key model objects by identifier instead of returning
    # an array.  Normalize only that envelope shape; matching below stays exact.
    if isinstance(models, dict):
        models = list(models.values())
    if not isinstance(models, list):
        models = []
    mineru = next(
        (
            model
            for model in models
            if isinstance(model, dict)
            and str(
                model.get("provider_name") or model.get("model_provider") or ""
            ).casefold()
            == "mineru"
            and str(model.get("name") or model.get("model_name") or "")
            == "mineru-from-env"
        ),
        None,
    )
    if mineru is None:
        raise RuntimeError(
            f"RAGFlow did not provision its local MinerU OCR model: {models}"
        )
    return {"model": mineru, "provisioning_retry": first_error is not None}


def sync_mineru_provider_config(
    client: RAGFlowClient, *, mineru_url: str, backend: str
) -> dict[str, Any]:
    """Align generated env instances, which RAGFlow duplicates on profile switches."""
    desired_key = {
        "MINERU_APISERVER": mineru_url.rstrip("/"),
        "MINERU_OUTPUT_DIR": "/ragflow/mineru-output",
        "MINERU_BACKEND": backend,
        "MINERU_SERVER_URL": "",
        "MINERU_DELETE_OUTPUT": "0",
    }
    listed = client.get("/api/v1/providers/MinerU/instances").get("data") or []
    if not isinstance(listed, list):
        raise RuntimeError("RAGFlow returned an invalid MinerU instances list")
    generated = [
        item
        for item in listed
        if isinstance(item, dict)
        and re.fullmatch(
            r"mineru-from-env(?:\(\d+\))?", str(item.get("instance_name", ""))
        )
    ]
    if not generated:
        raise RuntimeError("RAGFlow has no generated env-backed MinerU instance")
    changes = []
    for item in generated:
        name = item["instance_name"]
        path = f"/api/v1/providers/MinerU/instances/{name}"
        instance = client.get(path).get("data") or {}
        if not isinstance(instance, dict):
            raise RuntimeError(f"RAGFlow returned an invalid MinerU instance: {name}")
        models = client.get(path + "/models").get("data") or []
        if not isinstance(models, list) or not models:
            raise RuntimeError(f"RAGFlow MinerU instance has no OCR model: {name}")
        current_key: Any = instance.get("api_key") or {}
        if isinstance(current_key, str):
            try:
                current_key = json.loads(current_key)
            except json.JSONDecodeError:
                current_key = {}
        updated = current_key != desired_key
        changes.append({"instance_name": name, "updated": updated})
        if not updated:
            continue
        model_info = [
            {
                "model_name": str(model.get("name") or model.get("model_name")),
                "model_type": model.get("model_type") or ["ocr"],
                "max_tokens": int(model.get("max_tokens") or 0),
                "extra": {},
            }
            for model in models
            if isinstance(model, dict)
            and (model.get("name") or model.get("model_name"))
        ]
        client.put(
            path,
            json={
                "instance_name": name,
                "api_key": desired_key,
                "base_url": "",
                "region": instance.get("region") or "",
                "model_info": model_info,
                "verify": False,
            },
        )
    return {
        "updated": any(item["updated"] for item in changes),
        "instances": changes,
        "config": desired_key,
    }


def ensure_dataset(
    client: RAGFlowClient, name: str, embedding_model: str, language: str = "German"
) -> dict[str, Any]:
    parser_config = desired_parser_config()
    # RAGFlow v0.27 returns a permission-style error when an exact-name filter
    # has no match, so list first and filter locally on fresh installations.
    page = 1
    existing = None
    seen = set()
    while True:
        listed = client.get("/api/v1/datasets", params={"page": page, "page_size": 100})
        datasets = listed.get("data") or []
        existing = next(
            (
                item
                for item in datasets
                if item.get("name", "").casefold() == name.casefold()
            ),
            None,
        )
        if existing is not None or len(datasets) < 100:
            break
        identities = {item["id"] for item in datasets}
        if identities <= seen:
            raise RuntimeError("RAGFlow dataset pagination did not advance.")
        seen.update(identities)
        page += 1
    payload = {
        "name": name,
        "description": "Local tool catalogs; MinerU is the primary PDF parser.",
        "embedding_model": embedding_model,
        "chunk_method": "naive",
        "parser_config": parser_config,
    }
    update_payload = {**payload, "language": language}
    if existing:
        client.put(f"/api/v1/datasets/{existing['id']}", json=update_payload)
        return client.get(f"/api/v1/datasets/{existing['id']}")["data"]
    created = client.post("/api/v1/datasets", json=payload)["data"]
    client.put(f"/api/v1/datasets/{created['id']}", json=update_payload)
    return client.get(f"/api/v1/datasets/{created['id']}")["data"]


def find_document(
    client: RAGFlowClient, dataset_id: str, name: str
) -> dict[str, Any] | None:
    page = 1
    seen = set()
    while True:
        payload = client.get(
            f"/api/v1/datasets/{dataset_id}/documents",
            params={
                "page": page,
                "page_size": 100,
                "orderby": "create_time",
                "desc": "true",
            },
        )
        documents = (payload.get("data") or {}).get("docs") or []
        existing = next(
            (document for document in documents if document.get("name") == name), None
        )
        if existing is not None or len(documents) < 100:
            return existing
        identities = {document["id"] for document in documents}
        if identities <= seen:
            raise RuntimeError("RAGFlow document pagination did not advance.")
        seen.update(identities)
        page += 1


def upload_document(
    client: RAGFlowClient, dataset_id: str, pdf: Path, upload_name: str
) -> dict[str, Any]:
    with pdf.open("rb") as handle:
        payload = client.post(
            f"/api/v1/datasets/{dataset_id}/documents",
            files={"file": (upload_name, handle, "application/pdf")},
        )
    data = payload.get("data") or []
    if isinstance(data, dict):
        return data
    if not data:
        raise RuntimeError(f"RAGFlow upload returned no document: {payload}")
    return data[0]


def poll_document(
    client: RAGFlowClient, dataset_id: str, document_id: str, timeout: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    last_printed = ""
    while time.monotonic() < deadline:
        payload = client.get(
            f"/api/v1/datasets/{dataset_id}/documents",
            params={"id": document_id, "page_size": 1},
        )
        docs = (payload.get("data") or {}).get("docs") or []
        if not docs:
            raise RuntimeError(f"document {document_id} disappeared while parsing")
        last = docs[0]
        status_line = f"run={last.get('run')} progress={last.get('progress')} message={last.get('progress_msg', '')}"
        if status_line != last_printed:
            print(status_line, flush=True)
            last_printed = status_line
        run = str(last.get("run", "")).upper()
        progress = float(last.get("progress") or 0)
        if (
            run in {"2", "4", "CANCEL", "FAIL"}
            or progress < 0
            or "[ERROR]" in str(last.get("progress_msg", ""))
        ):
            raise RuntimeError(f"RAGFlow parsing failed: {status_line}")
        if run in {"3", "DONE"} or progress >= 1:
            return last
        time.sleep(5)
    raise TimeoutError(f"RAGFlow parsing timed out after {timeout}s: {last}")


def cache_artifacts(source_sha: str) -> list[dict[str, Any]]:
    root = Path("/parsed/cache/v1") / source_sha
    results = []
    for pointer_path in sorted(root.glob("*/pages/*/success.json")):
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        attempt = pointer_path.parent / pointer["attempt"]
        manifest_path = attempt / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = manifest.get("config") or {}
        request_config = config.get("request") or {}
        results.append(
            {
                "page": manifest.get("page"),
                "cache_key": manifest.get("cache_key"),
                "config_digest": manifest.get("config_digest"),
                "mineru_version": config.get("mineru_version"),
                "model_revision": config.get("model_revision"),
                "runtime_fingerprint": config.get("runtime_fingerprint"),
                "device": config.get("device_mode"),
                "backend": request_config.get("backend"),
                "effective_backend": config.get("effective_backend"),
                "status": manifest.get("status"),
                "processing_time": manifest.get("processing_time"),
                "response_sha256": manifest.get("response_sha256"),
                "manifest": str(manifest_path),
            }
        )
    return results


def _main(run_id: str, invocation_started: str) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pdf",
        type=Path,
        nargs="?",
        default=Path("/catalogs/test/guhring_catalog_test.pdf"),
    )
    parser.add_argument(
        "--ragflow-url", default=os.environ.get("RAGFLOW_URL", "http://ragflow")
    )
    parser.add_argument(
        "--mineru-url", default=os.environ.get("MINERU_API_URL", "http://mineru:8000")
    )
    parser.add_argument(
        "--email", default=os.environ.get("RAGFLOW_ADMIN_EMAIL", "admin@guhring.local")
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("RAGFLOW_ADMIN_PASSWORD", "change-me-local"),
    )
    parser.add_argument(
        "--dataset", default=os.environ.get("RAGFLOW_DATASET", "Tool Catalogs (MinerU)")
    )
    parser.add_argument(
        "--language", default=os.environ.get("RAGFLOW_LANGUAGE", "German")
    )
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get(
            "RAGFLOW_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5@Builtin"
        ),
    )
    parser.add_argument(
        "--mineru-version", default=os.environ.get("MINERU_VERSION", "3.4.5")
    )
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--parse-timeout", type=int, default=7200)
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()

    if not args.setup_only and not args.pdf.is_file():
        raise FileNotFoundError(f"Test PDF does not exist: {args.pdf}")

    started = time.monotonic()
    mineru_health = wait_http(
        f"{args.mineru_url.rstrip('/')}/health", args.startup_timeout
    )
    openapi = requests.head(f"{args.mineru_url.rstrip('/')}/openapi.json", timeout=10)
    if openapi.status_code != 200:
        raise RuntimeError(
            f"RAGFlow-compatible MinerU HEAD /openapi.json failed: HTTP {openapi.status_code}"
        )
    wait_http(
        f"{args.ragflow_url.rstrip('/')}/api/v1/system/healthz", args.startup_timeout
    )

    client = RAGFlowClient(args.ragflow_url, args.email, args.password)
    client.login()
    mineru_provider = ensure_mineru_provider(client)
    mineru_provider_config = sync_mineru_provider_config(
        client,
        mineru_url=args.mineru_url,
        backend=os.environ.get("MINERU_BACKEND", "pipeline"),
    )
    dataset = ensure_dataset(client, args.dataset, args.embedding_model, args.language)
    if (
        str((dataset.get("parser_config") or {}).get("layout_recognize", "")).lower()
        != "mineru"
    ):
        raise RuntimeError(
            f"dataset is not MinerU-first: {dataset.get('parser_config')}"
        )

    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": invocation_started,
        "ragflow_url": args.ragflow_url,
        "mineru_url": args.mineru_url,
        "mineru_health": mineru_health,
        "mineru_openapi_head": openapi.status_code,
        "mineru_provider": mineru_provider,
        "mineru_provider_config": mineru_provider_config,
        "dataset_id": dataset["id"],
        "dataset_name": dataset["name"],
        "dataset_parser_config": dataset.get("parser_config"),
    }
    if args.setup_only:
        report["status"] = "configured"
    else:
        source_sha = sha256_file(args.pdf)
        upload_name = f"catalog-{source_sha[:12]}.pdf"
        document = find_document(client, dataset["id"], upload_name)
        if document is None:
            document = upload_document(client, dataset["id"], args.pdf, upload_name)
        # A document keeps a copy of its dataset parser configuration. Refresh
        # it on every idempotent run so an older upload cannot silently fall
        # back to DeepDOC after the dataset is changed to MinerU.
        document = client.patch(
            f"/api/v1/datasets/{dataset['id']}/documents/{document['id']}",
            json={"chunk_method": "naive", "parser_config": desired_parser_config()},
        )["data"]
        if (
            str(
                (document.get("parser_config") or {}).get("layout_recognize", "")
            ).lower()
            != "mineru"
        ):
            raise RuntimeError(
                f"document is not MinerU-first: {document.get('parser_config')}"
            )
        parse_endpoint = f"/api/v1/datasets/{dataset['id']}/documents/parse"
        client.post(parse_endpoint, json={"document_ids": [document["id"]]})
        provisioning_retries = 0
        try:
            completed = poll_document(
                client, dataset["id"], document["id"], args.parse_timeout
            )
        except RuntimeError as exc:
            # RAGFlow v0.27.0 has a one-time lazy-provisioning defect for an
            # env-backed OCR provider: the instance row is committed, but the
            # first call then treats Peewee's integer insert result as a model
            # object. Retrying only this exact failure completes the missing
            # model row; subsequent runs do not enter this branch.
            if "MinerU not found" not in str(exc):
                raise
            provisioning_retries = 1
            print(
                "RAGFlow v0.27 MinerU provider was partially provisioned; retrying once.",
                flush=True,
            )
            document = client.patch(
                f"/api/v1/datasets/{dataset['id']}/documents/{document['id']}",
                json={
                    "chunk_method": "naive",
                    "parser_config": desired_parser_config(),
                },
            )["data"]
            client.post(parse_endpoint, json={"document_ids": [document["id"]]})
            completed = poll_document(
                client, dataset["id"], document["id"], args.parse_timeout
            )
        all_artifacts = cache_artifacts(source_sha)
        expected_backend = (
            os.environ.get("MINERU_BACKEND", "pipeline").strip().casefold()
        )
        if expected_backend in {"vlm-transformers", "vlm-engine"}:
            expected_backend = "vlm-engine"
        artifacts = [
            item
            for item in all_artifacts
            if item.get("mineru_version") == args.mineru_version
            and item.get("runtime_fingerprint")
            == mineru_health.get("runtime_fingerprint")
            and str(item.get("device", "")).casefold()
            == str(mineru_health.get("device", "")).casefold()
            and str(item.get("effective_backend") or item.get("backend", "")).casefold()
            == expected_backend
        ]
        if not artifacts:
            raise RuntimeError(
                f"RAGFlow finished without a persistent MinerU {args.mineru_version} cache artifact; "
                f"found: {all_artifacts}"
            )
        report.update(
            {
                "status": "success",
                "source_file": str(args.pdf),
                "source_sha256": source_sha,
                "document_id": document["id"],
                "document": completed,
                "mineru_cache_artifacts": artifacts,
                "provider_provisioning_retries": provisioning_retries,
            }
        )
    report["processing_time"] = round(time.monotonic() - started, 6)
    report["completed_at"] = utc_now()
    persist_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    """Persist failures atomically so an older successful run cannot look current."""

    invocation_started = utc_now()
    run_id = uuid.uuid4().hex
    try:
        return _main(run_id, invocation_started)
    except Exception as exc:
        report = {
            "status": "failed",
            "run_id": run_id,
            "started_at": invocation_started,
            "completed_at": utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        persist_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
