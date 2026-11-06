#!/usr/bin/env python3
"""Optional local-only OCR worker. Downloads only after an explicit install action."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import uuid
from pydantic import BaseModel, Field

import system_diagnostics as diagnostics
import baidu_cpu_runtime as runtime

ROOT = Path(os.getenv("BAIDU_OCR_DATA", "/models"))
LOCK = threading.RLock()
ACTIVE = {}


class Input(BaseModel):
    image: str = Field(max_length=24_000_000)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_tokens: int = Field(2048, ge=256, le=8192)
    threads: int = Field(0, ge=0, le=4096)


def worker(job):
    folder = ROOT / "jobs" / job
    request = diagnostics.read_json(folder / "request.json")
    try:
        runtime.infer(
            ROOT,
            folder / "source.png",
            folder / "result.json",
            request["max_tokens"],
            request["threads"],
        )
    except Exception as exc:
        diagnostics.atomic(folder / "error.json", {"error": str(exc)})
        raise


def app_factory():
    from fastapi import FastAPI, HTTPException
    from PIL import Image

    app = FastAPI(title="CatalogFlow experimental Baidu CPU OCR")
    runtime.verify_dependencies()
    ROOT.mkdir(parents=True, exist_ok=True)
    if (
        diagnostics.read_json(ROOT / "install-state.json", {}).get("status")
        == "installing"
    ):
        diagnostics.atomic(
            ROOT / "install-state.json",
            {
                "status": "interrupted",
                "error": "Service restarted during download; Install resumes cached files.",
            },
        )

    @app.get("/health")
    def health():
        try:
            runtime.verify(ROOT)
            installed = True
        except (OSError, ValueError, KeyError):
            installed = False
        state = diagnostics.read_json(ROOT / "install-state.json", {})
        return {
            "model": runtime.MODEL,
            "revision": runtime.REVISION,
            "device": "cpu",
            "experimental": True,
            "installed": installed,
            "installation": state,
        }

    @app.post("/install", status_code=202)
    def install():
        def download():
            try:
                runtime.prepare(ROOT)
                diagnostics.atomic(ROOT / "install-state.json", {"status": "completed"})
            except Exception as exc:
                diagnostics.atomic(
                    ROOT / "install-state.json", {"status": "failed", "error": str(exc)}
                )

        with LOCK:
            state = diagnostics.read_json(ROOT / "install-state.json", {})
            if state.get("status") == "installing":
                return state
            diagnostics.atomic(ROOT / "install-state.json", {"status": "installing"})
            threading.Thread(target=download, daemon=True).start()
        return {"status": "installing"}

    @app.post("/ocr", status_code=202)
    def ocr(request: Input):
        if not health()["installed"]:
            raise HTTPException(409, "Install the pinned Baidu CPU model first.")
        try:
            source = base64.b64decode(request.image, validate=True)
            if hashlib.sha256(source).hexdigest() != request.source_sha256:
                raise ValueError("Image SHA256 does not match.")
            with Image.open(io.BytesIO(source)) as image:
                if max(image.size) > 5000 or min(image.size) < 16:
                    raise ValueError(
                        "OCR page dimensions must be between 16 and 5000 pixels."
                    )
                image.verify()
        except Exception as exc:
            raise HTTPException(400, str(exc))
        with LOCK:
            if any(p.poll() is None for p in ACTIVE.values()):
                raise HTTPException(409, "A CPU OCR task is already running.")
            job = uuid.uuid4().hex
            folder = ROOT / "jobs" / job
            folder.mkdir(parents=True)
            (folder / "source.png").write_bytes(source)
            diagnostics.atomic(
                folder / "request.json", request.model_dump(exclude={"image"})
            )
            diagnostics.atomic(
                folder / "progress.json", {"status": "queued", "device": "cpu"}
            )
            with (folder / "worker.log").open("w") as log:
                ACTIVE[job] = subprocess.Popen(
                    [sys.executable, __file__, "--run", job],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        return {"id": job, "status": "queued"}

    def folder_for(job):
        if not re.fullmatch(r"[a-f0-9]{32}", job) or not (ROOT / "jobs" / job).is_dir():
            raise HTTPException(404, "OCR task not found.")
        return ROOT / "jobs" / job

    @app.get("/jobs/{job}")
    def progress(job: str):
        folder = folder_for(job)
        result = diagnostics.read_json(folder / "result.json")
        error = diagnostics.read_json(folder / "error.json")
        with LOCK:
            process = ACTIVE.get(job)
            if (
                not result
                and not error
                and (process is None or process.poll() is not None)
            ):
                error = {
                    "error": "OCR worker stopped or service restarted; inspect worker.log."
                }
        return {
            "id": job,
            "status": "completed" if result else "failed" if error else "running",
            "result": result,
            "error": error.get("error") if error else None,
            "progress": diagnostics.read_json(folder / "progress.json", {}),
        }

    @app.delete("/jobs/{job}")
    def cancel(job: str):
        folder = folder_for(job)
        with LOCK:
            process = ACTIVE.get(job)
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(5)
        diagnostics.atomic(
            folder / "error.json", {"error": "CPU OCR stopped by user or timeout."}
        )
        return {"status": "cancelled"}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run")
    args = parser.parse_args()
    if args.run:
        worker(args.run)
    else:
        import uvicorn

        uvicorn.run(app_factory(), host="0.0.0.0", port=8095)
