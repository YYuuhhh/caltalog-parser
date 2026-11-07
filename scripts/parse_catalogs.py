#!/usr/bin/env python3
"""Parse every PDF in a mounted catalog folder through the persistent API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import requests
from processing_control import checkpoint


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            pure = PurePosixPath(member.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"unsafe ZIP path: {member.filename}")
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError(
                    f"symlink ZIP member is not allowed: {member.filename}"
                )
            target = (destination / Path(*pure.parts)).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"ZIP path escapes destination: {member.filename}")
        archive.extractall(destination)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def check_parse_response(response: requests.Response, run_dir: Path) -> None:
    """Retain bounded error details instead of discarding the parser's cause."""
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk[: 8193 - len(body)])
                if len(body) > 8192:
                    break
            text = bytes(body[:8192]).decode("utf-8", errors="replace")
        except requests.RequestException as read_error:
            text = f"Could not read parser error response: {read_error}"
        try:
            payload = json.loads(text)
        except ValueError:
            detail = text
        else:
            detail = (
                payload.get("detail", payload.get("error", payload))
                if isinstance(payload, dict)
                else payload
            )
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
        detail = " ".join(detail.split())[:2000]
        atomic_json(
            run_dir / "error-response.json",
            {
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body": text,
                "truncated": len(body) > 8192,
            },
        )
        raise requests.HTTPError(
            f"{exc}{': ' + detail if detail else ''}", response=response
        ) from exc


