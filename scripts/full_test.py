"""Health, asset, and evidence checks used by the Docker full-test workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

REQUIRED_STEPS = {
    "compose",
    "baseline",
    "health",
    "runtime",
    "assets",
    "regression",
    "inventory",
    "ingestion",
    "export",
    "raw_validate",
    "structure",
    "structure_validate",
    "cached_structure",
    "golden",
    "openai_rows_plan",
    "openai_pages_plan",
    "restart",
    "restarted_health",
    "cached_ingestion",
    "restart_cache",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_digests(root):
    return {path.name: digest(path) for path in Path(root).glob("*.py")}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def health(timeout):
    deadline = time.monotonic() + timeout
    endpoints = {
        "mineru": "http://mineru:8000/health",
        "ragflow": "http://ragflow/api/v1/system/healthz",
        "tei": "http://tei/health",
        "ragflow_ui": "http://ragflow/",
    }
    last = {}
    while True:
        for name, url in endpoints.items():
            try:
                with urlopen(url, timeout=5) as response:
                    body = response.read() if name in {"mineru", "ragflow"} else b""
                    state = json.loads(body) if body else {"status": "ok"}
                    if state.get("status") not in {"ok", "healthy"}:
                        raise RuntimeError(f"Unexpected health status: {state}")
                    last[name] = {"status": "healthy", "http_status": response.status}
            except Exception as exc:
                last[name] = {"status": "unavailable", "error": str(exc)}
        if all(item["status"] == "healthy" for item in last.values()):
            return last
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Services did not become healthy: {last}")
        time.sleep(3)


def assets():
    import yaml
    from jsonschema import Draft202012Validator

    schemas = Path("/schemas")
    for path in schemas.glob("*.json"):
        value = json.loads(path.read_text())
        if path.name.endswith(".schema.json"):
            Draft202012Validator.check_schema(value)
    mapping_schema = json.loads((schemas / "field-mapping.schema.json").read_text())
    mappings = list(Path("/mappings").glob("*.yaml"))
    for path in mappings:
        Draft202012Validator(mapping_schema).validate(yaml.safe_load(path.read_text()))
    return {
        "schemas": len(list(schemas.glob("*.json"))),
        "mapping_profiles": len(mappings),
    }


def memory_bytes(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)\s*", value)
    factors = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    if match is None or match[2] not in factors:
        raise ValueError(f"Unsupported memory measurement: {value!r}")
    return int(float(match[1]) * factors[match[2]])


def summarize(
    run_id,
    *,
    logs_root=Path("/logs"),
    parsed_root=Path("/parsed"),
    scripts_root=Path("/scripts"),
    wrapper_path=Path("/mineru-src/app.py"),
):
    root = logs_root / "full-test-runs" / run_id
    parsed = parsed_root / "full-test-runs" / run_id
    steps = []
    for line in (root / "steps.tsv").read_text().splitlines():
        name, category, code, elapsed = line.split("\t")
        steps.append(
            {
                "name": name,
                "category": category,
                "exit_code": int(code),
                "elapsed_seconds": int(elapsed),
                "stdout": str(root / f"{name}.stdout"),
                "stderr": str(root / f"{name}.stderr"),
            }
        )
    operational_failures = [
        step["name"]
        for step in steps
        if step["category"] == "operational" and step["exit_code"]
    ]
    operational_failures.extend(
        f"missing_step:{name}"
        for name in sorted(REQUIRED_STEPS - {step["name"] for step in steps})
    )
    quality_failures = [
        step["name"]
        for step in steps
        if step["category"] == "quality" and step["exit_code"]
    ]
    additional_checks = {
        "running_wrapper_matches_repository": False,
        "existing_raw_workbook_unchanged": False,
        "etl_cache_reused": False,
        "canonical_content_hashes_stable": False,
        "tested_code_unchanged": False,
    }
    runtime = root / "runtime.stdout"
    if runtime.is_file():
        additional_checks["running_wrapper_matches_repository"] = (
            runtime.read_text().strip() == digest(wrapper_path)
        )
    try:
        raw_before = json.loads((root / "baseline.json").read_text())
        additional_checks["existing_raw_workbook_unchanged"] = raw_before[
            "raw_sha256"
        ] == digest(parsed_root / "master_raw.xlsx")
        additional_checks["tested_code_unchanged"] = raw_before.get(
            "code_sha256"
        ) == code_digests(scripts_root)
    except (ValueError, OSError, KeyError):
        pass
    first_etl = None
    for name in ["structure", "cached_structure"]:
        path = root / f"{name}.stdout"
        try:
            value = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if name == "structure":
            first_etl = value
            if value.get("results", {}).get("blocking_issues"):
                quality_failures.append("catalog_publication_gate")
        else:
            additional_checks["etl_cache_reused"] = value.get("cache_hit") is True
            if first_etl is not None:
                before = {
                    key: item.get("content_sha256")
                    for key, item in first_etl.get("outputs", {}).items()
                }
                after = {
                    key: item.get("content_sha256")
                    for key, item in value.get("outputs", {}).items()
                }
                additional_checks["canonical_content_hashes_stable"] = (
                    bool(before) and all(before.values()) and before == after
                )
    peaks = {}
    stats = root / "resources.jsonl"
    if stats.is_file():
        for line in stats.read_text().splitlines():
            try:
                sample = json.loads(line)
                key = sample.get("Name") or sample["Container"]
                peak = peaks.setdefault(
                    key,
                    {"cpu_percent_peak_observed": 0, "memory_bytes_peak_observed": 0},
                )
                peak["cpu_percent_peak_observed"] = max(
                    peak["cpu_percent_peak_observed"],
                    float(sample["CPUPerc"].rstrip("%")),
                )
                peak["memory_bytes_peak_observed"] = max(
                    peak["memory_bytes_peak_observed"],
                    memory_bytes(sample["MemUsage"].split(" / ")[0]),
                )
            except (ValueError, KeyError, TypeError):
                continue
    operational_failures.extend(
        name for name, passed in additional_checks.items() if not passed
    )
    report = {
        "schema_version": "1.0",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed"
        if operational_failures
        else "quality_blocked"
        if quality_failures
        else "passed",
        "operational_failures": operational_failures,
        "quality_failures": sorted(set(quality_failures)),
        "steps": steps,
        "checks": additional_checks,
        "resources": peaks,
        "openai": {
            "real_api_calls": 0,
            "coverage": "mock regression tests and real-PDF dry runs",
        },
        "external_acceptance": {
            "xlam": "not_available",
            "promatool_golden_pdf": "not_available",
            "gpu": "not_run",
            "multilingual_profile": "not_run",
        },
        "evidence_directory": str(root),
        "workbooks_directory": str(parsed),
        "code_sha256": code_digests(scripts_root),
    }
    if first_etl is not None:
        report["catalog_results"] = first_etl.get("results")
        report["catalog_completeness"] = first_etl.get("completeness")
    atomic_json(root / "report.json", report)
    atomic_json(
        logs_root / "full-test-latest.json",
        {
            "run_id": run_id,
            "status": report["status"],
            "report_sha256": digest(root / "report.json"),
            "report": str(root / "report.json"),
        },
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["health", "assets", "baseline", "report"])
    parser.add_argument("--run-id")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.mode in {"baseline", "report"} and (
        not args.run_id
        or Path(args.run_id).name != args.run_id
        or args.run_id in {".", ".."}
    ):
        parser.error("a safe --run-id is required")
    if args.mode == "health":
        value = health(args.timeout)
    elif args.mode == "assets":
        value = assets()
    elif args.mode == "baseline":
        value = {
            "raw_sha256": digest("/parsed/master_raw.xlsx"),
            "code_sha256": code_digests("/scripts"),
        }
        atomic_json(Path("/logs/full-test-runs") / args.run_id / "baseline.json", value)
    else:
        value = summarize(args.run_id)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if args.mode == "report":
        return (
            1
            if value["status"] == "failed"
            else 2
            if value["status"] == "quality_blocked"
            else 0
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
