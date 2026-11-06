#!/usr/bin/env python3
"""CPU catalog workbench: manufacturer library, serial queue and mandatory review."""

from __future__ import annotations
import copy
import errno
import json
import asyncio
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
import io
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

import catalog_registry as registry
import extraction_quality as quality
import manufacturer_library as ml
import subscription_review as review
import catalog_detection as detection
import model_catalog as models
import workbench_outputs as outputs
import review_notes
import processing_control as control
import local_review as local
import local_model_install

ROOTS = {
    name: Path(os.getenv(f"PIPELINE_{name.upper()}_ROOT", f"/{name}"))
    for name in ("catalogs", "parsed", "logs", "mappings", "schemas")
}
SCRIPTS = Path(__file__).resolve().parent
UI = Path(os.getenv("PIPELINE_UI_ASSETS", "/ui"))
STAGES = [
    ("catalog", "Register"),
    ("parse", "Extract"),
    ("raw", "RAW export"),
    ("correctness", "Page correctness"),
    ("completeness", "Page completeness"),
    ("internet", "Internet verification"),
    ("mapping", "Manufacturer mapping"),
    ("structure", "Structure"),
    ("duplicates", "Compare duplicates"),
    ("final", "Final tool validation"),
    ("validate", "Workbook integrity"),
]
STOP_STATUSES = {"failed", "cancelled", "interrupted", "paused", "pausing", "stopping"}


def now():
    return datetime.now(timezone.utc).isoformat()


def library():
    return ml.Library(ROOTS["catalogs"] / "library", ROOTS["mappings"])


def queue():
    return ROOTS["logs"] / "review-queue"


def restricted_path(value, roots, suffix=None):
    path = Path(value).resolve()
    if not any(path.is_relative_to(ROOTS[root].resolve()) for root in roots):
        raise ValueError(f"Choose a file inside {', '.join(roots)}.")
    if not path.is_file() or suffix and path.suffix.lower() != suffix:
        raise ValueError("File unavailable or has the wrong type: " + path.name)
    return path


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_pdf: str = ""
    manufacturer_id: str = "guhring"
    revision: str = Field("unknown", min_length=1, max_length=100)
    languages: str = Field("en,ru,zh", min_length=1, max_length=100)
    families: str = Field("milling_cutters", min_length=1, max_length=300)
    page_selection: bool = False
    first_page: int = Field(1, ge=1)
    last_page: int | None = Field(None, ge=1)
    extraction_model: Literal["mineru-3.4.5-pipeline"] = "mineru-3.4.5-pipeline"
    method: Literal["auto", "txt", "ocr"] = "auto"
    parser_language: Literal[
        "ch",
        "ch_server",
        "korean",
        "ta",
        "te",
        "ka",
        "th",
        "el",
        "arabic",
        "east_slavic",
        "cyrillic",
        "devanagari",
    ] = "ch"
    tables: bool = True
    formulas: bool = True
    automatic_quality: bool = True
    confidence_threshold: float = Field(0.9, ge=0.5, le=1, allow_inf_nan=False)
    max_quality_passes: int = Field(2, ge=1, le=3)
    force_parse: bool = False
    parse_timeout: int = Field(7200, ge=10, le=86400)
    mineru_url: str = "http://mineru:8000"
    review_provider: Literal["local", "work"] = "local"
    final_provider: Literal["local", "work"] = "local"
    model: str = Field("qwen3-vl:8b-instruct", max_length=160)
    reasoning: models.ReasoningEffort = "none"
    final_model: str = Field("qwen3.5:9b", max_length=160)
    final_reasoning: models.ReasoningEffort = "medium"
    local_url: str = "http://local-llm:11434"
    local_ram_gb: Literal[32, 128, 750] = 32
    local_context_tokens: int = Field(32768, ge=16384, le=131072)
    local_output_tokens: int = Field(4096, ge=512, le=16384)
    local_use_gpu: Literal[False] = False
    local_keep_loaded: bool = True
    local_preload: bool = True
    local_load_timeout: int = Field(7200, ge=10, le=86400)
    local_load_into_ram: bool = True
    local_temperature: float = Field(0.2, ge=0, le=2, allow_inf_nan=False)
    local_source_urls: str = Field("", max_length=4000)
    local_search_url: str = ""
    automatic_review: bool = True
    prepare_manual_review: bool = False
    review_timeout: int = Field(1800, ge=30, le=86400)
    review_dpi: int = Field(180, ge=100, le=300)
    review_batch_rows: int = Field(60, ge=10, le=100)
    force_structure: bool = False

    @model_validator(mode="after")
    def dependencies(self):
        if (
            self.page_selection
            and self.last_page is not None
            and self.last_page < self.first_page
        ):
            raise ValueError("Last page must be greater than or equal to first page.")
        url = urlsplit(self.mineru_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "MinerU URL must be HTTP(S), without credentials, queries or fragments."
            )
        if not self.languages.strip() or not self.families.strip():
            raise ValueError("Specify at least one language and tool family.")
        model_root = ROOTS["catalogs"] / "library"
        local.validate_url(self.local_url)
        if self.local_search_url:
            local.validate_url(self.local_search_url)
        if any(not review.valid_source(u) for u in self.local_source_urls.split()):
            raise ValueError("Official source URLs must use HTTPS without credentials.")
        if self.model != "none":
            if self.review_provider == "work":
                models.validate_choice(model_root, self.model, self.reasoning)
            else:
                local.validate_model(self.model, self.reasoning)
        if self.final_model != "none":
            if self.final_provider == "work":
                models.validate_choice(
                    model_root, self.final_model, self.final_reasoning
                )
            else:
                local.validate_model(self.final_model, self.final_reasoning)
        return self

    def local_settings(self):
        return {
            "url": self.local_url,
            "ram_gb": self.local_ram_gb,
            "context_tokens": self.local_context_tokens,
            "output_tokens": self.local_output_tokens,
            "use_gpu": self.local_use_gpu,
            "keep_loaded": self.local_keep_loaded,
            "load_timeout": self.local_load_timeout,
            "load_into_ram": self.local_load_into_ram,
            "temperature": self.local_temperature,
            "source_urls": self.local_source_urls,
            "search_url": self.local_search_url,
        }

    def bounds(self, total):
        return (
            (self.first_page - 1, (self.last_page or total) - 1)
            if self.page_selection
            else (0, total - 1)
        )


def validate_settings(config):
    source = restricted_path(config.source_pdf, ("catalogs",), ".pdf")
    pages = len(PdfReader(source).pages)
    first, last = config.bounds(pages)
    if first > last or last >= pages:
        raise ValueError(f"Selected page range exceeds the PDF ({pages} pages).")
    if config.languages == "auto":
        metadata = detection.inspect(
            source,
            library().manufacturers(),
            ROOTS["catalogs"] / "library/detections",
            allow_ocr=False,
        )
        config.languages = ",".join(metadata["language"]["codes"]) or "und"
    mapping = library().mapping(config.manufacturer_id)
    registry.validate_manifest(mapping, ROOTS["schemas"] / "field-mapping.schema.json")
    ml.validate_mapping(mapping)
    return source, pages, mapping


def parse_command(config, folder, output, first, last, method=None):
    args = [
        sys.executable,
        str(SCRIPTS / "parse_catalogs.py"),
        "--manifest",
        str(folder / "catalog.yaml"),
        "--schema",
        str(ROOTS["schemas"] / "catalog-manifest.schema.json"),
        "--output",
        str(output / "downloads"),
        "--logs-root",
        str(folder / "parsing"),
        "--api-url",
        config.mineru_url,
        "--backend",
        "pipeline",
        "--language",
        config.parser_language,
        "--method",
        method or config.method,
        "--start-page",
        str(first),
        "--end-page",
        str(last),
        "--timeout",
        str(config.parse_timeout),
        "--control-file",
        str(folder / "control.json"),
        "--queue-control-file",
        str(ROOTS["logs"] / "queue-control.json"),
        "--resume-completed",
    ]
    for enabled, flag in (
        (config.force_parse, "--force"),
        (not config.tables, "--no-tables"),
        (not config.formulas, "--no-formula"),
    ):
        if enabled:
            args.append(flag)
    return args


def etl_command(config, folder, output):
    args = [
        sys.executable,
        str(SCRIPTS / "catalog_etl.py"),
        "--raw",
        str(output / "master_raw.xlsx"),
        "--catalog-manifest",
        str(folder / "catalog.yaml"),
        "--mapping",
        str(folder / "mapping.yaml"),
        "--manifest-schema",
        str(ROOTS["schemas"] / "catalog-manifest.schema.json"),
        "--mapping-schema",
        str(ROOTS["schemas"] / "field-mapping.schema.json"),
        "--decision-schema",
        str(ROOTS["schemas"] / "review-decision.schema.json"),
        "--decisions",
        str(folder / "decisions.jsonl"),
        "--output-root",
        str(output / "etl"),
        "--publish-root",
        str(output / "etl/latest"),
        "--logs-root",
        str(folder / "etl"),
        "--allow-partial-inventory",
    ]
    if config.force_structure:
        args.append("--force")
    return args


def review_enabled(config, stage):
    if stage not in review.CHECKS:
        return True
    return (config.final_model if stage == "final" else config.model) != "none"


