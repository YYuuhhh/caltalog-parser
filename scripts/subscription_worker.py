#!/usr/bin/env python3
"""Host-only ChatGPT subscription bridge; credentials never enter Docker."""

from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from subscription_review import atomic_json, validate_result, validate_evidence
from model_catalog import REASONING_LEVELS, validate_choice
from refresh_models import refresh as refresh_model_metadata


def environment():
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENAI_", "CODEX_API_")) and k != "CODEX_ACCESS_TOKEN"
    }


def auth_status(binary):
    p = subprocess.run(
        [binary, "login", "status"],
        env=environment(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (
        "chatgpt"
        if p.returncode == 0 and "using ChatGPT" in p.stdout + p.stderr
        else "unavailable"
    )


def command(binary, folder, request):
    if request["reasoning"] not in REASONING_LEVELS:
        raise ValueError("Unsupported reasoning level for an isolated review task.")
    # Ignore user config (MCPs, instructions and model overrides); preserve only
    # the CLI's credential store, and force subscription authentication.
    result = [
        binary,
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(folder),
        "--model",
        request["model"],
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        f'model_reasoning_effort="{request["reasoning"]}"',
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="live"'
        if request["stage"] in ("internet", "mapping", "final")
        and "official_sources" not in request.get("context", {})
        else 'web_search="disabled"',
        "-c",
        "features.multi_agent=false",
        "-c",
        "project_doc_max_bytes=0",
        "--output-schema",
        str(folder / "schema.json"),
        "--output-last-message",
        str(folder / "response.json"),
        "--json",
    ]
    for name in request["images"]:
        image = (folder / name).resolve()
        if image.parent != folder.resolve() or image.suffix != ".png":
            raise ValueError("Invalid evidence image.")
        result += ["--image", str(image)]
    return result + ["-"]


def pipeline_available(url):
    if url is None:
        return True  # Standalone worker and isolated task tests.
    try:
        with urlopen(url.rstrip("/") + "/health", timeout=2) as response:
            state = json.load(response)
            return isinstance(state, dict) and state.get("status") == "healthy"
    except (OSError, ValueError, URLError):
        return False


def run(queue, once=False, pipeline_url=None):
    binary = shutil.which("codex")
    if not binary:
        raise SystemExit(
            "Install Codex CLI and sign in with ChatGPT before starting the worker."
        )
    queue.mkdir(parents=True, exist_ok=True)
    # Only one host consumer may claim tasks, even if make is called twice.
    import fcntl

    with (queue / ".worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A subscription worker is already running.")
        auth = auth_status(binary)
        if auth != "chatgpt":
            raise SystemExit(
                "Subscription review requires codex login with ChatGPT; API-key login is not accepted."
            )
        metadata_mtime = None
        while True:
            ready = pipeline_available(pipeline_url)
            cache = Path.home() / ".codex/models_cache.json"
            if cache.is_file() and cache.stat().st_mtime != metadata_mtime:
                try:
                    refresh_model_metadata(
                        Path(__file__).resolve().parents[1]
                        / "catalogs/library/models.json",
                        cache,
                    )
                    metadata_mtime = cache.stat().st_mtime
                except (ValueError, OSError, KeyError):
                    pass  # Preserve the last valid catalog; inference settings stay pinned.
            atomic_json(
                queue / "worker.json",
                {
                    "heartbeat": time.time(),
                    "auth": auth,
                    "pid": os.getpid(),
                    "pipeline_available": ready,
                    "detail": "ChatGPT subscription · fresh sessions · per-stage reasoning"
                    if ready
                    else "Waiting for the main pipeline to start.",
                },
            )
            tasks = (
                sorted(queue.glob("*/request.json"), key=lambda p: p.stat().st_mtime)
                if ready
                else []
            )
            for path in tasks:
                folder = path.parent.resolve()
                if any(
                    (folder / n).exists()
                    for n in ("result.json", "error.json", "cancelled.json")
                ):
                    continue
                request = json.loads(path.read_text())
                if request.get("provider", "work") != "work":
                    continue
                if request.get("manual_handoff", request.get("work_mode")):
                    continue
                if not pipeline_available(pipeline_url):
                    break
                try:
                    if (
                        not re.fullmatch(r"[a-f0-9]{32}", request["task_id"])
                        or folder.name != request["task_id"]
                    ):
                        raise ValueError("Invalid task identity.")
                    validate_choice(
                        Path(__file__).resolve().parents[1] / "catalogs/library",
                        request["model"],
                        request["reasoning"],
                    )
                    validate_evidence(request, folder)
                    atomic_json(
                        folder / "execution.json",
                        {
                            "provider": "codex_subscription",
                            "model": request["model"],
                            "reasoning": request["reasoning"],
                            "fresh_context": True,
                            "context_sha256": request["context_sha256"],
                        },
                    )
                    with (
                        (folder / "events.jsonl").open("w") as events,
                        (folder / "stderr.log").open("w") as errors,
                    ):
                        process = subprocess.Popen(
                            command(binary, folder, request),
                            stdin=subprocess.PIPE,
                            stdout=events,
                            stderr=errors,
                            text=True,
                            env=environment(),
                            start_new_session=True,
                        )
                        process.stdin.write((folder / "prompt.txt").read_text())
                        process.stdin.close()
                        deadline = time.monotonic() + request.get("timeout", 1800)
                        while process.poll() is None:
                            ready = pipeline_available(pipeline_url)
                            atomic_json(
                                queue / "worker.json",
                                {
                                    "heartbeat": time.time(),
                                    "auth": auth,
                                    "pid": os.getpid(),
                                    "task_id": request["task_id"],
                                    "pipeline_available": ready,
                                },
                            )
                            if (
                                (folder / "cancelled.json").exists()
                                or time.monotonic() > deadline
                                or not ready
                            ):
                                os.killpg(process.pid, signal.SIGTERM)
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    os.killpg(process.pid, signal.SIGKILL)
                                    process.wait()
                                raise RuntimeError(
                                    "The main pipeline went offline; subscription review stopped."
                                    if not ready
                                    else "Subscription review stopped or timed out."
                                )
                            time.sleep(1)
                    if process.returncode:
                        raise RuntimeError(
                            "Codex subscription review failed; check its saved stderr.log. No fallback model or paid API was used."
                        )
                    result = json.loads((folder / "response.json").read_text())
                    validate_result(request, result)
                    events = [
                        json.loads(line)
                        for line in (folder / "events.jsonl").read_text().splitlines()
                        if line.strip()
                    ]
                    searched = sum(
                        e.get("item", {}).get("type") == "web_search" for e in events
                    )
                    if (
                        request["stage"] in ("internet", "mapping", "final")
                        and "official_sources" not in request.get("context", {})
                        and result["status"] == "passed"
                        and not searched
                    ):
                        raise ValueError(
                            "A passed internet check requires an actual live web-search event."
                        )
                    execution = json.loads((folder / "execution.json").read_text())
                    execution["web_search_events"] = searched
                    execution["official_source_snapshot"] = (
                        request.get("context", {})
                        .get("official_sources", {})
                        .get("snapshot_sha256")
                    )
                    atomic_json(folder / "execution.json", execution)
                    atomic_json(folder / "result.json", result)
                except Exception as exc:
                    atomic_json(folder / "error.json", {"error": str(exc)})
            if once:
                break
            time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "logs/review-queue",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--pipeline-url", help="Wait until this workbench is healthy.")
    args = parser.parse_args()
    run(args.queue, args.once, args.pipeline_url)
