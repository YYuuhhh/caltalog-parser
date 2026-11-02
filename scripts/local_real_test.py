#!/usr/bin/env python3
"""Run a real CPU extraction/review/validation test on a dimensioned catalog page."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, data=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main(args):
    folder = (
        ROOT
        / "logs/local-model-test"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True)
    protected = [
        ROOT / "parsed/master_raw.xlsx",
        ROOT / "catalogs/manifests/guhring-production.yaml",
        ROOT / "mappings/guhring.yaml",
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    wanted = ["qwen3-vl:8b-instruct", "qwen3.5:9b"]
    deadline = time.monotonic() + args.timeout
    print(
        "Waiting for verified local model downloads; no synthetic answers are used.",
        flush=True,
    )
    while True:
        installed = {m["name"] for m in request(args.ollama, "/api/tags")["models"]}
        if wanted[0] in installed:
            break
        if time.monotonic() > deadline:
            raise TimeoutError("Model downloads are still incomplete.")
        time.sleep(5)
    queue = request(args.url, "/api/queue")
    if queue["active_run"] or queue["queued_runs"]:
        raise RuntimeError(
            "Wait for the existing catalog queue before starting this test."
        )
    config = request(args.url, "/api/bootstrap")["defaults"]
    config.update(
        source_pdf=args.source,
        review_provider="local",
        final_provider="local",
        model=wanted[0],
        final_model=wanted[1],
        reasoning="none",
        final_reasoning="none",
        automatic_review=True,
        local_use_gpu=False,
        local_keep_loaded=not args.release_extractor,
        page_selection=True,
        first_page=4,
        last_page=4,
        local_context_tokens=32768,
        local_output_tokens=4096,
        review_batch_rows=60,
        review_dpi=120,
        max_quality_passes=1,
        review_timeout=args.timeout,
        prepare_manual_review=True,
        local_source_urls="https://guhring.com/media/tools/6717.pdf https://guhring.com/media/tools/6719.pdf",
    )
    pre_released = False
    if args.resume:
        if not re.fullmatch(r"[a-f0-9]{32}", args.resume):
            raise ValueError("Resume needs a saved run ID.")
        previous = request(args.url, "/api/runs/" + args.resume)
        if (
            previous["status"] not in ("failed", "interrupted", "cancelled")
            or previous["config"].get("review_provider") != "local"
            or previous["config"].get("final_provider") != "local"
        ):
            raise ValueError("Resume a stopped local CPU test only.")
        (folder / "before-resume.json").write_text(json.dumps(previous, indent=2))
        saved_path = ROOT / "logs/ui-runs" / args.resume / "state.json"
        saved = json.loads(saved_path.read_text())
        saved["config"]["local_keep_loaded"] = not args.release_extractor
        temporary = saved_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
        temporary.replace(saved_path)
        config = saved["config"]
        if args.release_extractor:
            subprocess.run(
                ["docker", "compose", "stop", "mineru"], cwd=ROOT, check=True
            )
            pre_released = True
        state = request(args.url, "/api/runs/" + args.resume + "/resume", {})
    else:
        state = request(args.url, "/api/runs", config)
    (folder / "config.json").write_text(json.dumps(config, indent=2))
    job = state["id"]
    (folder / "run-id.txt").write_text(job)
    print("Real local CPU run: " + job, flush=True)
    released = pre_released
    samples = []
    last = None
    try:
        while True:
            state = request(args.url, "/api/runs/" + job)
            summary = (
                state["status"],
                state.get("current_stage"),
                len(state["review_tasks"]),
            )
            if summary != last:
                print(str(summary), flush=True)
                last = summary
            if (
                args.release_extractor
                and not released
                and any(
                    s["id"] == "parse" and s["status"] == "completed"
                    for s in state["stages"]
                )
            ):
                subprocess.run(
                    ["docker", "compose", "stop", "mineru"], cwd=ROOT, check=True
                )
                released = True
                print(
                    "Extraction saved; released idle extractor RAM for CPU review.",
                    flush=True,
                )
            running = request(args.ollama, "/api/ps")
            for model in running.get("models", []):
                samples.append(
                    {
                        "name": model["name"],
                        "size_vram": model.get("size_vram"),
                        "size": model.get("size"),
                    }
                )
                if model.get("size_vram", 0):
                    raise RuntimeError("The test unexpectedly allocated GPU memory.")
            (folder / "state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2)
            )
            if state["status"] in (
                "completed",
                "quality_blocked",
                "failed",
                "cancelled",
                "interrupted",
            ):
                break
            if time.monotonic() > deadline:
                request(args.url, f"/api/runs/{job}/cancel", {})
                raise TimeoutError("The real CPU test exceeded its time budget.")
            time.sleep(3)
    finally:
        if released:
            for model in request(args.ollama, "/api/ps").get("models", []):
                request(
                    args.ollama,
                    "/api/generate",
                    {"model": model["name"], "keep_alive": 0, "stream": False},
                )
            subprocess.run(
                ["docker", "compose", "up", "-d", "mineru"], cwd=ROOT, check=True
            )
    executions = []
    for task in state["review_tasks"]:
        path = ROOT / "logs/review-queue" / task["id"] / "execution.json"
        if path.exists():
            result_path = path.with_name("result.json")
            result_status = (
                json.loads(result_path.read_text())["status"]
                if result_path.exists()
                else None
            )
            executions.append(
                {
                    "stage": task["stage"],
                    "result_status": result_status,
                    **json.loads(path.read_text()),
                }
            )
    report = {
        "dry_run": False,
        "run_id": job,
        "source": args.source,
        "scope": "physical PDF page 4, including dimensioned drawings and two tool tables",
        "status": state["status"],
        "error": state.get("error"),
        "cpu_samples": samples,
        "executions": executions,
        "work_calls": 0,
        "final_model_executed": any(
            e["stage"] == "final"
            and e["provider"] == "ollama_local"
            and e["result_status"] is not None
            for e in executions
        ),
        "inputs_unchanged": all(
            hashlib.sha256(p.read_bytes()).hexdigest() == before[str(p)]
            for p in protected
        ),
        "note": "This is a real selected-page test; unresolved findings and partial scope must not be called full-catalog validation.",
    }
    (folder / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print("Real-test report: " + str(folder / "report.json"), flush=True)
    return (
        0
        if report["final_model_executed"]
        and state["status"] in ("completed", "quality_blocked")
        and report["inputs_unchanged"]
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7861")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--source", default="/catalogs/imports/guhring/CFK_Milling_Cutters_2013.pdf"
    )
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument(
        "--resume", help="Resume unfinished stages of a stopped local CPU test."
    )
    parser.add_argument(
        "--release-extractor",
        action="store_true",
        help="Release idle extractor RAM after parsing on small WSL instances; restored after the test.",
    )
    raise SystemExit(main(parser.parse_args()))