def compile_stages(config, run_id, pages):
    folder = ROOTS["logs"] / "ui-runs" / run_id
    output = ROOTS["parsed"] / "ui-runs" / run_id
    commands = {
        "parse": [parse_command(config, folder, output, *config.bounds(pages))],
        "raw": [
            [
                sys.executable,
                str(SCRIPTS / "export_raw.py"),
                "--input-root",
                str(output / "downloads"),
                "--output",
                str(output / "master_raw.xlsx"),
            ]
        ],
        "structure": [etl_command(config, folder, output)],
        "validate": [
            [
                sys.executable,
                str(SCRIPTS / "validate_parsing.py"),
                "--workbook",
                str(output / "master_raw.xlsx"),
                "--parsed-root",
                str(output / "downloads"),
                "--catalogs-root",
                str(ROOTS["catalogs"]),
                "--logs-root",
                str(folder / "raw-validation"),
            ],
            etl_command(config, folder, output) + ["--validate-only"],
        ],
    }
    return [
        {
            "id": id,
            "title": title,
            "enabled": review_enabled(config, id),
            "commands": commands.get(id, []),
        }
        for id, title in STAGES
    ]


def redact(text):
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 4
            and (name.endswith("_KEY") or name.endswith("_PASSWORD"))
        ):
            text = text.replace(value, "[redacted]")
    return text


def workbook_rows(path, sheet):
    if not path.is_file():
        return []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        names = (
            [
                name
                for name in workbook.sheetnames
                if name == "raw" or name.startswith("raw_")
            ]
            if sheet == "raw"
            else [sheet]
        )
        result = []
        for name in names:
            iterator = workbook[name].iter_rows(values_only=True)
            headers = next(iterator)
            result += [dict(zip(headers, row)) for row in iterator]
        return result
    finally:
        workbook.close()


def review_cell(row, compact=False):
    cell = {
        field: row.get(field)
        for field in (
            "raw_parameter_name",
            "raw_value",
            "article_number",
            "table_index",
            "row_index",
            "column_index",
            "header_path",
            "cell_tag",
            "rowspan",
            "colspan",
            "bbox",
            "bbox_scope",
            "bbox_units",
        )
    }
    if compact:
        if cell.get("bbox_scope") == "table":
            for key in ("bbox", "bbox_scope", "bbox_units"):
                cell.pop(key, None)  # Preserved once in table_regions instead.
        cell = {
            k: v
            for k, v in cell.items()
            if v is not None and not (k in ("rowspan", "colspan") and v == 1)
        }
    return cell


def duplicate_context(duplicate):
    def side(value):
        product = value["product"]
        return {
            "catalog_id": value["catalog_id"],
            "manufacturer": product.get("manufacturer"),
            "name": product.get("order_number") or product.get("article_number"),
            "identity_status": product.get("identity_status"),
            "parameters": [
                {
                    field: attribute.get(field)
                    for field in (
                        "canonical_field",
                        "value_type",
                        "value_text",
                        "value_decimal",
                        "value_min",
                        "value_max",
                        "canonical_unit",
                        "fit_class",
                        "tolerance_lower",
                        "tolerance_upper",
                        "review_status",
                        "raw_parameter_name",
                        "mapping_rule_id",
                    )
                }
                for attribute in product.get("attributes", [])
            ],
        }

    return {
        "kind": duplicate["kind"],
        "left": side(duplicate["left"]),
        "right": side(duplicate["right"]),
    }


def bounded_chunks(records, max_rows, budget=26000):
    chunks, chunk, size = [], [], 0
    for record in records:
        slim = review_cell(record) if "raw_value" in record else record
        length = len(json.dumps(slim, ensure_ascii=False))
        if length > budget:
            raise ValueError(
                "One record exceeds the review context limit; split its evidence before reviewing."
            )
        if chunk and (len(chunk) >= max_rows or size + length > budget):
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(record)
        size += length
    if chunk:
        chunks.append(chunk)
    return chunks or [[]]


class Jobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.active = None
        self.process = None
        self.cancelled = threading.Event()
        self.pending = deque()
        self.worker = None
        self.model_loader = None

    def control_file(self, job):
        return self.folder(job) / "control.json"

    def queue_file(self):
        return ROOTS["logs"] / "queue-control.json"

    def checkpoint(self, job):
        return control.checkpoint(self.control_file(job), self.queue_file())

    def queue_status(self):
        with self.lock:
            paused = bool(control.read(self.queue_file()).get("paused"))
            return {
                "paused": paused,
                "active_run": self.active,
                "queued_runs": list(self.pending),
                "status": (
                    self.read(self.active)["status"]
                    if self.active
                    else "paused"
                    if paused
                    else "idle"
                ),
            }

    def pause_queue(self, paused=True):
        with self.changed:
            review.atomic_json(
                self.queue_file(), {"paused": paused, "requested_at": now()}
            )
            self.changed.notify_all()
            return self.queue_status()

    def folder(self, job):
        if not re.fullmatch(r"[a-f0-9]{32}", job):
            raise HTTPException(404, "Run not found")
        return ROOTS["logs"] / "ui-runs" / job

    def read(self, job):
        try:
            state = json.loads((self.folder(job) / "state.json").read_text())
        except FileNotFoundError:
            raise HTTPException(404, "Run not found")
        if state["status"] in ("running", "awaiting_review") and self.active != job:
            state.update(
                status="interrupted",
                error="The workbench stopped or restarted during this run. Saved progress can be resumed.",
            )
        if (
            state["status"] == "queued"
            and job != self.active
            and job not in self.pending
        ):
            state.update(
                status="interrupted",
                error="The workbench stopped or restarted before this queued catalog began. Resume the saved run to requeue it.",
            )
        individual = control.read(self.control_file(job))
        queued_pause = bool(control.read(self.queue_file()).get("paused")) and (
            job == self.active or job in self.pending
        )
        state["paused_individually"] = bool(individual.get("paused"))
        state["paused_by_queue"] = queued_pause
        if (
            job == self.active
            and self.cancelled.is_set()
            and state["status"]
            not in ("completed", "quality_blocked", "cancelled", "failed")
        ):
            state["status"] = "stopping"
        elif individual.get("paused") or queued_pause:
            if state["status"] in (
                "running",
                "awaiting_review",
                "queued",
                "interrupted",
                "paused",
            ):
                state["status"] = (
                    "pausing"
                    if job == self.active
                    and not (self.folder(job) / "paused.json").exists()
                    else "paused"
                )
                state.pop("error", None)
        if state["status"] in STOP_STATUSES:
            state["stoppage"] = self.stoppage_info(job, state)
        else:
            state.pop("stoppage", None)
        state["files"] = self.files(job)
        state["confidence"] = outputs.confidence(state)
        self.extraction_progress(job, state)
        try:
            state["model_load"] = json.loads(
                (self.folder(job) / "model-load/state.json").read_text()
            )
        except (OSError, ValueError):
            pass
        return state

    def start_model_load(self, job, config, model):
        if not config.local_preload or not config.local_keep_loaded or model == "none":
            return
        with self.lock:
            current = self.model_loader
            if current and current["job"] == job and current["model"] == model:
                return
            if current and current["process"].poll() is None:
                raise RuntimeError(
                    "Another local model is still loading. Only one model may load at a time."
                )
            folder = self.folder(job) / "model-load"
            review.atomic_json(
                folder / "request.json",
                {"model": model, "local": config.local_settings()},
            )
            review.atomic_json(
                folder / "state.json",
                {
                    "status": "queued",
                    "model": model,
                    "started_at": now(),
                    "load_timeout": config.local_load_timeout,
                    "load_into_ram": config.local_load_into_ram,
                },
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
            with (folder / "load.log").open("a") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPTS / "local_review.py"),
                        "--preload",
                        str(folder),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            self.model_loader = {
                "job": job,
                "model": model,
                "process": process,
                "folder": folder,
            }

    def wait_model_load(self, job, model):
        loader = self.model_loader
        if not loader or loader["job"] != job or loader["model"] != model:
            return
        while loader["process"].poll() is None:
            if self.cancelled.is_set():
                raise InterruptedError("Stopped by user.")
            self.checkpoint(job)
            self.cancelled.wait(0.2)
        if loader["process"].returncode:
            try:
                detail = json.loads((loader["folder"] / "state.json").read_text()).get(
                    "error", "Model preload failed."
                )
            except (OSError, ValueError):
                detail = "Model preload failed. See model-load/load.log."
            raise RuntimeError(redact(detail))

    def stop_model_load(self, job):
        loader = self.model_loader
        if not loader or loader["job"] != job:
            return
        process = loader["process"]
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        path = loader["folder"] / "state.json"
        try:
            state = json.loads(path.read_text())
            if state.get("status") in ("loading", "queued"):
                state.update(status="stopped", completed_at=now())
                review.atomic_json(path, state)
        except (OSError, ValueError):
            pass
        self.model_loader = None

    def extraction_progress(self, job, state):
        stage = next((s for s in state["stages"] if s["id"] == "parse"), None)
        if not stage or stage["status"] in ("completed", "skipped"):
            return
        try:
            manifest = yaml.safe_load((self.folder(job) / "catalog.yaml").read_text())
            config = state.get("config", {})
            first = (
                config.get("first_page", 1) - 1 if config.get("page_selection") else 0
            )
            last = config.get("last_page") if config.get("page_selection") else None
            pages = [
                p
                for p in manifest["page_states"]
                if p["page_idx"] >= first and (last is None or p["page_idx"] < last)
            ]
            if not pages:
                return
            saved = sum(p["status"] in ("parsed", "empty") for p in pages)
            failed = sum(p["status"] == "failed" for p in pages)
            detail = f"{saved}/{len(pages)} pages saved"
            percent = 80 * saved / len(pages)
            if saved == len(pages):
                selected = {p["page_idx"] + 1 for p in pages}
                assessed = sum(
                    p.get("page") in selected for p in state.get("quality", [])
                )
                percent += 20 * assessed / len(pages)
                detail = f"Quality check · {assessed}/{len(pages)} pages"
            elif failed:
                detail += f" · {failed} failed"
            else:
                current = next((p for p in pages if p["status"] == "processing"), None)
                if current:
                    detail += f" · PDF page {current['page_idx'] + 1}"
            stage["progress"] = {
                "percent": min(99, percent),
                "estimated": True,
                "detail": detail,
            }
        except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError):
            pass  # A missing/corrupt diagnostic must not prevent reading a run.

    def stoppage_info(self, job, state, reason=None, include_page=False):
        kind = state["status"]
        saved = state.get("stoppage") or {}
        if saved.get("kind") != kind:
            saved = {}
        stage = (
            saved.get("stage")
            or state.get("current_stage")
            or next(
                (
                    s["id"]
                    for s in state["stages"]
                    if s["status"] in ("running", "failed")
                ),
                next(
                    (
                        s["id"]
                        for s in state["stages"]
                        if s["status"] not in ("completed", "skipped")
                    ),
                    state["stages"][-1]["id"] if state["stages"] else "catalog",
                ),
            )
        )
        local = control.read(self.control_file(job))
        queue_control = control.read(self.queue_file())
        if kind in ("paused", "pausing"):
            reason = (
                "This catalog was paused by the user."
                if local.get("paused")
                else "The processing queue was paused by the user."
            )
            if kind == "pausing":
                reason += " The current page or step is being saved before pausing."
        elif kind == "stopping":
            reason = local.get(
                "stop_reason",
                "A stop was requested by the user. Completed pages are retained.",
            )
        else:
            reason = (
                reason
                or saved.get("reason")
                or state.get("error")
                or local.get("stop_reason")
                or "No detailed reason was recorded for this saved run."
            )
        page = saved.get("page")
        if page is None and state.get("current_page", {}).get("stage") == stage:
            page = state["current_page"]["page"]
        if page is None:
            page = next(
                (
                    t.get("page")
                    for t in reversed(state.get("review_tasks", []))
                    if t.get("stage") == stage
                    and t.get("status") in ("waiting", "failed", "cancelled")
                ),
                None,
            )
        if page is None and stage == "parse" and include_page:
            manifest_path = self.folder(job) / "catalog.yaml"
            if manifest_path.is_file():
                try:
                    manifest = yaml.safe_load(manifest_path.read_text()) or {}
                    page = next(
                        (
                            p["page_idx"] + 1
                            for p in manifest.get("page_states", [])
                            if p["status"] == "processing"
                        ),
                        None,
                    )
                    if page is None:
                        failed_pages = [
                            p["page_idx"] + 1
                            for p in manifest.get("page_states", [])
                            if p["status"] == "failed"
                        ]
                        page = failed_pages[0] if len(failed_pages) == 1 else None
                except (OSError, yaml.YAMLError, TypeError, KeyError):
                    pass  # A missing diagnostic must not obscure the original stop reason.
        return {
            "kind": kind,
            "stage": stage,
            "stage_title": next(
                (s["title"] for s in state["stages"] if s["id"] == stage),
                dict(STAGES).get(stage, stage),
            ),
            "reason": redact(reason),
            "page": page,
            "occurred_at": saved.get("occurred_at")
            or (
                local.get("requested_at")
                if local.get("paused") or local.get("stopped")
                else None
            )
            or (
                queue_control.get("requested_at")
                if kind in ("paused", "pausing") and queue_control.get("paused")
                else None
            )
            or state.get("completed_at"),
        }

    def save(self, job, state):
        review.atomic_json(self.folder(job) / "state.json", state)

    def files(self, job):
        output = ROOTS["parsed"] / "ui-runs" / job
        files = [(output / "master_raw.xlsx", "RAW workbook")]
        files += [
            (
                output / "etl/latest" / f"master_{name}.xlsx",
                name.capitalize() + " workbook",
            )
            for name in ("reviewed", "canonical", "structured", "matcher")
        ]
        files += [
            (self.folder(job) / name, title)
            for name, title in [
                ("state.json", "Run report"),
                ("catalog.yaml", "Catalog manifest"),
                ("mapping.yaml", "Manufacturer mapping"),
                ("reviews.json", "Review evidence"),
                ("quality.json", "Extraction confidence"),
                ("duplicates.json", "Duplicate comparisons"),
                ("manual-review.json", "Manual review evidence"),
                ("run.log", "Run log"),
            ]
        ]
        return [
            {
                "name": title,
                "filename": path.name,
                "bytes": path.stat().st_size,
                "url": f"/api/runs/{job}/download/{path.name}",
            }
            for path, title in files
            if path.is_file()
        ]

    def check_model_unused(self, url, model):
        with self.lock:
            for job in set(self.pending) | ({self.active} if self.active else set()):
                state = json.loads((self.folder(job) / "state.json").read_text())
                config = state["config"]
                if config.get("local_url", "").rstrip("/") != url:
                    continue
                if any(
                    config.get(provider) == "local" and config.get(field) == model
                    for provider, field in (
                        ("review_provider", "model"),
                        ("final_provider", "final_model"),
                    )
                ):
                    raise local_model_install.Conflict(
                        f"This model is reserved by an active or queued catalog ({state.get('source_name', job)}). Stop that catalog before deleting it."
                    )

    def check_models_not_deleting(self, config):
        for provider, field in (
            ("review_provider", "model"),
            ("final_provider", "final_model"),
        ):
            if getattr(config, provider) == "local" and model_installer.is_removing(
                config.local_url, getattr(config, field)
            ):
                raise HTTPException(
                    409,
                    "A selected local model is being deleted. Choose an installed model or wait for deletion to finish.",
                )

    def start_many(self, configs):
        prepared = [(config, *validate_settings(config)) for config in configs]
        with self.lock:
            for config in configs:
                self.check_models_not_deleting(config)
            result = []
            for config, source, pages, mapping in prepared:
                job = uuid.uuid4().hex
                folder = self.folder(job)
                folder.mkdir(parents=True)
                (ROOTS["parsed"] / "ui-runs" / job).mkdir(parents=True)
                catalog_id = library().register(
                    config.manufacturer_id,
                    source,
                    registry.digest(source),
                    pages,
                    config.revision,
                )
                state = {
                    "id": job,
                    "catalog_id": catalog_id,
                    "status": "queued",
                    "created_at": now(),
                    "source_name": source.name,
                    "config": config.model_dump(),
                    "manufacturer": mapping["manufacturer"],
                    "target_manufacturer_id": library().target(),
                    "review_tasks": [],
                    "stages": [
                        {"id": id, "title": title, "status": "queued"}
                        for id, title in STAGES
                    ],
                }
                self.save(job, state)
                self.pending.append(job)
                result.append(state)
            self.changed.notify_all()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.consume, daemon=True)
                self.worker.start()
            return result

    def resume(self, job):
        with self.lock:
            state = self.read(job)
            if state["status"] in ("paused", "pausing") and (
                job == self.active or job in self.pending
            ):
                review.atomic_json(
                    self.control_file(job), {"paused": False, "stopped": False}
                )
                self.changed.notify_all()
                return self.read(job)
            if state["status"] not in ("failed", "interrupted", "cancelled", "paused"):
                raise HTTPException(
                    409, "Only paused, interrupted, failed or stopped runs can resume."
                )
            saved_config = dict(state["config"])
            # Older runs retain their explicit Work model choices.
            saved_config.setdefault("review_provider", "work")
            saved_config.setdefault("final_provider", "work")
            if saved_config["review_provider"] == "work":
                saved_config.setdefault("reasoning", "medium")
            if saved_config["final_provider"] == "work":
                saved_config.setdefault("final_reasoning", "medium")
            saved_config.pop("work_mode", None)
            if saved_config.get("model") == "chatgpt":
                saved_config["model"] = "gpt-6.1-sol"
            if saved_config.get("final_model") == "chatgpt":
                saved_config["final_model"] = "gpt-6.1-sol"
            if "automatic_review" not in saved_config and saved_config.get(
                "model", ""
            ).startswith("gpt-"):
                saved_config.update(
                    automatic_review=False,
                    final_model=saved_config.get("final_model", "gpt-6.1-sol"),
                )
            config = Settings(**saved_config)
            validate_settings(config)
            self.check_models_not_deleting(config)
            state["config"] = config.model_dump()
            manifest_path = self.folder(job) / "catalog.yaml"
            if manifest_path.is_file():
                manifest = yaml.safe_load(manifest_path.read_text())
                registry.verify_inventory(manifest, manifest_path)
            for progress in state["stages"]:
                if progress["status"] != "completed":
                    progress["status"] = "queued"
            state.update(status="queued")
            state.pop("stoppage", None)
            state.pop("current_page", None)
            review.atomic_json(
                self.control_file(job), {"paused": False, "stopped": False}
            )
            (self.folder(job) / "paused.json").unlink(missing_ok=True)
            state.pop("error", None)
            state.pop("completed_at", None)
            self.pending.append(job)
            self.changed.notify_all()
            self.save(job, state)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.consume, daemon=True)
                self.worker.start()
            return state

    def start(self, config):
        return self.start_many([config])[0]

    def consume(self):
        while True:
            with self.changed:
                while True:
                    if not self.pending:
                        self.worker = None
                        return
                    job = next(
                        (
                            j
                            for j in self.pending
                            if not control.read(self.control_file(j)).get("paused")
                        ),
                        None,
                    )
                    if job and not control.read(self.queue_file()).get("paused"):
                        break
                    self.changed.wait()
                self.pending.remove(job)
                self.active = job
                self.cancelled.clear()
            state = self.read(job)
            self.execute(job, Settings(**state["config"]), state)
            with self.lock:
                self.active = None
                self.process = None

    def command(self, job, stage, number, command):
        self.checkpoint(job)
        folder = self.folder(job)
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
        env.update(PYTHONUNBUFFERED="1", MINERU_BACKEND="pipeline")
        with (
            (folder / "run.log").open("a") as log,
            (folder / f"{stage}-{number}.stdout").open("w") as output,
        ):
            log.write(f"\n[{now()}] {dict(STAGES)[stage]}\n")
            with self.lock:
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by user.")
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                    text=True,
                    start_new_session=True,
                )
                process = self.process
            for line in process.stdout:
                line = redact(line)
                output.write(line)
                log.write(line)
                log.flush()
            code = process.wait()
            with self.lock:
                self.process = None
            if self.cancelled.is_set():
                raise InterruptedError("Stopped by user.")
            if code:
                if stage == "parse":
                    try:
                        manifest = (
                            yaml.safe_load((folder / "catalog.yaml").read_text()) or {}
                        )
                        failed = next(
                            (
                                p
                                for p in manifest.get("page_states", [])
                                if p.get("status") == "failed"
                                and p.get("reason")
                                and isinstance(p.get("page_idx"), int)
                                and p["page_idx"] >= 0
                            ),
                            None,
                        )
                    except (OSError, yaml.YAMLError, TypeError, AttributeError):
                        failed = None
                    if failed:
                        raise RuntimeError(
                            redact(
                                f"Extract failed on PDF page {failed['page_idx'] + 1} (exit {code}): {str(failed['reason'])[:2200]}"
                            )
                        )
                raise RuntimeError(
                    f"{dict(STAGES)[stage]} failed (exit {code}). See the run log."
                )
            self.checkpoint(job)

    def extract(self, job, config, state, output):
        folder = self.folder(job)
        health = requests.get(config.mineru_url.rstrip("/") + "/health", timeout=10)
        health.raise_for_status()
        runtime = health.json()
        if runtime.get("device") != "cpu":
            raise ValueError("This workbench requires the CPU MinerU service.")
        if runtime.get("model_revision") != config.extraction_model:
            raise ValueError(
                "The selected extraction model does not match the running CPU model bundle."
            )
        pages = len(PdfReader(config.source_pdf).pages)
        self.command(
            job,
            "parse",
            0,
            parse_command(config, folder, output, *config.bounds(pages)),
        )
        manifest_path = folder / "catalog.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        report = []
        for initial in list(manifest["page_states"]):
            self.checkpoint(job)
            if initial["status"] != "parsed":
                continue
            current = initial
            score = quality.assess(Path(config.source_pdf), current)
            attempts = [
                {"method": config.method, "cache_key": current["cache_key"], **score}
            ]
            method = config.method
            for index in range(1, config.max_quality_passes):
                self.checkpoint(job)
                if (
                    not config.automatic_quality
                    or score["confidence"] >= config.confidence_threshold
                ):
                    break
                method = quality.retry_method(method, score, index)
                if method is None or method in {
                    attempt["method"] for attempt in attempts
                }:
                    break
                key = (initial["source_sha256"], initial["page_idx"])
                # Force only this retry without mutating the retained page state.
                # A tied, failed or interrupted retry must keep the prior artifact.
                retry_manifest = copy.deepcopy(manifest)
                for p in retry_manifest["page_states"]:
                    if (p["source_sha256"], p["page_idx"]) == key:
                        p.pop("artifact_path", None)
                        p.pop("artifact_sha256", None)
                registry.save_manifest(manifest_path, retry_manifest)
                try:
                    self.command(
                        job,
                        "parse",
                        initial["page_idx"] * 10 + index,
                        parse_command(
                            config,
                            folder,
                            output,
                            initial["page_idx"],
                            initial["page_idx"],
                            method,
                        ),
                    )
                    candidate_manifest = yaml.safe_load(manifest_path.read_text())
                    candidate = next(
                        p
                        for p in candidate_manifest["page_states"]
                        if (p["source_sha256"], p["page_idx"]) == key
                    )
                    candidate_score = quality.assess(Path(config.source_pdf), candidate)
                    selected = quality.keep_candidate(score, candidate_score)
                    attempts.append(
                        {
                            "method": method,
                            "cache_key": candidate["cache_key"],
                            "selected": selected,
                            **candidate_score,
                        }
                    )
                    if selected:
                        current, score = candidate, candidate_score
                    manifest = candidate_manifest
                finally:
                    manifest["page_states"] = [
                        current if (p["source_sha256"], p["page_idx"]) == key else p
                        for p in manifest["page_states"]
                    ]
                    manifest["parse_selection"] = [
                        {
                            "source_sha256": current["source_sha256"],
                            "page_idx": current["page_idx"],
                            "cache_key": current["cache_key"],
                        }
                        if (p["source_sha256"], p["page_idx"]) == key
                        else p
                        for p in manifest["parse_selection"]
                    ]
                    registry.save_manifest(manifest_path, manifest)
            report.append(
                {
                    "page": initial["page_idx"] + 1,
                    "selected_cache_key": current["cache_key"],
                    "attempts": attempts,
                    **score,
                }
            )
            state["quality"] = report
            self.save(job, state)
        review.atomic_json(folder / "quality.json", report)
        state["pages"] = {
            "total": pages,
            "parsed": len(report),
            "failed": sum(p["status"] == "failed" for p in manifest["page_states"]),
        }

    def review_stage(self, job, stage, config, state, output):
        import fitz

        if (
            config.final_provider if stage == "final" else config.review_provider
        ) == "local":
            self.wait_model_load(
                job, config.final_model if stage == "final" else config.model
            )
        folder = self.folder(job)
        manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
        local_task = (
            config.final_provider if stage == "final" else config.review_provider
        ) == "local"
        mapping = yaml.safe_load((folder / "mapping.yaml").read_text())
        registry.verify_inventory(manifest, folder / "catalog.yaml")
        all_results = (
            json.loads((folder / "reviews.json").read_text())
            if (folder / "reviews.json").exists()
            else []
        )
        all_results = [r for r in all_results if r["stage"] != stage]
        raw = workbook_rows(output / "master_raw.xlsx", "raw")
        canonical = output / "etl/latest/master_canonical.xlsx"
        products = workbook_rows(canonical, "products")
        attributes = workbook_rows(canonical, "attributes")
        provenance = workbook_rows(canonical, "provenance")
        provenance_by_id = {p["provenance_id"]: p for p in provenance}
        prov_pages = {
            p["provenance_id"]: int(p["page_idx"])
            for p in provenance
            if p.get("page_idx") is not None
        }
        by_product = {}
        for a in attributes:
            by_product.setdefault(a["product_version_id"], []).append(a)
        duplicates = (
            json.loads((folder / "duplicates.json").read_text())
            if (folder / "duplicates.json").exists()
            else []
        )
        pdf = fitz.open(config.source_pdf)
        try:
            review_pages = [
                p for p in manifest["page_states"] if p["status"] == "parsed"
            ]
            progress = next(
                (s for s in state.get("stages", []) if s["id"] == stage), {}
            )
            for page_number, page in enumerate(review_pages):
                index = page["page_idx"]
                state["current_page"] = {"stage": stage, "page": index + 1}
                image = folder / f"page-{index + 1}.png"
                if not image.is_file():
                    pdf[index].get_pixmap(dpi=config.review_dpi).save(image)
                rows = [
                    r
                    for r in raw
                    if int(r["page_idx"]) == index
                    and r["cache_key"] == page["cache_key"]
                ]
                page_products = [
                    {**p, "attributes": by_product.get(p["product_version_id"], [])}
                    for p in products
                    if prov_pages.get(p["primary_provenance_id"]) == index
                ]
                blocks, _ = quality.artifacts(page)
                # Inventory stays small; complete original page image accompanies
                # every chunk. No other catalog/page/task history is supplied.
                inventory = [
                    {
                        "type": b.get("type"),
                        "bbox": b.get("bbox"),
                        "text": str(b.get("text", ""))[:300],
                    }
                    for b in blocks
                    if isinstance(b, dict)
                ]
                chunks = bounded_chunks(rows, config.review_batch_rows)
                empty_tool_audits = {}
                if stage == "final":
                    if config.final_provider == "local":
                        ready = [p for p in page_products if p.get("attributes")]
                        missing = []
                        for p in page_products:
                            if p.get("attributes"):
                                continue
                            trace = provenance_by_id.get(p["primary_provenance_id"], {})
                            missing.append(
                                {
                                    "product_version_id": p["product_version_id"],
                                    "identity_status": p.get("identity_status"),
                                    "article_number": p.get("article_number"),
                                    "table_index": trace.get("table_index"),
                                    "row_index": trace.get("row_index"),
                                    "missing_fields": ["normalized parameters"],
                                }
                            )
                        chunks = bounded_chunks(ready, 2) if ready else []
                        if missing:
                            for audit in bounded_chunks(missing, 20):
                                empty_tool_audits[len(chunks)] = audit
                                chunks.append([])
                        if not chunks:
                            chunks = [[]]
                    else:
                        chunks = bounded_chunks(page_products, 5)
                if stage in ("completeness", "mapping"):
                    # One page-local inventory task, with each printed header and
                    # row accounted for. Correctness separately checks all cells.
                    headers = {}
                    for r in rows:
                        header = str(r.get("raw_parameter_name") or "")
                        headers.setdefault(header, r)
                    chunks = [list(headers.values())]
                row_inventory = {}
                for r in rows:
                    identity = str(r["table_index"]) + ":" + str(r["row_index"])
                    row_inventory.setdefault(
                        identity,
                        {
                            "table": r["table_index"],
                            "row": r["row_index"],
                            "cells": 0,
                            "article_number": r.get("article_number"),
                        },
                    )["cells"] += 1
                for number, chunk in enumerate(chunks):
                    self.checkpoint(job)
                    progress["progress"] = {
                        "percent": 100
                        * (page_number + number / len(chunks))
                        / len(review_pages),
                        "estimated": True,
                        "detail": f"{page_number}/{len(review_pages)} pages · PDF page {index + 1} · task {number + 1}/{len(chunks)}",
                    }
                    self.save(job, state)
                    context = {
                        "catalog_id": manifest["catalog_id"],
                        "manufacturer": manifest["manufacturer"],
                        "revision": manifest["revision"],
                        "catalog_languages": manifest["languages"],
                        "source_sha256": page["source_sha256"],
                        "page": index + 1,
                        "cache_key": page["cache_key"],
                        "chunk": number + 1,
                        "chunks": len(chunks),
                        "total_raw_cells": len(rows),
                        "extracted_product_rows": len(
                            {
                                (r["table_index"], r["row_index"])
                                for r in rows
                                if r.get("article_number")
                            }
                        ),
                        "extraction_inventory": inventory,
                        "row_inventory": list(row_inventory.values()),
                        "raw_cells": [review_cell(r, compact=local_task) for r in chunk]
                        if stage != "final"
                        else [],
                        "products": chunk if stage == "final" else [],
                        "mapping_rules": [
                            r
                            for r in mapping["rules"]
                            if r["canonical_field"]
                            in {row.get("raw_parameter_name") for row in rows}
                        ],
                        "symbol_dictionary": [
                            s
                            for s in mapping.get("symbol_dictionary", [])
                            if s["symbol"]
                            in {row.get("raw_parameter_name") for row in rows}
                        ],
                        "duplicate_candidates": [
                            duplicate_context(d)
                            for d in duplicates
                            if any(
                                side["product"]["product_version_id"]
                                in {p["product_version_id"] for p in chunk}
                                for side in (d["left"], d["right"])
                            )
                        ]
                        if stage == "final"
                        else [],
                    }
                    if local_task:
                        if number in empty_tool_audits:
                            context["empty_tool_rows"] = empty_tool_audits[number]
                            context["final_audit"] = (
                                "These source rows have no normalized parameters. Audit omissions and row classification against the original page and official sources; they cannot pass tool validation."
                            )
                        if stage in ("mapping", "final"):
                            context["mapping_contract"] = {
                                "families": [f["name"] for f in mapping["families"]],
                                "canonical_fields": sorted(
                                    set(
                                        mapping.get("matcher_contract", {}).get(
                                            "wide_fields", []
                                        )
                                    )
                                    | {r["canonical_field"] for r in mapping["rules"]}
                                ),
                            }
                        regions = {}
                        for row in rows:
                            regions.setdefault(
                                row["table_index"],
                                {
                                    k: row.get(k)
                                    for k in (
                                        "table_index",
                                        "bbox",
                                        "bbox_scope",
                                        "bbox_units",
                                    )
                                },
                            )
                        context["table_regions"] = list(regions.values())
                        context["table_opening_rows"] = [
                            review_cell(r, compact=True)
                            for r in rows
                            if int(r["row_index"]) < 2
                        ]
                    provider = (
                        config.final_provider
                        if stage == "final"
                        else config.review_provider
                    )
                    reused = review.reusable_result(
                        queue(),
                        state["review_tasks"],
                        stage,
                        context,
                        [image],
                        model=config.final_model if stage == "final" else config.model,
                        reasoning=config.final_reasoning
                        if stage == "final"
                        else config.reasoning,
                        provider=provider,
                        local=config.local_settings() if provider == "local" else None,
                        confidence_threshold=config.confidence_threshold,
                    )
                    if reused:
                        request, result = reused
                        all_results.append(
                            {
                                "task_id": request["task_id"],
                                "stage": stage,
                                "page": index + 1,
                                "result": result,
                            }
                        )
                        review.atomic_json(folder / "reviews.json", all_results)
                        if stage != "mapping":
                            continue
                        mapping = review.merge_mapping(
                            mapping,
                            result,
                            f"{manifest['documents'][0]['sha256']}:page:{index + 1}",
                        )
                        continue
                    request = review.create_task(
                        queue(),
                        stage,
                        context,
                        [image],
                        model=config.final_model if stage == "final" else config.model,
                        reasoning=config.final_reasoning
                        if stage == "final"
                        else config.reasoning,
                        manual_handoff=provider == "work"
                        and not config.automatic_review,
                        confidence_threshold=config.confidence_threshold,
                        provider=provider,
                        local=config.local_settings() if provider == "local" else None,
                    )
                    request["timeout"] = config.review_timeout
                    review.atomic_json(
                        queue() / request["task_id"] / "request.json", request
                    )
                    state["review_tasks"].append(
                        {
                            "id": request["task_id"],
                            "stage": stage,
                            "page": index + 1,
                            "chunk": number + 1,
                            "status": "waiting",
                        }
                    )
                    state["status"] = "awaiting_review"
                    self.save(job, state)
                    if provider == "local":
                        self.command(
                            job,
                            stage,
                            number,
                            [
                                sys.executable,
                                str(SCRIPTS / "local_review.py"),
                                "--task",
                                str(queue() / request["task_id"]),
                            ],
                        )
                    result = review.wait_result(
                        queue(),
                        request,
                        self.cancelled,
                        config.review_timeout,
                        checkpoint=lambda: self.checkpoint(job),
                    )
                    state["review_tasks"][-1]["status"] = result["status"]
                    state["review_tasks"][-1]["confidence"] = result["confidence"]
                    state["status"] = "running"
                    all_results.append(
                        {
                            "task_id": request["task_id"],
                            "stage": stage,
                            "page": index + 1,
                            "result": result,
                        }
                    )
                    review.atomic_json(folder / "reviews.json", all_results)
                    if stage == "mapping":
                        mapping = review.merge_mapping(
                            mapping,
                            result,
                            f"{manifest['documents'][0]['sha256']}:page:{index + 1}",
                        )
                        registry.validate_manifest(
                            mapping, ROOTS["schemas"] / "field-mapping.schema.json"
                        )
                        (folder / "mapping.yaml").write_text(
                            yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False)
                        )
                    self.save(job, state)
                progress["progress"] = {
                    "percent": min(99, 100 * (page_number + 1) / len(review_pages)),
                    "estimated": True,
                    "detail": f"{page_number + 1}/{len(review_pages)} pages reviewed",
                }
                self.save(job, state)
            if stage == "mapping":
                mapping = library().save_mapping(config.manufacturer_id, mapping)
                (folder / "mapping.yaml").write_text(
                    yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False)
                )
        finally:
            pdf.close()
        return all(
            r["result"]["status"] == "passed"
            for r in all_results
            if r["stage"] == stage
        )

    def execute(self, job, config, state):
        folder = self.folder(job)
        output = ROOTS["parsed"] / "ui-runs" / job
        state["status"] = "running"
        try:
            source, pages, mapping = validate_settings(config)
            commands = {
                s["id"]: s["commands"] for s in compile_stages(config, job, pages)
            }
            if config.review_provider == "local" and config.model != "none":
                self.start_model_load(job, config, config.model)
            elif config.final_provider == "local" and config.final_model != "none":
                self.start_model_load(job, config, config.final_model)
            for progress in state["stages"]:
                if progress["status"] == "completed":
                    continue
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by user.")
                self.checkpoint(job)
                stage = progress["id"]
                if stage == "structure" and config.final_provider == "local":
                    self.start_model_load(job, config, config.final_model)
                state["current_stage"] = stage
                if not review_enabled(config, stage):
                    progress.update(
                        status="skipped",
                        quality_blocked=True,
                        reason=f"No model selected; {progress['title'].lower()} was not performed. The catalog remains unvalidated.",
                    )
                    self.save(job, state)
                    continue
                progress.update(status="running", started_at=now())
                self.save(job, state)
                if stage == "catalog":
                    manifest = registry.register(
                        source,
                        catalog_id=state["catalog_id"],
                        manufacturer=mapping["manufacturer"],
                        role="TARGET"
                        if config.manufacturer_id == state["target_manufacturer_id"]
                        else "SOURCE",
                        revision=config.revision,
                        mapping_profile=mapping["mapping_profile"],
                        families=[
                            s.strip() for s in config.families.split(",") if s.strip()
                        ],
                        languages=[
                            s.strip() for s in config.languages.split(",") if s.strip()
                        ],
                    )
                    registry.save_manifest(folder / "catalog.yaml", manifest)
                    (folder / "mapping.yaml").write_text(
                        yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False)
                    )
                    (folder / "decisions.jsonl").write_text("")
                    library().status(state["catalog_id"], "processing", job)
                elif stage == "parse":
                    self.extract(job, config, state, output)
                elif stage in review.CHECKS:
                    if not self.review_stage(job, stage, config, state, output):
                        progress["quality_blocked"] = True
                elif stage == "duplicates":
                    library().index_workbook(
                        state["catalog_id"], output / "etl/latest/master_canonical.xlsx"
                    )
                    matches = library().duplicates(state["catalog_id"])
                    review.atomic_json(folder / "duplicates.json", matches)
                    state["duplicate_count"] = len(matches)
                    outputs.manufacturer_workbook(
                        library(),
                        config.manufacturer_id,
                        ROOTS["catalogs"]
                        / "library/workbooks"
                        / (config.manufacturer_id + ".xlsx"),
                    )
                else:
                    for number, command in enumerate(commands[stage]):
                        self.command(job, stage, number, command)
                if stage == "structure":
                    pointer = json.loads((output / "etl/latest.json").read_text())
                    report = json.loads(
                        (
                            output
                            / "etl"
                            / pointer["attempt_relpath"]
                            / "manifest.json"
                        ).read_text()
                    )
                    state["metrics"] = report.get("results", {})
                progress.update(status="completed", completed_at=now())
                self.save(job, state)
            if config.prepare_manual_review:
                self.prepare_manual(job, state)
            full_catalog = state.get("pages", {}).get("parsed") == pages
            blocked = (
                state.get("metrics", {}).get("blocking_issues", 0)
                or any(s.get("quality_blocked") for s in state["stages"])
                or not full_catalog
            )
            state["status"] = "quality_blocked" if blocked else "completed"
            library().status(
                state["catalog_id"], "needs_review" if blocked else "validated", job
            )
        except InterruptedError as exc:
            reason = control.read(self.control_file(job)).get("stop_reason", str(exc))
            state.update(status="cancelled", error=redact(reason))
            state["stoppage"] = {
                **self.stoppage_info(job, state, reason, include_page=True),
                "occurred_at": now(),
            }
            for task in state["review_tasks"]:
                if task["status"] == "waiting":
                    task["status"] = "cancelled"
            library().status(state["catalog_id"], "cancelled", job)
        except Exception as exc:
            state.update(status="failed", error=redact(str(exc)))
            state["stoppage"] = {
                **self.stoppage_info(job, state, state["error"], include_page=True),
                "occurred_at": now(),
            }
            for task in state["review_tasks"]:
                if task["status"] == "waiting":
                    task["status"] = "failed"
            library().status(state["catalog_id"], "failed", job)
            for s in state["stages"]:
                if s["status"] == "running":
                    s["status"] = "failed"
        finally:
            self.stop_model_load(job)
            for s in state["stages"]:
                if s["status"] in ("running", "queued"):
                    s["status"] = "not_run"
            state["completed_at"] = now()
            self.save(job, state)

    def prepare_manual(self, job, state):
        folder = self.folder(job)
        canonical = (
            ROOTS["parsed"] / "ui-runs" / job / "etl/latest/master_canonical.xlsx"
        )
        if not canonical.is_file():
            raise ValueError("The normalized workbook is not available yet.")
        manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
        evidence = (
            json.loads((folder / "reviews.json").read_text())
            if (folder / "reviews.json").is_file()
            else []
        )
        packets = outputs.manual_packets(
            workbook_rows(canonical, "products"),
            workbook_rows(canonical, "attributes"),
            workbook_rows(canonical, "provenance"),
            evidence,
            state["catalog_id"],
            manifest["documents"][0]["sha256"],
        )
        review.atomic_json(folder / "manual-review.json", packets)
        return packets

    def cancel(self, job, scope="catalog"):
        with self.lock:
            stop_reason = (
                "The processing queue was stopped by the user."
                if scope == "queue"
                else "This catalog was stopped by the user."
            )
            stop_control = {
                "paused": False,
                "stopped": True,
                "stop_reason": stop_reason,
                "requested_at": now(),
            }
            if job in self.pending:
                state = self.read(job)
                self.pending.remove(job)
                review.atomic_json(self.control_file(job), stop_control)
                state.update(status="cancelled", completed_at=now())
                state["stoppage"] = self.stoppage_info(
                    job, state, stop_reason + " This catalog had not started."
                )
                for s in state["stages"]:
                    s["status"] = "not_run"
                self.save(job, state)
                library().status(state["catalog_id"], "cancelled", job)
                self.changed.notify_all()
                return {"status": "cancelled"}
            if self.active != job:
                state = self.read(job)
                if state["status"] == "paused":
                    review.atomic_json(self.control_file(job), stop_control)
                    state.update(status="cancelled", completed_at=now())
                    state["stoppage"] = self.stoppage_info(job, state, stop_reason)
                    self.save(job, state)
                    library().status(state["catalog_id"], "cancelled", job)
                    return {"status": "cancelled"}
                raise HTTPException(409, "This run is not active or queued.")
            review.atomic_json(self.control_file(job), stop_control)
            self.cancelled.set()
            if (
                self.model_loader
                and self.model_loader["job"] == job
                and self.model_loader["process"].poll() is None
            ):
                try:
                    os.killpg(self.model_loader["process"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            state = self.read(job)
            for task in state.get("review_tasks", []):
                if task["status"] in ("waiting", "cancelled"):
                    review.atomic_json(
                        queue() / task["id"] / "cancelled.json", {"task_id": task["id"]}
                    )
            if self.process:
                process = self.process
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

                def kill():
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

                timer = threading.Timer(5, kill)
                timer.daemon = True
                timer.start()
            return {"status": "stopping"}

    def pause(self, job):
        with self.changed:
            state = self.read(job)
            if state["status"] not in (
                "running",
                "awaiting_review",
                "queued",
                "pausing",
                "paused",
            ):
                raise HTTPException(409, "Only an active or queued catalog can pause.")
            review.atomic_json(
                self.control_file(job),
                {"paused": True, "stopped": False, "requested_at": now()},
            )
            self.changed.notify_all()
            return self.read(job)

    def stop_queue(self):
        with self.changed:
            for job in list(self.pending):
                self.cancel(job, scope="queue")
            if self.active:
                self.cancel(self.active, scope="queue")
            self.pause_queue(False)
            return self.queue_status()


jobs = Jobs()
app = FastAPI(title="CatalogFlow", docs_url="/api/docs")


def storage_help():
    return (
        f"The app runs as UID {os.geteuid()}:GID {os.getegid()}. "
        "On the server, run: sudo docker compose run --rm --no-deps "
        "--name CatalogFlow_storage-repair init-storage. "
        "For existing root-owned runtime files, also repair ownership of catalogs, logs and parsed."
    )


@app.exception_handler(PermissionError)
async def storage_permission_error(request: Request, exc: PermissionError):
    path = redact(str(exc.filename or "runtime storage"))
    return JSONResponse(
        {"detail": f"CatalogFlow cannot access {path}. {storage_help()}"},
        status_code=503,
    )


@app.exception_handler(sqlite3.Error)
async def library_database_error(request: Request, exc: sqlite3.Error):
    reason = redact(str(exc))
    detail = f"The manufacturer library database is unavailable: {reason}."
    if "readonly" in reason.lower() or "unable to open" in reason.lower():
        detail += " " + storage_help()
    return JSONResponse({"detail": detail}, status_code=503)


@app.exception_handler(Exception)
async def unexpected_server_error(request: Request, exc: Exception):
    return JSONResponse(
        {
            "detail": "The server could not complete this request. Check pipeline-ui logs for the cause."
        },
        status_code=500,
    )


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "Cross-origin changes are not allowed."}, status_code=403
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/health")
def health():
    for name in ("catalogs", "logs", "parsed"):
        path = ROOTS[name]
        if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
            raise PermissionError(
                errno.EACCES, "Runtime directory is not writable", str(path)
            )
    library().target()
    return {"status": "healthy"}


@app.get("/api/bootstrap")
def bootstrap():
    catalogs = []
    lib = library()
    manufacturers = lib.manufacturers()
    for subdir in ("imports/guhring", "input", "test", "uploads"):
        folder = ROOTS["catalogs"] / subdir
        paths = folder.rglob("*") if folder.is_dir() else []
        for path in sorted(
            p for p in paths if p.is_file() and p.suffix.lower() == ".pdf"
        ):
            if not path.resolve().is_relative_to(ROOTS["catalogs"].resolve()):
                continue
            try:
                metadata = detection.inspect(
                    path,
                    manufacturers,
                    ROOTS["catalogs"] / "library/detections",
                    allow_ocr=False,
                )
                catalogs.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "pages": metadata["total_pages"],
                        "bytes": path.stat().st_size,
                        "detection": metadata,
                        "origin": "Windows · GUHRING"
                        if subdir == "imports/guhring"
                        else subdir,
                        "manufacturer_hint": "guhring"
                        if subdir == "imports/guhring"
                        else None,
                    }
                )
            except Exception:
                continue
    defaults = Settings().model_dump()
    defaults["mineru_url"] = os.getenv("MINERU_API_URL", defaults["mineru_url"])
    if catalogs:
        preferred = next(
            (
                c
                for c in catalogs
                if c["manufacturer_hint"]
                and c["name"] == "GTC_Milling_Cutters 2018.pdf"
            ),
            catalogs[0],
        )
        defaults["source_pdf"] = preferred["path"]
    historical = list((ROOTS["catalogs"] / "manifests").glob("*.yaml"))
    historical += list((ROOTS["logs"] / "ui-runs").glob("*/catalog.yaml"))
    for path in historical:
        try:
            manifest = yaml.safe_load(path.read_text())
            registry.validate_manifest(
                manifest, ROOTS["schemas"] / "catalog-manifest.schema.json"
            )
            lib.import_manifest(
                manifest,
                path,
                path.parent.name if path.parent.name != "manifests" else None,
            )
        except (ValueError, OSError, KeyError):
            continue
    return {
        "catalogs": catalogs,
        "defaults": defaults,
        "manufacturers": manufacturers,
        "target_manufacturer_id": lib.target(),
        "reviewer": review.capability(queue()),
        "active_run": jobs.active,
        "queued_runs": list(jobs.pending),
        "models": models.catalog(ROOTS["catalogs"] / "library"),
        "local_llm": {"presets": local.PRESETS, "models": []},
    }


