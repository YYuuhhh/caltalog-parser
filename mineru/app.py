"""Persistent, cache-aware compatibility API in front of MinerU.

RAGFlow calls MinerU's synchronous ``/file_parse`` endpoint.  The upstream
endpoint deliberately stores temporary task files, so this proxy archives the
exact response ZIP before producing any derived/rebased output.  Cache entries
are content-addressed by document SHA-256, original page, MinerU version, and
the complete parsing configuration.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pypdf import PdfReader, PdfWriter
from starlette.datastructures import UploadFile

try:
    from mineru.version import __version__ as MINERU_VERSION
except Exception:  # pragma: no cover - health still explains a broken install
    MINERU_VERSION = os.environ.get("MINERU_VERSION", "unknown")


PARSED_ROOT = Path(os.environ.get("PARSED_ROOT", "/parsed"))
CACHE_SCHEMA = os.environ.get("MINERU_CACHE_SCHEMA", "v1")
UPSTREAM_URL = os.environ.get("MINERU_UPSTREAM_URL", "http://127.0.0.1:8001").rstrip(
    "/"
)
UPSTREAM_TIMEOUT = int(os.environ.get("MINERU_UPSTREAM_TIMEOUT", "7200"))
MODEL_REVISION = os.environ.get("MINERU_MODEL_REVISION", "unknown")
DEVICE_MODE = os.environ.get("MINERU_DEVICE_MODE", "cpu")
TABLE_MERGE = os.environ.get("MINERU_TABLE_MERGE_ENABLE", "0")
DEFAULT_BACKEND = os.environ.get("MINERU_BACKEND", "pipeline")
RUNTIME_FINGERPRINT_PATH = Path("/opt/mineru-runtime-fingerprint.txt")
RUNTIME_FINGERPRINT = (
    hashlib.sha256(RUNTIME_FINGERPRINT_PATH.read_bytes()).hexdigest()
    if RUNTIME_FINGERPRINT_PATH.is_file()
    else "unavailable"
)

# Only fields whose accepted value space is known are canonicalized.  Unknown
# fields remain byte-for-byte distinct in the cache identity so a future MinerU
# option cannot accidentally collide with another real configuration.
_BOOLEAN_CONFIG_FIELDS = {
    "client_side_output_generation",
    "formula_enable",
    "image_analysis",
    "table_enable",
}
_ENUM_CONFIG_FIELDS = {
    "backend",
    "effort",
    "lang_list",
    "parse_method",
}
_TRUE_CONFIG_VALUES = {"1", "true", "yes", "on"}
_FALSE_CONFIG_VALUES = {"0", "false", "no", "off"}

app = FastAPI(
    title="MinerU persistent parsing API",
    description="RAGFlow-compatible MinerU API with immutable raw output and deterministic page caching.",
    version=f"{MINERU_VERSION}+cache-{CACHE_SCHEMA}",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_or_copy(source: str, destination: str) -> str:
    """Publish an immutable raw archive without duplicating large artifacts."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _safe_name(name: str) -> str:
    # MinerU/RAGFlow use the exact upload stem as the outer ZIP directory.
    # Strip path components and NUL only; retain spaces, Unicode and source
    # punctuation instead of normalizing user data.
    basename = Path(name.replace("\\", "/")).name.replace("\x00", "")
    return basename if basename not in {"", ".", ".."} else "document.pdf"


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _effective_backend(value: str) -> str:
    # RAGFlow v0.27 uses the legacy name while MinerU 3.4.5 names its local
    # Transformers VLM backend "vlm-engine". Preserve both in provenance.
    normalized = value.strip().casefold()
    if normalized in {"vlm-transformers", "vlm-engine"}:
        return "vlm-engine"
    return normalized


