"""Explicit, resumable Ollama installs with shared, persisted UI progress."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

import local_review as local
import subscription_review as review

ACTIVE = {"queued", "downloading"}


class Conflict(ValueError):
    """A model is reserved by another operation."""


class DeleteError(RuntimeError):
    """Ollama could not complete deletion."""


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Installer:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.removals = set()

    def _save(self, job, root):
        job["updated_at"] = timestamp()
        review.atomic_json(Path(root) / job["id"] / "state.json", job)

    def start(self, url, model, root):
        url = local.validate_url(url)
        local.validate_model(model, "none")
        if model == "none":
            raise ValueError("No model does not need installation.")
        root = Path(root)
        with self.lock:
            if any(server == url for server, _ in self.removals):
                raise Conflict(
                    "A model is being deleted on this server. Try again when it finishes."
                )
            for (folder, _), job in self.jobs.items():
                if folder == root and job["url"] == url and job["status"] in ACTIVE:
                    if job["model"] == model:
                        return copy.deepcopy(job)
                    raise ValueError(
                        "Another model is being installed on this server. Wait for it to finish."
                    )
            job = {
                "id": uuid.uuid4().hex,
                "url": url,
                "model": model,
                "status": "queued",
                "message": "Waiting for the local server",
                "completed": 0,
                "total": 0,
                "percent": None,
                "error": "",
                "started_at": timestamp(),
            }
            self.jobs[(root, job["id"])] = job
            self._save(job, root)
            threading.Thread(target=self._run, args=(job, root), daemon=True).start()
            return copy.deepcopy(job)

    def is_removing(self, url, model):
        with self.lock:
            return (local.validate_url(url), model) in self.removals

    def delete(self, url, model, root, check_unused):
        """Delete via Ollama, reserving the server against concurrent installs."""
        url = local.validate_url(url)
        local.validate_model(model, "none")
        if model == "none":
            raise ValueError("No model has no weights to delete.")
        root = Path(root)
        with self.lock:
            if any(server == url for server, _ in self.removals) or any(
                job["url"] == url and job["status"] in ACTIVE
                for job in self.jobs.values()
            ):
                raise Conflict(
                    "Wait for the model installation or deletion on this server to finish."
                )
            self.removals.add((url, model))
        try:
            # The pipeline checks this reservation when queueing/resuming, so a
            # run cannot claim the model between this check and the HTTP request.
            check_unused(url, model)
            self.list(url, root)
            with local.session(url) as client:
                try:
                    response = client.delete(
                        url + "/api/delete",
                        json={"model": model},
                        timeout=(5, 120),
                        allow_redirects=False,
                    )
                    absent = response.status_code == 404
                    if not absent and (not response.ok or response.is_redirect):
                        local.checked(response)
                except (requests.RequestException, ValueError) as exc:
                    raise DeleteError(str(exc)) from exc
                # Ollama returns an empty successful response, not JSON.
            with self.lock:
                for (folder, _), job in self.jobs.items():
                    if (
                        job["url"] == url
                        and job["model"] == model
                        and job["status"] == "installed"
                    ):
                        job.update(
                            status="deleted",
                            message="Model deleted. Install again to download it.",
                        )
                        self._save(job, folder)
                job = {
                    "id": uuid.uuid4().hex,
                    "url": url,
                    "model": model,
                    "status": "deleted",
                    "message": "Model deleted. Install again to download it.",
                    "completed": 0,
                    "total": 0,
                    "percent": None,
                    "error": "",
                    "started_at": timestamp(),
                }
                self.jobs[(root, job["id"])] = job
                self._save(job, root)
            return {"status": "deleted", "model": model, "already_absent": absent}
        finally:
            with self.lock:
                self.removals.discard((url, model))

    def list(self, url, root):
        url = local.validate_url(url)
        root = Path(root)
        with self.lock:
            for path in sorted(root.glob("*/state.json"), reverse=True)[:100]:
                key = (root, path.parent.name)
                if key in self.jobs:
                    continue
                try:
                    job = json.loads(path.read_text())
                    if job["url"] != url:
                        continue
                    if job["status"] in ACTIVE:
                        job.update(
                            status="interrupted",
                            message="The workbench restarted. Install again to resume the download.",
                        )
                        self._save(job, root)
                    self.jobs[key] = job
                except (OSError, ValueError, KeyError):
                    continue
            return sorted(
                (
                    copy.deepcopy(j)
                    for (folder, _), j in self.jobs.items()
                    if folder == root and j["url"] == url
                ),
                key=lambda j: j["started_at"],
                reverse=True,
            )[:100]

    def _update(self, job, root, **changes):
        with self.lock:
            job.update(changes)
            self._save(job, root)

    def _run(self, job, root):
        url, model = job["url"], job["model"]
        try:
            self._update(
                job, root, status="downloading", message="Checking installed models"
            )
            with local.session(url) as client:
                tags = local.checked(
                    client.get(url + "/api/tags", timeout=10, allow_redirects=False)
                )
                if not any(m.get("name") == model for m in tags.get("models", [])):
                    layers, last_save, success = {}, 0, False
                    with client.post(
                        url + "/api/pull",
                        json={"model": model, "stream": True},
                        stream=True,
                        timeout=(10, 300),
                        allow_redirects=False,
                    ) as response:
                        if not response.ok or response.is_redirect:
                            local.checked(response)
                        for line in response.iter_lines(chunk_size=1):
                            if not line:
                                continue
                            item = json.loads(line)
                            if item.get("error"):
                                raise ValueError(str(item["error"])[:500])
                            if item.get("digest") and item.get("total"):
                                total = max(0, int(item["total"]))
                                layers[item["digest"]] = (
                                    total,
                                    min(total, max(0, int(item.get("completed", 0)))),
                                )
                            total = sum(v[0] for v in layers.values())
                            completed = sum(v[1] for v in layers.values())
                            success = item.get("status") == "success"
                            if time.monotonic() - last_save >= 0.5 or success:
                                self._update(
                                    job,
                                    root,
                                    message=str(
                                        item.get("status", "Downloading weights")
                                    )[:200],
                                    total=total,
                                    completed=completed,
                                    percent=min(99, round(100 * completed / total))
                                    if total
                                    else None,
                                )
                                last_save = time.monotonic()
                        if not success:
                            raise ValueError(
                                "Download ended before Ollama verified the model. Install again to resume."
                            )
                info = local.describe(client, url, model)
                self._update(
                    job,
                    root,
                    status="installed",
                    message="Installed · vision capabilities verified",
                    percent=100,
                    model_info=info,
                )
        except Exception as exc:
            self._update(
                job,
                root,
                status="failed",
                message="Installation failed. Install again to retry.",
                error=str(exc)[:500],
            )
