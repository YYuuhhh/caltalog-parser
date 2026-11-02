#!/usr/bin/env python3
"""Download CPU review models with resumable Ollama pulls and saved progress."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit
import urllib.request

MODELS = {
    "32": ["qwen3.5:9b", "qwen3-vl:8b-instruct"],
    "750": ["qwen3-vl:235b-a22b-instruct-bf16"],
}
ROOT = Path(__file__).resolve().parents[1]


def backing_storage(url):
    """WSL's virtual free space must be capped by its actual Windows drive."""
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        return None  # A remote Ollama server stores models on its own disks.
    path = ROOT / "data/ollama"
    try:
        ids = subprocess.check_output(
            ["docker", "compose", "ps", "-q", "local-llm"], cwd=ROOT, text=True
        ).strip()
        if ids:
            mounts = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", ids, "--format", "{{json .Mounts}}"],
                    text=True,
                )
            )
            path = Path(
                next(m["Source"] for m in mounts if m["Destination"] == "/root/.ollama")
            )
        available = shutil.disk_usage(path).free
        if os.getenv("WSL_DISTRO_NAME") and not str(path).startswith("/mnt/"):
            distro = os.environ["WSL_DISTRO_NAME"].replace("'", "''")
            script = (
                "Get-ChildItem 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq '"
                + distro
                + "' | ForEach-Object { [regex]::Match($_.BasePath, '[A-Za-z]:').Value }"
            )
            drive = subprocess.check_output(
                [
                    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                text=True,
                timeout=15,
            ).strip()
            if re.fullmatch(r"[A-Za-z]:", drive):
                available = min(
                    available, shutil.disk_usage("/mnt/" + drive[0].lower()).free
                )
        return path / "models/blobs", available
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return None


def required_download(model, blobs):
    fallback = 500_000_000_000 if "235b" in model else 8_000_000_000
    try:
        name, tag = model.split(":", 1)
        with urllib.request.urlopen(
            f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag}", timeout=15
        ) as response:
            layers = json.load(response)["layers"]
        needed = 0
        for layer in layers:
            blob = blobs / layer["digest"].replace(":", "-")
            if blob.is_file() and blob.stat().st_size == layer["size"]:
                continue
            received = 0
            for part in blobs.glob(blob.name + "-partial-*"):
                try:
                    if part.stat().st_size > 16384:
                        continue
                    received += int(json.loads(part.read_text()).get("completed", 0))
                except (OSError, ValueError, AttributeError):
                    continue
            needed += max(0, layer["size"] - received)
        return needed
    except (OSError, ValueError, KeyError):
        return fallback


def run(url, models, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(output.read_text()) if output.is_file() else {}
    for model in models:
        if state.get(model, {}).get("status") == "success":
            continue
        storage = backing_storage(url)
        if storage:
            needed = required_download(model, storage[0]) + 10_000_000_000
            if storage[1] < needed:
                reason = f"Insufficient backing-disk space: need {needed / 1e9:.1f} GB including reserve, available {storage[1] / 1e9:.1f} GB. Configure OLLAMA_DATA_DIR on a larger disk or use the larger machine's Ollama URL."
                state[model] = {
                    **state.get(model, {}),
                    "status": "paused_storage",
                    "reason": reason,
                }
                output.write_text(json.dumps(state, indent=2))
                raise SystemExit(reason)
        for attempt in range(1, 6):
            try:
                print("Pulling " + model, flush=True)
                request = urllib.request.Request(
                    url.rstrip("/") + "/api/pull",
                    data=json.dumps({"model": model, "stream": True}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                last_save = 0
                with urllib.request.urlopen(request, timeout=300) as response:
                    for line in response:
                        item = json.loads(line)
                        item["updated_at"] = datetime.now(timezone.utc).isoformat()
                        if item.get("error"):
                            raise RuntimeError(item["error"])
                        state[model] = item
                        if (
                            time.monotonic() - last_save > 2
                            or item.get("status") == "success"
                        ):
                            temp = output.with_suffix(".tmp")
                            temp.write_text(json.dumps(state, indent=2))
                            temp.replace(output)
                            last_save = time.monotonic()
                if state[model]["status"] != "success":
                    raise RuntimeError(
                        "Ollama pull ended before verification completed."
                    )
                print("Verified download: " + model, flush=True)
                break
            except (OSError, ValueError, RuntimeError) as exc:
                state[model] = {
                    **state.get(model, {}),
                    "status": "retrying" if attempt < 5 else "failed",
                    "error": str(exc),
                    "attempt": attempt,
                }
                output.write_text(json.dumps(state, indent=2))
                print(str(exc), flush=True)
                if attempt == 5:
                    raise
                time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--ram", choices=("32", "750", "all"), default="32")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "logs/local-model-test/downloads.json",
    )
    args = parser.parse_args()
    selected = (
        MODELS[args.ram]
        if args.ram != "all"
        else [m for group in MODELS.values() for m in group]
    )
    run(args.url, selected, args.output)
