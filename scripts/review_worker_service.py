#!/usr/bin/env python3
"""Manage the host review worker alongside the Docker pipeline."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

SERVICE = "catalog-parser-review-worker.service"
ROOT = Path(__file__).resolve().parents[1]


def unit_quote(value, command=False):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("Service paths cannot contain newlines.")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def pipeline_url(root):
    port = os.environ.get("PIPELINE_UI_PORT")
    env = root / ".env"
    if not port and env.is_file():
        for line in env.read_text().splitlines():
            name, _, value = line.strip().partition("=")
            if name == "PIPELINE_UI_PORT":
                port = value.strip().strip("\"'")
                break
    port = int(port or 7861)
    if not 1 <= port <= 65535:
        raise ValueError("PIPELINE_UI_PORT must be between 1 and 65535.")
    return f"http://127.0.0.1:{port}"


def unit_text(root, python, binary, url):
    path = str(Path(binary).parent) + os.pathsep + os.defpath
    command = [
        python,
        "-u",
        str(root / "scripts/subscription_worker.py"),
        "--queue",
        str(root / "logs/review-queue"),
        "--pipeline-url",
        url,
    ]
    credential_home = os.environ.get("CODEX_HOME")
    return "\n".join(
        [
            "[Unit]",
            "Description=Catalog parser host subscription reviewer",
            "[Service]",
            "Type=simple",
            "WorkingDirectory=" + str(root).replace("%", "%%"),
            "Environment=" + unit_quote("PATH=" + path),
            *(
                ["Environment=" + unit_quote("CODEX_HOME=" + credential_home)]
                if credential_home
                else []
            ),
            "ExecStart=" + " ".join(unit_quote(arg, command=True) for arg in command),
            "Restart=on-failure",
            "RestartSec=5",
            "KillMode=control-group",
            "TimeoutStopSec=10",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def manage(action, root=ROOT):
    if not shutil.which("systemctl"):
        raise RuntimeError(
            "Enable systemd in WSL/Linux to manage the host review service."
        )
    unit = Path.home() / ".config/systemd/user" / SERVICE
    if action in ("start", "restart"):
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("Install Codex CLI and run codex login on the host.")
        missing = [
            name
            for name in ("requests", "jsonschema")
            if not importlib.util.find_spec(name)
        ]
        if missing:
            raise RuntimeError(
                "Install host Python dependencies: " + ", ".join(missing)
            )
        content = unit_text(root.resolve(), sys.executable, binary, pipeline_url(root))
        changed = not unit.is_file() or unit.read_text() != content
        if changed:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE], check=True)
        operation = "restart" if changed or action == "restart" else "start"
        subprocess.run(["systemctl", "--user", operation, SERVICE], check=True)
        print("Host review worker started; it waits while the pipeline is offline.")
    elif action == "stop":
        if unit.is_file():
            subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)
    elif action == "status":
        if unit.is_file():
            subprocess.run(["systemctl", "--user", "status", "--no-pager", SERVICE])
        else:
            print("Host review service is not installed; make start installs it.")
    elif action == "logs":
        subprocess.run(
            ["journalctl", "--user", "-u", SERVICE, "-n", "100", "--no-pager"],
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("start", "stop", "restart", "status", "logs")
    )
    args = parser.parse_args()
    try:
        manage(args.action)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
