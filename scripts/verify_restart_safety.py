#!/usr/bin/env python3
"""Verify that a post-restart RAGFlow run reused an immutable MinerU page."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def read_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} is not a JSON object")
    return value


def mounted_path(value: Any, parsed_root: Path) -> Path:
    raw = Path(str(value))
    if not raw.is_absolute():
        raise ValueError(f"expected an absolute parser path, got {raw}")
    try:
        relative = raw.relative_to("/parsed")
    except ValueError as exc:
        raise ValueError(f"parser path is outside /parsed: {raw}") from exc
    return parsed_root / relative


def parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--parsed-root", type=Path, default=Path("/parsed"))
    parser.add_argument("--ragflow-log", type=Path, default=Path("/logs/ragflow-e2e-latest.json"))
    parser.add_argument("--output", type=Path, default=Path("/logs/restart-safety-latest.json"))
    parser.add_argument("--expected-device", required=True)
    parser.add_argument("--expected-backend", required=True)
    parser.add_argument("--expected-attempt-count", type=int, default=1)
    args = parser.parse_args()

    source_sha = sha256_file(args.pdf)
    ragflow = read_object(args.ragflow_log)
    listed = ragflow.get("mineru_cache_artifacts")
    artifacts = [item for item in listed if isinstance(item, Mapping)] if isinstance(listed, list) else []
    matching = [item for item in artifacts if item.get("page") == 0 and item.get("status") == "success"]
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {
        "source_sha256": source_sha,
        "ragflow_run_id": ragflow.get("run_id"),
    }

    checks["ragflow_completed_through_internal_mineru"] = (
        ragflow.get("status") == "success"
        and ragflow.get("source_sha256") == source_sha
        and str((ragflow.get("document") or {}).get("run", "")).upper() == "DONE"
        and urlparse(str(ragflow.get("mineru_url", ""))).hostname == "mineru"
    )
    checks["one_matching_cache_artifact"] = len(matching) == 1

    if matching:
        logged = matching[0]
        manifest_path = mounted_path(logged.get("manifest"), args.parsed_root)
        manifest = read_object(manifest_path)
        page_dir = manifest_path.parent.parent.parent
        pointer = read_object(page_dir / "success.json")
        attempts_dir = page_dir / "attempts"
        attempts = sorted(path for path in attempts_dir.iterdir() if path.is_dir())
        response_path = manifest_path.parent / "response.zip"
        raw_response_path = mounted_path(manifest.get("raw_archive"), args.parsed_root) / "response.zip"
        response_sha = sha256_file(response_path)
        raw_sha = sha256_file(raw_response_path)
        config = manifest.get("config") if isinstance(manifest.get("config"), Mapping) else {}
        request = config.get("request") if isinstance(config.get("request"), Mapping) else {}
        config_digest = hashlib.sha256(canonical_json(config)).hexdigest()
        cache_key = hashlib.sha256(
            f"guhring-cache-v1\0{source_sha}\0{manifest.get('page')}\0{config_digest}".encode()
        ).hexdigest()
        expected_backend = args.expected_backend.casefold()
        if expected_backend in {"vlm-transformers", "vlm-engine"}:
            expected_backend = "vlm-engine"

        checks.update(
            {
                "cache_identity_recomputes": (
                    manifest.get("config_digest") == config_digest
                    and manifest.get("cache_key") == cache_key
                    and pointer.get("cache_key") == cache_key
                ),
                "runtime_matches_expected_profile": (
                    str(config.get("device_mode", "")).casefold()
                    == args.expected_device.casefold()
                    and str(request.get("backend", "")).casefold() == expected_backend
                ),
                "successful_pointer_still_selects_original_attempt": (
                    page_dir / str(pointer.get("attempt")) == manifest_path.parent
                    and len(attempts) == args.expected_attempt_count
                ),
                "cache_and_raw_archives_are_byte_identical": (
                    response_sha
                    == raw_sha
                    == manifest.get("response_sha256")
                    == pointer.get("response_sha256")
                    == logged.get("response_sha256")
                ),
                "artifact_predates_post_restart_run": (
                    parse_time(manifest.get("completed_at")) < parse_time(ragflow.get("started_at"))
                ),
                # The wrapper served the persisted page before the freshly restarted
                # upstream MinerU process had executed any task.
                "post_restart_request_did_not_reinvoke_upstream": (
                    ((ragflow.get("mineru_health") or {}).get("upstream") or {}).get(
                        "completed_tasks"
                    )
                    == 0
                ),
            }
        )
        evidence.update(
            {
                "manifest": str(manifest_path),
                "attempt": manifest_path.parent.name,
                "attempt_count": len(attempts),
                "cache_key": cache_key,
                "config_digest": config_digest,
                "response_sha256": response_sha,
                "raw_response_sha256": raw_sha,
                "artifact_completed_at": manifest.get("completed_at"),
                "post_restart_run_started_at": ragflow.get("started_at"),
                "original_processing_time_seconds": manifest.get("processing_time"),
                "post_restart_e2e_time_seconds": ragflow.get("processing_time"),
            }
        )

    failures = [name for name, passed in checks.items() if not passed]
    report = {
        "status": "success" if not failures else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "failures": failures,
        "evidence": evidence,
    }
    atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
