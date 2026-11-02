#!/usr/bin/env python3
"""Run regressions and a real CPU probe. Work generation is opt-in only."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def request(base, endpoint, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base + endpoint, data=body, headers={"Content-Type": "application/json"}
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def run(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "logs/workbench-tests" / stamp
    output.mkdir(parents=True)
    preserved = [
        ROOT / "parsed/master_raw.xlsx",
        ROOT / "catalogs/manifests/guhring-production.yaml",
        ROOT / "mappings/guhring.yaml",
    ]
    before = {str(p): digest(p) for p in preserved}
    results = {}
    for label, command in [
        (
            "python",
            [
                "docker",
                "compose",
                "run",
                "--rm",
                "--no-deps",
                "tools",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/tests",
                "/mineru-src/tests",
            ],
        ),
        ("browser", ["node", "tests/ui_e2e.cjs"]),
    ]:
        print("Checking " + label, flush=True)
        env = dict(os.environ)
        env.setdefault("NODE_PATH", "/tmp/catalog-ui-browser/node_modules")
        private_libraries = Path(
            "/tmp/catalog-ui-browser/deps/root/usr/lib/x86_64-linux-gnu"
        )
        if private_libraries.is_dir():
            env["LD_LIBRARY_PATH"] = str(private_libraries) + (
                ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
            )
            env["PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS"] = "1"
        with (output / f"{label}.log").open("w") as log:
            code = subprocess.run(
                command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
            ).returncode
        results[label] = {"exit_code": code, "log": str(output / f"{label}.log")}
        if code:
            raise SystemExit(f"{label} checks failed. See {output / (label + '.log')}")
    bootstrap = request(args.url, "/api/bootstrap")
    if not args.allow_work_generation:
        source = next(
            (c for c in bootstrap["catalogs"] if c["path"] == args.source), None
        )
        if source is None:
            raise SystemExit("The test source was not found in the catalog inventory.")
        first = args.first_page or min(7, source["pages"])
        last = args.last_page or first
        command = [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "tools",
            "python",
            "/scripts/local_workbench_probe.py",
            "--source",
            args.source,
            "--first-page",
            str(first),
            "--last-page",
            str(last),
        ]
        print("Real CPU probe; no ChatGPT Work generation", flush=True)
        with (output / "cpu.log").open("w") as log:
            code = subprocess.run(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
            ).returncode
        results["cpu"] = {"exit_code": code, "log": str(output / "cpu.log")}
        unchanged = all(digest(p) == before[str(p)] for p in preserved)
        report = {
            "checks": results,
            "production_inputs_unchanged": unchanged,
            "work_generation_calls": 0,
            "platform_generation_calls": 0,
            "scope": {
                "first_page": first,
                "last_page": last,
                "total_pages": source["pages"],
            },
            "note": "Review responses in regression fixtures are simulated. Real model validation was not performed.",
        }
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Report: " + str(output / "report.json"), flush=True)
        return 0 if code == 0 and unchanged else 1
    if not bootstrap["reviewer"]["available"]:
        raise SystemExit(
            "Start make review-worker with ChatGPT subscription authentication before the real catalog test."
        )
    config = bootstrap["defaults"]
    config.update(
        automatic_review=True,
        review_provider="work",
        final_provider="work",
        reasoning="medium",
        final_reasoning="medium",
        model="gpt-6-sol",
        final_model="gpt-6-astra",
    )
    config["source_pdf"] = args.source
    if args.first_page is not None:
        config.update(
            page_selection=True, first_page=args.first_page, last_page=args.last_page
        )
    run = request(args.url, "/api/runs", config)
    job = run["id"]
    print("Real CPU + subscription review run: " + job, flush=True)
    last = None
    try:
        while True:
            state = request(args.url, "/api/runs/" + job)
            summary = (
                state["status"],
                next(
                    (s["title"] for s in state["stages"] if s["status"] == "running"),
                    "",
                ),
                len(state["review_tasks"]),
            )
            if summary != last:
                print(str(summary), flush=True)
                last = summary
            if state["status"] in (
                "completed",
                "quality_blocked",
                "failed",
                "cancelled",
                "interrupted",
            ):
                break
            time.sleep(3)
    except KeyboardInterrupt:
        request(args.url, f"/api/runs/{job}/cancel", {})
        raise
    unchanged = all(digest(p) == before[str(p)] for p in preserved)
    report = {
        "checks": results,
        "run": state,
        "production_inputs_unchanged": unchanged,
        "platform_api_calls": 0,
        "scope": "selected pages" if config["page_selection"] else "full catalog",
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print("Report: " + str(output / "report.json"), flush=True)
    if not unchanged or state["status"] in ("failed", "cancelled", "interrupted"):
        return 1
    return 3 if state["status"] == "quality_blocked" else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7861")
    parser.add_argument(
        "--source", default="/catalogs/input/GTC_Milling_Cutters 2018.pdf"
    )
    parser.add_argument("--first-page", type=int)
    parser.add_argument("--last-page", type=int)
    parser.add_argument(
        "--allow-work-generation",
        action="store_true",
        help="Explicitly allow real reviews to consume ChatGPT Work allowance.",
    )
    raise SystemExit(run(parser.parse_args()))