def _canonical_config_value(key: str, value: str) -> str:
    """Return MinerU's semantic value for a known cache-identity field."""

    if key in _BOOLEAN_CONFIG_FIELDS:
        normalized = value.strip().casefold()
        if normalized in _TRUE_CONFIG_VALUES:
            return "true"
        if normalized in _FALSE_CONFIG_VALUES:
            return "false"
        # Invalid/future values must not collide merely because they resemble
        # a boolean, so preserve them exactly and let MinerU validate them.
        return value
    if key == "backend":
        return _effective_backend(value)
    if key in _ENUM_CONFIG_FIELDS:
        return value.strip().casefold()
    return value


def _request_form_provenance(form_items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Preserve exact non-file form values, including order and duplicates."""

    return [{"name": key, "value": value} for key, value in form_items]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def _page_lock(page_dir: Path) -> Iterator[None]:
    page_dir.mkdir(parents=True, exist_ok=True)
    lock_path = page_dir / ".lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_extract(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    base = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            raw_name = member.filename.replace("\\", "/")
            pure = PurePosixPath(raw_name)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"unsafe ZIP member: {member.filename}")
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError(
                    f"symlink ZIP member is not allowed: {member.filename}"
                )
            target = (destination / Path(*pure.parts)).resolve()
            if target != base and base not in target.parents:
                raise ValueError(
                    f"ZIP member escapes output directory: {member.filename}"
                )
        archive.extractall(destination)


def _find_one(root: Path, suffix: str, required: bool = False) -> Path | None:
    matches = sorted(path for path in root.rglob(f"*{suffix}") if path.is_file())
    if not matches:
        if required:
            raise ValueError(f"MinerU response is missing *{suffix}")
        return None
    return matches[0]


def _validate_success(page_dir: Path, expected_key: str) -> dict[str, Any] | None:
    pointer_path = page_dir / "success.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = _read_json(pointer_path)
        if (
            pointer.get("cache_key") != expected_key
            or pointer.get("status") != "success"
        ):
            return None
        attempt = page_dir / pointer["attempt"]
        response_path = attempt / "response.zip"
        extracted_path = attempt / "extracted"
        if not response_path.is_file() or not extracted_path.is_dir():
            return None
        if _sha256_file(response_path) != pointer.get("response_sha256"):
            return None
        raw_archive = Path(str(pointer.get("raw_archive", "")))
        raw_response = raw_archive / "response.zip"
        if not raw_archive.is_dir() or not raw_response.is_file():
            return None
        if _sha256_file(raw_response) != pointer.get("response_sha256"):
            return None
        content_path = _find_one(extracted_path, "_content_list.json", required=True)
        content = _read_json(content_path)
        if not isinstance(content, list):
            return None
        pointer["attempt_path"] = str(attempt)
        return pointer
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _config_from_form(form_items: list[tuple[str, str]]) -> tuple[dict[str, Any], str]:
    ignored = {
        "output_dir",
        "start_page_id",
        "end_page_id",
        "response_format_zip",
        "return_md",
        "return_middle_json",
        "return_model_output",
        "return_content_list",
        "return_images",
        "return_original_file",
        "force",
    }
    multi: dict[str, list[str]] = {}
    for key, value in form_items:
        if key not in ignored:
            multi.setdefault(key, []).append(str(value))
    request_config: dict[str, str | list[str]] = {
        key: values[0] if len(values) == 1 else values
        for key, values in sorted(multi.items())
    }
    request_config.setdefault("backend", DEFAULT_BACKEND)
    request_config.setdefault("effort", "medium")
    request_config.setdefault("lang_list", "ch")
    request_config.setdefault("parse_method", "auto")
    request_config.setdefault("table_enable", "true")
    request_config.setdefault("formula_enable", "true")
    request_config.setdefault("image_analysis", "true")
    request_config.setdefault("client_side_output_generation", "false")
    canonical_request: dict[str, str | list[str]] = {}
    for key, value in request_config.items():
        if isinstance(value, list):
            canonical_request[key] = [
                _canonical_config_value(key, item) for item in value
            ]
        else:
            canonical_request[key] = _canonical_config_value(key, value)
    effective_backend = canonical_request["backend"]
    config = {
        "cache_schema": CACHE_SCHEMA,
        "mineru_version": MINERU_VERSION,
        "model_revision": MODEL_REVISION,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "device_mode": DEVICE_MODE.strip().casefold(),
        "table_merge_enable": _canonical_config_value("table_enable", TABLE_MERGE),
        "effective_backend": effective_backend,
        "request": canonical_request,
    }
    return config, _sha256_bytes(_canonical_json(config))


def _cache_key(file_sha: str, page: int, config_digest: str) -> str:
    payload = (
        f"guhring-cache-{CACHE_SCHEMA}\0{file_sha}\0{page}\0{config_digest}".encode()
    )
    return hashlib.sha256(payload).hexdigest()


def _upstream_fields(form_items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    dropped = {
        "output_dir",
        "start_page_id",
        "end_page_id",
        "response_format_zip",
        "return_md",
        "return_middle_json",
        "return_model_output",
        "return_content_list",
        "return_images",
        "return_original_file",
        "force",
    }
    fields = [
        (key, _canonical_config_value(key, value))
        for key, value in form_items
        if key not in dropped
    ]
    present = {key for key, _ in fields}
    defaults = {
        "backend": _effective_backend(DEFAULT_BACKEND),
        "effort": "medium",
        "lang_list": "ch",
        "parse_method": "auto",
        "table_enable": "true",
        "formula_enable": "true",
        "image_analysis": "true",
        "client_side_output_generation": "false",
    }
    fields.extend((key, value) for key, value in defaults.items() if key not in present)
    fields.extend(
        [
            # The proxy sends an actual one-page PDF so MinerU never has to
            # reopen/rewrite a multi-thousand-page catalog for every cache key.
            ("start_page_id", "0"),
            ("end_page_id", "0"),
            ("return_md", "true"),
            ("return_middle_json", "true"),
            ("return_model_output", "true"),
            ("return_content_list", "true"),
            ("return_images", "true"),
            ("return_original_file", "false"),
            ("response_format_zip", "true"),
        ]
    )
    return fields


def _parse_page(
    *,
    file_bytes: bytes,
    filename: str,
    media_type: str,
    file_sha: str,
    page: int,
    config: dict[str, Any],
    config_digest: str,
    form_items: list[tuple[str, str]],
    force: bool,
) -> dict[str, Any]:
    key = _cache_key(file_sha, page, config_digest)
    page_dir = (
        PARSED_ROOT
        / "cache"
        / CACHE_SCHEMA
        / file_sha
        / config_digest
        / "pages"
        / f"{page:06d}"
    )
    with _page_lock(page_dir):
        if not force:
            hit = _validate_success(page_dir, key)
            if hit:
                hit["cache_hit"] = True
                return hit

        attempt_rel = (
            Path("attempts")
            / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}"
        )
        attempt = page_dir / attempt_rel
        attempt.mkdir(parents=True, exist_ok=False)
        request_meta = {
            "cache_key": key,
            "source_sha256": file_sha,
            "source_file": filename,
            "page": page,
            "upstream_page_pdf_sha256": _sha256_bytes(file_bytes),
            "upstream_page_pdf_bytes": len(file_bytes),
            "config_digest": config_digest,
            "config": config,
            "request_form": _request_form_provenance(form_items),
            "created_at": _utc_now(),
        }
        _atomic_json(attempt / "request.json", request_meta)
        started = time.monotonic()
        try:
            response = requests.post(
                f"{UPSTREAM_URL}/file_parse",
                files={"files": (filename, file_bytes, media_type)},
                data=_upstream_fields(form_items),
                headers={"Accept": "application/zip"},
                timeout=UPSTREAM_TIMEOUT,
            )
            raw_body = response.content
            if response.status_code >= 400:
                (attempt / "response.bin").write_bytes(raw_body)
                raise RuntimeError(
                    f"MinerU returned HTTP {response.status_code}: {raw_body[:500]!r}"
                )
            if not zipfile.is_zipfile(io.BytesIO(raw_body)):
                (attempt / "response.bin").write_bytes(raw_body)
                raise RuntimeError(
                    f"MinerU returned a non-ZIP response: {response.headers.get('content-type')}"
                )

            response_path = attempt / "response.zip"
            response_path.write_bytes(raw_body)
            response_sha = _sha256_bytes(raw_body)
            extracted = attempt / "extracted"
            _safe_extract(response_path, extracted)
            content_path = _find_one(extracted, "_content_list.json", required=True)
            content = _read_json(content_path)
            if not isinstance(content, list):
                raise ValueError("MinerU content_list must be a JSON list")

            elapsed = round(time.monotonic() - started, 6)
            manifest = {
                **request_meta,
                "status": "success",
                "response_sha256": response_sha,
                "response_content_type": response.headers.get("content-type"),
                "processing_time": elapsed,
                "completed_at": _utc_now(),
            }
            raw_dir = (
                PARSED_ROOT
                / "raw"
                / file_sha
                / config_digest
                / f"page-{page + 1:06d}"
                / attempt.name
            )
            manifest["raw_archive"] = str(raw_dir)
            _atomic_json(attempt / "manifest.json", manifest)

            raw_dir.parent.mkdir(parents=True, exist_ok=True)
            # Each archive path is unique and never replaced. Hard links keep
            # the exact raw bytes visible under both cache/ and raw/ without
            # doubling storage; copy2 is the cross-filesystem fallback.
            shutil.copytree(attempt, raw_dir, copy_function=_link_or_copy)

            pointer = {
                "cache_key": key,
                "status": "success",
                "attempt": str(attempt_rel),
                "response_sha256": response_sha,
                "processing_time": elapsed,
                "completed_at": manifest["completed_at"],
                "raw_archive": str(raw_dir),
            }
            _atomic_json(page_dir / "key.json", request_meta)
            _atomic_json(page_dir / "success.json", pointer)
            pointer["attempt_path"] = str(attempt)
            pointer["cache_hit"] = False
            return pointer
        except Exception as exc:
            _atomic_json(
                attempt / "manifest.json",
                {
                    **request_meta,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "processing_time": round(time.monotonic() - started, 6),
                    "completed_at": _utc_now(),
                },
            )
            raise


def _replace_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    return value


def _load_optional_json(root: Path, suffix: str) -> Any | None:
    path = _find_one(root, suffix)
    return _read_json(path) if path else None


def _merge_json_pages(values: list[Any], key: str | None = None) -> Any:
    present = [value for value in values if value is not None]
    if not present:
        return None
    if key and all(
        isinstance(value, dict) and isinstance(value.get(key), list)
        for value in present
    ):
        merged = {name: item for name, item in present[0].items() if name != key}
        merged[key] = [entry for value in present for entry in value[key]]
        return merged
    if all(isinstance(value, list) for value in present):
        return [entry for value in present for entry in value]
    return present


def _write_zip_from_tree(tree: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(item for item in tree.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(tree).as_posix())


def _derive_document(
    *,
    filename: str,
    file_bytes: bytes,
    file_sha: str,
    config_digest: str,
    config: dict[str, Any],
    start_page: int,
    end_page: int,
    page_results: list[dict[str, Any]],
    return_flags: dict[str, bool],
    request_form: list[dict[str, str]],
) -> tuple[Path, dict[str, Any]]:
    source_stem = Path(_safe_name(filename)).stem
    derivation = {
        "source_sha256": file_sha,
        "config_digest": config_digest,
        "start_page": start_page,
        "end_page": end_page,
        "page_artifacts": [item["response_sha256"] for item in page_results],
        "response_schema": 2,
        "return_flags": return_flags,
    }
    derivation_id = _sha256_bytes(_canonical_json(derivation))
    output_dir = (
        PARSED_ROOT
        / "documents"
        / source_stem
        / file_sha
        / config_digest
        / "requests"
        / f"pages-{start_page + 1:06d}-{end_page + 1:06d}"
        / derivation_id
    )
    zip_path = output_dir / "result.zip"
    manifest_path = output_dir / "manifest.json"
    if zip_path.is_file() and manifest_path.is_file():
        manifest = _read_json(manifest_path)
        if _sha256_file(zip_path) == manifest.get("result_sha256"):
            # The derived archive is immutable, but cache-hit headers describe
            # this request rather than the request which first built it.
            return zip_path, {
                **manifest,
                "cache_hits": sum(bool(item.get("cache_hit")) for item in page_results),
                "served_from_cache": all(
                    bool(item.get("cache_hit")) for item in page_results
                ),
                "request_form": request_form,
            }

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="derive-", dir=output_dir) as temporary:
        tree = Path(temporary) / "tree"
        backend = str(config.get("request", {}).get("backend", "pipeline")).lower()
        method = str(config.get("request", {}).get("parse_method", "auto")).lower()
        if backend == "pipeline":
            result_mode = method if method in {"auto", "txt", "ocr"} else "auto"
        elif backend.startswith("hybrid"):
            result_mode = (
                f"hybrid_{method if method in {'auto', 'txt', 'ocr'} else 'auto'}"
            )
        else:
            result_mode = "vlm"
        parse_dir = tree / source_stem / result_mode
        images_dir = parse_dir / "images"
        images_dir.mkdir(parents=True)

        content_pages: list[Any] = []
        content_v2_pages: list[Any] = []
        middle_pages: list[Any] = []
        model_pages: list[Any] = []
        markdown_pages: list[str] = []

        for relative_page, result in enumerate(page_results):
            extracted = Path(result["attempt_path"]) / "extracted"
            replacements: dict[str, str] = {}
            if return_flags["return_images"]:
                for image_path in sorted(
                    path
                    for path in extracted.rglob("*")
                    if path.is_file() and "images" in path.parts
                ):
                    new_name = f"p{relative_page + 1:06d}_{image_path.name}"
                    target = images_dir / new_name
                    if not target.exists():
                        shutil.copy2(image_path, target)
                    replacements[f"images/{image_path.name}"] = f"images/{new_name}"

            content = _load_optional_json(extracted, "_content_list.json") or []
            content = _replace_paths(content, replacements)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    block["page_idx"] = relative_page
                    block["source_page_idx"] = start_page + relative_page
            content_pages.append(content)

            content_v2 = _load_optional_json(extracted, "_content_list_v2.json")
            content_v2_pages.append(_replace_paths(content_v2, replacements))

            middle = _load_optional_json(extracted, "_middle.json")
            if isinstance(middle, dict):
                for page_info in middle.get("pdf_info", []):
                    if isinstance(page_info, dict):
                        page_info["page_idx"] = relative_page
                        page_info["source_page_idx"] = start_page + relative_page
            middle_pages.append(_replace_paths(middle, replacements))

            model = _load_optional_json(extracted, "_model.json")
            model_pages.append(_replace_paths(model, replacements))

            md_path = _find_one(extracted, ".md")
            if md_path:
                markdown_pages.append(
                    _replace_paths(md_path.read_text(encoding="utf-8"), replacements)
                )

        flat_content = [
            entry for page in content_pages if isinstance(page, list) for entry in page
        ]
        if return_flags["return_content_list"]:
            (parse_dir / f"{source_stem}_content_list.json").write_text(
                json.dumps(flat_content, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            merged_v2 = _merge_json_pages(content_v2_pages)
            if merged_v2 is not None:
                (parse_dir / f"{source_stem}_content_list_v2.json").write_text(
                    json.dumps(merged_v2, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        if return_flags["return_middle_json"]:
            merged_middle = _merge_json_pages(middle_pages, key="pdf_info")
            if merged_middle is not None:
                (parse_dir / f"{source_stem}_middle.json").write_text(
                    json.dumps(merged_middle, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        if return_flags["return_model_output"]:
            merged_model = _merge_json_pages(model_pages)
            if merged_model is not None:
                (parse_dir / f"{source_stem}_model.json").write_text(
                    json.dumps(merged_model, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        if return_flags["return_md"]:
            (parse_dir / f"{source_stem}.md").write_text(
                "\n\n".join(markdown_pages), encoding="utf-8"
            )
        if return_flags["return_original_file"]:
            original_name = f"{source_stem}_origin{Path(_safe_name(filename)).suffix}"
            (parse_dir / original_name).write_bytes(file_bytes)

        derived_meta = {
            **derivation,
            "cache_schema": CACHE_SCHEMA,
            "parser": "MinerU",
            "mineru_version": MINERU_VERSION,
            "config": config,
            "request_form": request_form,
            "source_file": filename,
            "pages": end_page - start_page + 1,
            "cache_hits": sum(bool(item.get("cache_hit")) for item in page_results),
            "processing_time": round(
                sum(float(item.get("processing_time", 0)) for item in page_results), 6
            ),
            "created_at": _utc_now(),
            "page_cache_keys": [item["cache_key"] for item in page_results],
            **(
                {
                    "cache_key": page_results[0]["cache_key"],
                    "source_page_idx": start_page,
                }
                if len(page_results) == 1
                else {}
            ),
        }
        (parse_dir / "cache_manifest.json").write_text(
            json.dumps(derived_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        staged_zip = output_dir / f".{uuid.uuid4().hex}.zip.tmp"
        _write_zip_from_tree(tree, staged_zip)
        os.replace(staged_zip, zip_path)

    manifest = {
        **derived_meta,
        "result_sha256": _sha256_file(zip_path),
        "result_zip": str(zip_path),
    }
    _atomic_json(manifest_path, manifest)
    files_dir = output_dir / "files"
    if not files_dir.exists():
        _safe_extract(zip_path, files_dir)
    return zip_path, manifest


def _parse_document(
    *,
    file_bytes: bytes,
    filename: str,
    media_type: str,
    form_items: list[tuple[str, str]],
    force: bool,
) -> tuple[Path, dict[str, Any]]:
    try:
        source_buffer = io.BytesIO(file_bytes)
        reader = PdfReader(source_buffer)
        page_count = len(reader.pages)
    except Exception as exc:
        raise ValueError(f"cannot read PDF page count: {exc}") from exc
    if page_count < 1:
        raise ValueError("PDF contains no pages")

    values: dict[str, str] = {}
    for key, value in form_items:
        values[key] = value
    start_page = max(0, int(values.get("start_page_id", "0")))
    requested_end = int(values.get("end_page_id", str(page_count - 1)))
    end_page = min(page_count - 1, requested_end)
    if start_page > end_page:
        raise ValueError(
            f"invalid page range {start_page}..{requested_end} for {page_count} pages"
        )

    file_sha = _sha256_bytes(file_bytes)
    config, config_digest = _config_from_form(form_items)
    page_results: list[dict[str, Any]] = []
    for page in range(start_page, end_page + 1):
        one_page = io.BytesIO()
        writer = PdfWriter()
        writer.add_page(reader.pages[page])
        writer.write(one_page)
        page_results.append(
            _parse_page(
                file_bytes=one_page.getvalue(),
                filename=filename,
                media_type=media_type,
                file_sha=file_sha,
                page=page,
                config=config,
                config_digest=config_digest,
                form_items=form_items,
                force=force,
            )
        )
    return_flags = {
        "return_md": _bool(values.get("return_md"), True),
        "return_middle_json": _bool(values.get("return_middle_json"), True),
        "return_model_output": _bool(values.get("return_model_output"), False),
        "return_content_list": _bool(values.get("return_content_list"), True),
        "return_images": _bool(values.get("return_images"), True),
        "return_original_file": _bool(values.get("return_original_file"), False),
    }
    derive_lock = (
        PARSED_ROOT
        / "documents"
        / Path(_safe_name(filename)).stem
        / file_sha
        / config_digest
        / ".derive-lock"
    )
    with _page_lock(derive_lock):
        return _derive_document(
            filename=filename,
            file_bytes=file_bytes,
            file_sha=file_sha,
            config_digest=config_digest,
            config=config,
            start_page=start_page,
            end_page=end_page,
            page_results=page_results,
            return_flags=return_flags,
            request_form=_request_form_provenance(form_items),
        )


def _upstream_health() -> tuple[bool, dict[str, Any]]:
    try:
        response = requests.get(f"{UPSTREAM_URL}/health", timeout=3)
        try:
            body = response.json()
        except ValueError:
            body = {"body": response.text[:500]}
        detail = body if isinstance(body, dict) else {"body": body}
        detail["http_status"] = response.status_code
        return response.status_code == 200, detail
    except requests.RequestException as exc:
        return False, {"error": str(exc)}


@app.get("/health")
def health() -> JSONResponse:
    ready, detail = _upstream_health()
    # Retain upstream protocol fields used by MinerU clients while annotating
    # the persistent wrapper's own state.
    payload = {
        **detail,
        "status": "healthy" if ready else "starting",
        "mineru_version": MINERU_VERSION,
        "device": DEVICE_MODE,
        "model_revision": MODEL_REVISION,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "cache_schema": CACHE_SCHEMA,
        "upstream": detail,
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


@app.head("/openapi.json", include_in_schema=False)
def openapi_head() -> Response:
    ready, _ = _upstream_health()
    return Response(status_code=200 if ready else 503)


@app.get("/cache/status")
def cache_status() -> dict[str, Any]:
    success_files = list(
        (PARSED_ROOT / "cache" / CACHE_SCHEMA).glob("*/*/pages/*/success.json")
    )
    return {
        "schema": CACHE_SCHEMA,
        "successful_pages": len(success_files),
        "root": str(PARSED_ROOT),
    }


@app.post("/file_parse")
async def file_parse(request: Request):
    form = await request.form()
    uploads: list[UploadFile] = []
    form_items: list[tuple[str, str]] = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            if key == "files":
                uploads.append(value)
        else:
            form_items.append((key, str(value)))
    if not uploads:
        raise HTTPException(
            status_code=422,
            detail="at least one multipart field named 'files' is required",
        )
    if len(uploads) != 1:
        raise HTTPException(
            status_code=422,
            detail="the persistent API currently accepts one PDF per request",
        )

    upload = uploads[0]
    file_bytes = await upload.read()
    force = any(key == "force" and _bool(value) for key, value in form_items)
    try:
        zip_path, manifest = await asyncio.to_thread(
            _parse_document,
            file_bytes=file_bytes,
            filename=upload.filename or "document.pdf",
            media_type=upload.content_type or "application/pdf",
            form_items=form_items,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502, detail=f"MinerU upstream request failed: {exc}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"persistent parsing failed: {type(exc).__name__}: {exc}",
        ) from exc

    response_format_zip = True
    for key, value in form_items:
        if key == "response_format_zip":
            response_format_zip = _bool(value, True)
    if response_format_zip:
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"{Path(upload.filename or 'document').stem}_mineru.zip",
            headers={
                "X-MinerU-Version": MINERU_VERSION,
                "X-MinerU-Cache-Hits": str(manifest["cache_hits"]),
                "X-MinerU-Result-SHA256": manifest["result_sha256"],
            },
        )

    return JSONResponse(
        {
            "backend": config_backend(manifest),
            "version": MINERU_VERSION,
            "results": manifest,
        }
    )


def config_backend(manifest: dict[str, Any]) -> str:
    request_config = manifest.get("config", {}).get("request", {})
    return str(request_config.get("backend", _effective_backend(DEFAULT_BACKEND)))