@app.get("/api/library")
def get_library():
    lib = library()
    return {
        "manufacturers": lib.manufacturers(),
        "catalogs": lib.catalogs(),
        "target_manufacturer_id": lib.target(),
        "duplicates": lib.duplicates(),
    }


@app.get("/api/models")
def model_list():
    return models.catalog(ROOTS["catalogs"] / "library")


@app.get("/api/manufacturers/{id}/products")
def manufacturer_products(id: str):
    try:
        products = library().products(id)
        return {
            "rows": products[:200],
            "total": len(products),
            "workbook_url": f"/api/manufacturers/{id}/workbook",
        }
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/manufacturers/{id}/workbook")
def manufacturer_excel(id: str):
    try:
        path = outputs.manufacturer_workbook(
            library(), id, ROOTS["catalogs"] / "library/workbooks" / (id + ".xlsx")
        )
        return FileResponse(path, filename=id + "-normalized-products.xlsx")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ManufacturerInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.post("/api/manufacturers", status_code=201)
def add_manufacturer(config: ManufacturerInput):
    return library().add_manufacturer(config.name)


class TargetInput(BaseModel):
    manufacturer_id: str


@app.put("/api/library/target")
def target(config: TargetInput):
    try:
        return {"manufacturer_id": library().target(config.manufacturer_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/library/catalogs/{id}/pdf")
def catalog_pdf(id: str):
    catalog = next((c for c in library().catalogs() if c["id"] == id), None)
    if catalog is None:
        raise HTTPException(404, "Catalog not found")
    try:
        path = restricted_path(catalog["path"], ("catalogs",), ".pdf")
        if registry.digest(path) != catalog["sha256"]:
            raise ValueError("The original PDF changed; register this edition again.")
        return FileResponse(path, media_type="application/pdf")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/manufacturers/{id}/mapping")
def get_mapping(id: str):
    try:
        return library().mapping(id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.put("/api/manufacturers/{id}/mapping")
async def save_mapping(id: str, request: Request):
    try:
        mapping = await request.json()
        registry.validate_manifest(
            mapping, ROOTS["schemas"] / "field-mapping.schema.json"
        )
        library().save_mapping(id, mapping)
        return {"saved": True}
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


@app.get("/api/services")
def services(local_url: str = "http://local-llm:11434"):
    try:
        response = requests.get(
            os.getenv("MINERU_API_URL", "http://mineru:8000") + "/health", timeout=3
        )
        mineru = {
            "available": response.ok,
            "detail": response.json() if response.ok else {},
        }
    except requests.RequestException:
        mineru = {"available": False, "detail": {}}
    return {
        "mineru": mineru,
        "reviewer": review.capability(queue()),
        "local_llm": local.health(local_url),
    }


class LocalServerInput(BaseModel):
    url: str = "http://local-llm:11434"


class LocalInstallInput(LocalServerInput):
    model: str = Field(min_length=1, max_length=160)


model_installer = local_model_install.Installer()


@app.get("/api/local-model-installs")
def local_model_installs(url: str = "http://local-llm:11434"):
    try:
        return {
            "installs": model_installer.list(
                url, ROOTS["logs"] / "local-model-installs"
            )
        }
    except ValueError as exc:
        raise HTTPException(400, redact(str(exc)))


@app.post("/api/local-model-installs", status_code=202)
def install_local_model(config: LocalInstallInput):
    try:
        return model_installer.start(
            config.url, config.model, ROOTS["logs"] / "local-model-installs"
        )
    except local_model_install.Conflict as exc:
        raise HTTPException(409, redact(str(exc)))
    except ValueError as exc:
        raise HTTPException(400, redact(str(exc)))


@app.delete("/api/local-models")
def delete_local_model(config: LocalInstallInput):
    try:
        return model_installer.delete(
            config.url,
            config.model,
            ROOTS["logs"] / "local-model-installs",
            jobs.check_model_unused,
        )
    except local_model_install.Conflict as exc:
        raise HTTPException(409, redact(str(exc)))
    except local_model_install.DeleteError as exc:
        raise HTTPException(502, redact(str(exc)))
    except ValueError as exc:
        raise HTTPException(400, redact(str(exc)))


@app.post("/api/local-models")
def local_models(config: LocalServerInput):
    try:
        return local.discover(config.url)
    except Exception as exc:
        return {
            "available": False,
            "models": [],
            "presets": local.PRESETS,
            "detail": redact(str(exc)),
        }


@app.post("/api/plan")
def plan(config: Settings):
    try:
        source, pages, mapping = validate_settings(config)
        first, last = config.bounds(pages)
        return {
            "source": source.name,
            "pages": pages,
            "selected_pages": last - first + 1,
            "paid": False,
            "review_model": config.model,
            "final_model": config.final_model,
            "automatic_review": config.automatic_review,
            "review_provider": config.review_provider,
            "final_provider": config.final_provider,
            "reasoning": config.reasoning,
            "final_reasoning": config.final_reasoning,
            "manufacturer": mapping["manufacturer"],
            "stages": compile_stages(config, "preview", pages),
            "config": config.model_dump(),
            "reviewer": review.capability(queue()),
        }
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


@app.post("/api/runs", status_code=202)
def start(config: Settings):
    try:
        return jobs.start(config)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


class BatchInput(BaseModel):
    catalogs: list[Settings] = Field(min_length=1, max_length=100)


@app.post("/api/batches", status_code=202)
def batch(config: BatchInput):
    try:
        return {"runs": jobs.start_many(config.catalogs)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


@app.get("/api/runs")
def history():
    paths = sorted(
        (ROOTS["logs"] / "ui-runs").glob("*/state.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:100]
    return [jobs.read(p.parent.name) for p in paths]


@app.get("/api/runs/{job}")
def status(job: str):
    return jobs.read(job)


@app.post("/api/runs/{job}/resume", status_code=202)
def resume(job: str):
    try:
        return jobs.resume(job)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


@app.post("/api/runs/{job}/cancel")
def cancel(job: str):
    return jobs.cancel(job)


@app.post("/api/runs/{job}/pause")
def pause(job: str):
    return jobs.pause(job)


@app.get("/api/queue")
def queue_status():
    return jobs.queue_status()


@app.post("/api/queue/pause")
def pause_queue():
    return jobs.pause_queue()


@app.post("/api/queue/resume")
def resume_queue():
    return jobs.pause_queue(False)


@app.post("/api/queue/stop")
def stop_queue():
    return jobs.stop_queue()


@app.get("/api/runs/{job}/log")
def log(job: str):
    jobs.read(job)
    path = jobs.folder(job) / "run.log"
    if not path.exists():
        return {"text": ""}
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 24000))
        return {"text": redact(handle.read().decode("utf-8", errors="replace"))}


@app.get("/api/runs/{job}/issues")
def issues(job: str):
    jobs.read(job)
    return {
        "rows": workbook_rows(
            ROOTS["parsed"] / "ui-runs" / job / "etl/latest/master_canonical.xlsx",
            "issues",
        )[:100]
    }


def diagnostic_tail(path):
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 8192))
        return redact(
            "\n".join(handle.read().decode(errors="replace").splitlines()[-30:])
        )


@app.get("/api/runs/{job}/stoppage")
def stoppage(job: str):
    state = jobs.read(job)
    if state["status"] not in STOP_STATUSES:
        return {"stoppage": None, "diagnostics": [], "log_url": None}
    info = jobs.stoppage_info(job, state, include_page=True)
    diagnostics = []
    folder = jobs.folder(job)
    if state["status"] in ("failed", "interrupted", "cancelled"):
        stage_outputs = (
            sorted(
                folder.glob(f"{info['stage']}-*.stdout"),
                key=lambda p: p.stat().st_mtime,
            )
            if info["stage"] in dict(STAGES)
            else []
        )
        path = stage_outputs[-1] if stage_outputs else folder / "run.log"
        text = diagnostic_tail(path)
        if text:
            diagnostics.append(
                {
                    "label": "Latest saved stage output"
                    if stage_outputs
                    else "Run log excerpt",
                    "text": text,
                }
            )
        if info["stage"] in review.CHECKS and state["status"] == "failed":
            task = next(
                (
                    t
                    for t in reversed(state.get("review_tasks", []))
                    if t.get("stage") == info["stage"]
                ),
                None,
            )
            if task and re.fullmatch(r"[a-f0-9]{32}", task["id"]):
                for name, label in (
                    ("error.json", "Reviewer error"),
                    ("response-diagnostics.json", "Local model response details"),
                    ("ollama-http.json", "Local server HTTP response"),
                    ("stderr.log", "Reviewer output"),
                ):
                    text = diagnostic_tail(queue() / task["id"] / name)
                    if text:
                        diagnostics.append({"label": label, "text": text})
    log_url = next(
        (f["url"] for f in state["files"] if f["filename"] == "run.log"), None
    )
    return {"stoppage": info, "diagnostics": diagnostics, "log_url": log_url}


@app.post("/api/runs/{job}/manual-review")
def prepare_manual_review(job: str):
    state = jobs.read(job)
    if state["status"] not in (
        "completed",
        "quality_blocked",
        "failed",
        "interrupted",
        "cancelled",
    ):
        raise HTTPException(
            409, "Wait for the run to stop before preparing its review snapshot."
        )
    try:
        return {
            "tools": review_notes.with_notes(
                jobs.prepare_manual(job, state),
                jobs.folder(job) / "manual-decisions.json",
            )
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/runs/{job}/manual-review")
def manual_review(job: str):
    jobs.read(job)
    path = jobs.folder(job) / "manual-review.json"
    if not path.is_file():
        raise HTTPException(
            404, "Prepare manual review to create this evidence snapshot."
        )
    return {
        "tools": review_notes.with_notes(
            json.loads(path.read_text()), jobs.folder(job) / "manual-decisions.json"
        )
    }


class ManualDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_version_id: str = Field(min_length=1, max_length=200)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["pending", "checked", "needs_changes"]
    notes: str = Field("", max_length=2000)


@app.post("/api/runs/{job}/manual-review/decision")
def save_manual_decision(job: str, decision: ManualDecision):
    with jobs.lock:
        state = jobs.read(job)
        if state["status"] not in (
            "completed",
            "quality_blocked",
            "failed",
            "interrupted",
            "cancelled",
        ):
            raise HTTPException(
                409, "Wait for the run to stop before recording review notes."
            )
        folder = jobs.folder(job)
        packets = manual_review(job)["tools"]
        packet = next(
            (
                p
                for p in packets
                if p["product"]["product_version_id"] == decision.product_version_id
            ),
            None,
        )
        if not packet:
            raise HTTPException(404, "Tool is not part of this review snapshot.")
        manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
        try:
            source = restricted_path(
                manifest["documents"][0]["file"], ("catalogs",), ".pdf"
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if (
            packet["snapshot_sha256"] != decision.snapshot_sha256
            or packet["source_sha256"] != manifest["documents"][0]["sha256"]
            or registry.digest(source) != packet["source_sha256"]
        ):
            raise HTTPException(
                409, "The evidence has changed. Reopen the review snapshot."
            )
        path = folder / "manual-decisions.json"
        notes = json.loads(path.read_text()) if path.is_file() else {}
        entry = {**decision.model_dump(), "saved_at": now()}
        notes[decision.product_version_id] = entry
        review.atomic_json(path, notes)
        return {"decision": entry}


@app.get("/api/runs/{job}/source-page/{page}")
def source_page(job: str, page: int):
    import fitz

    jobs.read(job)
    folder = jobs.folder(job)
    try:
        manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
        source = restricted_path(
            manifest["documents"][0]["file"], ("catalogs",), ".pdf"
        )
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, "The original catalog PDF is unavailable.")
    if registry.digest(source) != manifest["documents"][0]["sha256"]:
        raise HTTPException(409, "The source PDF has changed.")
    with fitz.open(source) as pdf:
        if page < 1 or page > len(pdf):
            raise HTTPException(404, "PDF page unavailable")
        target = folder / "manual-images" / f"page-{page}.png"
        target.parent.mkdir(exist_ok=True)
        if not target.is_file():
            pdf[page - 1].get_pixmap(dpi=150).save(target)
    return FileResponse(target, media_type="image/png")


@app.get("/api/runs/{job}/download/{filename}")
def download(job: str, filename: str):
    jobs.read(job)
    if filename not in {f["filename"] for f in jobs.files(job)}:
        raise HTTPException(404, "File unavailable")
    for path in (
        ROOTS["parsed"] / "ui-runs" / job / filename,
        ROOTS["parsed"] / "ui-runs" / job / "etl/latest" / filename,
        jobs.folder(job) / filename,
    ):
        if path.is_file():
            return FileResponse(path, filename=filename)
    raise HTTPException(404, "File unavailable")


@app.get("/api/review/tasks/{task}")
def review_task(task: str):
    if not re.fullmatch(r"[a-f0-9]{32}", task):
        raise HTTPException(404, "Review task not found")
    folder = queue() / task
    if not (folder / "request.json").is_file():
        raise HTTPException(404, "Review task not found")
    return {
        "request": json.loads((folder / "request.json").read_text()),
        "prompt": (folder / "prompt.txt").read_text(),
        "schema": json.loads((folder / "schema.json").read_text()),
        "image_url": f"/api/review/tasks/{task}/image",
    }


@app.get("/api/review/tasks/{task}/image")
def review_image(task: str):
    review_task(task)
    return FileResponse(queue() / task / "evidence-0.png")


@app.get("/api/review/tasks/{task}/packet")
def review_packet(task: str):
    evidence = review_task(task)["request"]
    folder = queue() / task
    try:
        review.validate_evidence(evidence, folder)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("prompt.txt", "schema.json", "request.json", *evidence["images"]):
            archive.write(folder / name, name)
    return Response(
        stream.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="review-{task}.zip"'},
    )


@app.post("/api/review/tasks/{task}/result")
async def submit_review(task: str, request: Request):
    evidence = review_task(task)["request"]
    folder = queue() / task
    if not evidence.get("manual_handoff", evidence.get("work_mode", False)) or any(
        (folder / f).exists() for f in ("result.json", "cancelled.json", "error.json")
    ):
        raise HTTPException(409, "This task is not waiting for a Work-mode result.")
    try:
        review.validate_evidence(evidence, folder)
        result = review.validate_result(evidence, await request.json())
        review.atomic_json(
            folder / "execution.json",
            {
                "provider": "chatgpt_work_manual"
                if evidence["model"] != "chatgpt"
                else "chatgpt_chat_manual",
                "model": evidence["model"],
                "reasoning": evidence["reasoning"],
                "context_sha256": evidence["context_sha256"],
                "provenance": "User-supplied ChatGPT response; model execution cannot be authenticated by this import.",
            },
        )
        review.atomic_json(folder / "result.json", result)
        return {"accepted": True}
    except Exception as exc:
        raise HTTPException(400, redact(str(exc)))


@app.post("/api/upload", status_code=201)
async def upload(request: Request, name: str):
    if Path(name).name != name or "\\" in name or not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Choose a PDF filename without directories.")
    folder = ROOTS["catalogs"] / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex[:12]}-{name}"
    limit = int(os.getenv("PIPELINE_UI_MAX_UPLOAD_MB", "512")) * 1024**2
    size = 0
    try:
        with path.open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "PDF exceeds the configured upload limit.")
                output.write(chunk)
        pages = len(PdfReader(path).pages)
        if pages < 1:
            raise ValueError("PDF has no pages")
        metadata = await asyncio.to_thread(
            detection.inspect,
            path,
            library().manufacturers(),
            ROOTS["catalogs"] / "library/detections",
        )
        return {
            "path": str(path),
            "name": name,
            "pages": pages,
            "bytes": size,
            "detection": metadata,
        }
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, "The uploaded file is not a readable PDF.")


if UI.is_dir():
    app.mount("/assets", StaticFiles(directory=UI), name="assets")


@app.get("/")
def index():
    return FileResponse(UI / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PIPELINE_UI_PORT", "7861")))