def parse_one(args: argparse.Namespace, pdf: Path) -> dict[str, object]:
    if args.backend == "baidu-cpu":
        import baidu_extract

        return baidu_extract.parse_one(args, pdf)
    source_sha = sha256_file(pdf)
    run_id = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}"
    )
    run_dir = args.output / pdf.stem / source_sha / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    zip_path = run_dir / "response.zip"
    started = time.monotonic()
    fields = {
        "backend": args.backend,
        "parse_method": args.method,
        "lang_list": args.language,
        "formula_enable": str(args.formula).lower(),
        "table_enable": str(args.tables).lower(),
        "return_md": "true",
        "return_middle_json": "true",
        "return_model_output": "true",
        "return_content_list": "true",
        "return_images": "true",
        "return_original_file": "false",
        "response_format_zip": "true",
        "force": str(args.force).lower(),
    }
    if args.start_page is not None:
        fields["start_page_id"] = str(args.start_page)
    if args.end_page is not None:
        fields["end_page_id"] = str(args.end_page)
    try:
        with (
            pdf.open("rb") as handle,
            requests.post(
                f"{args.api_url.rstrip('/')}/file_parse",
                files={"files": (pdf.name, handle, "application/pdf")},
                data=fields,
                headers={"Accept": "application/zip"},
                timeout=args.timeout,
                stream=True,
            ) as response,
        ):
            check_parse_response(response, run_dir)
            content_type = response.headers.get("content-type")
            cache_hits = int(response.headers.get("X-MinerU-Cache-Hits", "0"))
            mineru_version = response.headers.get("X-MinerU-Version")
            expected_response_sha = response.headers.get("X-MinerU-Result-SHA256")
            with zip_path.open("wb") as output:
                response.raw.decode_content = True
                shutil.copyfileobj(response.raw, output, length=1024 * 1024)
        if not zipfile.is_zipfile(zip_path):
            raise RuntimeError(f"response was not a ZIP ({content_type})")
        response_sha = sha256_file(zip_path)
        if expected_response_sha and response_sha != expected_response_sha.casefold():
            raise RuntimeError(
                "downloaded ZIP SHA-256 does not match X-MinerU-Result-SHA256 "
                f"({response_sha} != {expected_response_sha})"
            )
        safe_extract(zip_path, run_dir / "files")
        result = {
            "file": str(pdf),
            "source_sha256": source_sha,
            "status": "success",
            "processing_time": round(time.monotonic() - started, 6),
            "response_zip": str(zip_path),
            "response_sha256": response_sha,
            "expected_response_sha256": expected_response_sha,
            "cache_hits": cache_hits,
            "mineru_version": mineru_version,
            "parser_backend": args.backend,
            "parse_method": args.method,
            "language": args.language,
            "table_enable": args.tables,
            "formula_enable": args.formula,
            "completed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    except Exception as exc:
        result = {
            "file": str(pdf),
            "source_sha256": source_sha,
            "status": "error",
            "processing_time": round(time.monotonic() - started, 6),
            "error": f"{type(exc).__name__}: {exc}",
            "completed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            result["http_status"] = exc.response.status_code
    atomic_json(run_dir / "manifest.json", result)
    return result


def matches_backend(artifact, args):
    metadata = list(artifact.parent.glob("files/**/cache_manifest.json"))
    if len(metadata) != 1:
        return False
    try:
        value = json.loads(metadata[0].read_text())
    except (OSError, ValueError):
        return False
    config = value.get("config", {})
    backend = (
        value.get("parser_backend")
        or config.get("effective_backend")
        or config.get("backend")
        or config.get("request", {}).get("backend")
    )
    if args.backend == "baidu-cpu":
        from baidu_cpu_runtime import REVISION

        return backend == "baidu-cpu" and all(
            config.get(k) == v
            for k, v in {
                "revision": REVISION,
                "dpi": args.ocr_dpi,
                "output_tokens": args.ocr_output_tokens,
                "table_enable": args.tables,
                "formula_enable": args.formula,
            }.items()
        )
    return backend in (None, args.backend)


def parse_manifest(args: argparse.Namespace) -> dict[str, object]:
    """Verify the complete inventory before any request and commit each page."""
    import copy
    import catalog_registry as registry
    import yaml

    manifest = yaml.safe_load(args.manifest.read_text())
    registry.validate_manifest(manifest, args.schema)
    registry.verify_inventory(manifest, args.manifest)
    start = getattr(args, "start_page", None)
    end = getattr(args, "end_page", None)
    if start is not None and start < 0 or end is not None and end < 0:
        raise ValueError("Page indexes must be nonnegative.")
    if start is not None and end is not None and start > end:
        raise ValueError("Start page must not exceed end page.")
    page_ranges = {}
    for document in manifest["documents"]:
        first = start if start is not None else 0
        last = end if end is not None else document["pages"] - 1
        if first >= document["pages"] or last >= document["pages"]:
            raise ValueError(
                f"Page range exceeds {document['file']} ({document['pages']} pages)."
            )
        page_ranges[document["sha256"]] = range(first, last + 1)
    states = {
        (row["source_sha256"], row["page_idx"]): row
        for row in manifest.get("page_states", [])
    }
    selections = {
        (row["source_sha256"], row["page_idx"]): row
        for row in manifest.get("parse_selection", [])
    }
    results = []
    for document in manifest["documents"]:
        pdf = registry.resolve_document(document, args.manifest)
        for page in page_ranges[document["sha256"]]:
            checkpoint(
                getattr(args, "control_file", None),
                getattr(args, "queue_control_file", None),
            )
            key = (document["sha256"], page)
            state = states.get(key, {})
            reuse_completed = not args.force or getattr(args, "resume_completed", False)
            if reuse_completed and state.get("status") == "empty":
                results.append(
                    {
                        "source_sha256": key[0],
                        "page_idx": page,
                        "status": "reviewed_empty",
                    }
                )
                continue
            artifact = Path(state.get("artifact_path", ""))
            if (
                reuse_completed
                and state.get("status") == "parsed"
                and key in selections
                and artifact.is_file()
                and sha256_file(artifact) == state.get("artifact_sha256")
                and matches_backend(artifact, args)
            ):
                results.append(
                    {
                        "source_sha256": key[0],
                        "page_idx": page,
                        "status": "success",
                        "cache_hit": True,
                    }
                )
                continue
            states[key] = {
                "source_sha256": key[0],
                "page_idx": page,
                "status": "processing",
            }
            manifest.update(page_states=list(states.values()), status="parsing")
            registry.save_manifest(args.manifest, manifest)
            request = copy.copy(args)
            request.start_page = request.end_page = page
            print(
                f"Parsing {pdf.name}: page {page + 1}/{document['pages']}",
                file=sys.stderr,
                flush=True,
            )
            result = parse_one(request, pdf)
            result.update(source_sha256=key[0], page_idx=page)
            if result["status"] == "success":
                try:
                    metadata_paths = list(
                        Path(result["response_zip"]).parent.glob(
                            "files/**/cache_manifest.json"
                        )
                    )
                    if len(metadata_paths) != 1:
                        raise ValueError(
                            "Page response must contain one cache_manifest.json."
                        )
                    metadata = json.loads(metadata_paths[0].read_text())
                    cache_key = metadata.get("cache_key")
                    if (
                        metadata.get("source_sha256") != key[0]
                        or metadata.get("source_page_idx") != page
                        or not isinstance(cache_key, str)
                        or len(cache_key) != 64
                        or any(char not in "0123456789abcdef" for char in cache_key)
                    ):
                        raise ValueError(
                            "Page response has no matching source/page/cache identity; update MinerU wrapper."
                        )
                    states[key] = {
                        "source_sha256": key[0],
                        "page_idx": page,
                        "status": "parsed",
                        "cache_key": cache_key,
                        "artifact_path": result["response_zip"],
                        "artifact_sha256": result["response_sha256"],
                    }
                    selections[key] = {
                        "source_sha256": key[0],
                        "page_idx": page,
                        "cache_key": cache_key,
                    }
                except (ValueError, OSError) as exc:
                    result.update(status="error", error=str(exc))
            if result["status"] != "success":
                states[key] = {
                    "source_sha256": key[0],
                    "page_idx": page,
                    "status": "failed",
                    "reason": str(result.get("error", "")),
                }
                selections.pop(key, None)
            results.append(result)
            manifest.update(
                page_states=list(states.values()),
                parse_selection=list(selections.values()),
            )
            registry.save_manifest(args.manifest, manifest)
    checkpoint(
        getattr(args, "control_file", None),
        getattr(args, "queue_control_file", None),
    )
    manifest["status"] = (
        "parsed"
        if all(row["status"] in {"parsed", "empty"} for row in states.values())
        else "parsing"
    )
    registry.save_manifest(args.manifest, manifest)
    return {
        "catalog_id": manifest["catalog_id"],
        "status": manifest["status"],
        "documents": results,
        "requested_pages_complete": all(
            result["status"] in {"success", "reviewed_empty"} for result in results
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=Path("/catalogs/input"))
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Verified catalog inventory; parse sequentially and resume per page.",
    )
    parser.add_argument(
        "--schema", type=Path, default=Path("/schemas/catalog-manifest.schema.json")
    )
    parser.add_argument("--logs-root", type=Path, default=Path("/logs"))
    parser.add_argument("--output", type=Path, default=Path("/parsed/batch"))
    parser.add_argument(
        "--api-url", default=os.environ.get("MINERU_API_URL", "http://mineru:8000")
    )
    parser.add_argument(
        "--backend",
        choices=("pipeline", "vlm-transformers", "baidu-cpu"),
        default=os.environ.get("MINERU_BACKEND", "pipeline"),
    )
    parser.add_argument("--language", default="latin")
    parser.add_argument("--ocr-dpi", type=int, default=144)
    parser.add_argument("--ocr-output-tokens", type=int, default=2048)
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--method", choices=("auto", "txt", "ocr"), default="auto")
    parser.add_argument("--start-page", type=int)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--resume-completed",
        action="store_true",
        help="Keep verified completed pages even when forcing remaining pages.",
    )
    parser.add_argument(
        "--control-file", type=Path, help="Catalog pause/stop checkpoint file."
    )
    parser.add_argument(
        "--queue-control-file", type=Path, help="Queue pause checkpoint file."
    )
    parser.add_argument("--no-tables", dest="tables", action="store_false")
    parser.add_argument("--no-formula", dest="formula", action="store_false")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.backend == "baidu-cpu":

        def stopped(signum, frame):
            raise InterruptedError("CPU extraction stopped by user.")

        signal.signal(signal.SIGTERM, stopped)
        if not args.manifest and (
            args.start_page is None or args.end_page != args.start_page
        ):
            print(
                "CPU Unlimited-OCR requires --manifest for full catalogs, or matching --start-page and --end-page for one page.",
                file=sys.stderr,
            )
            return 2
    if args.manifest:
        try:
            summary = parse_manifest(args)
            atomic_json(args.logs_root / "parse-batch-latest.json", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if summary["requested_pages_complete"] else 1
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if args.input.is_file():
        pdfs = [args.input]
    else:
        pdfs = sorted(
            path
            for path in args.input.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
    if not pdfs:
        print(f"No PDFs found under {args.input}", file=sys.stderr)
        return 2
    results = [parse_one(args, pdf) for pdf in pdfs]
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input": str(args.input),
        "documents": results,
    }
    atomic_json(args.logs_root / "parse-batch-latest.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] != "success" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
