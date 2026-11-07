"""Turn CPU Unlimited-OCR page output into immutable extraction/RAW artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import time
import uuid
import zipfile

from lxml import html as markup

import baidu_ocr
from baidu_cpu_runtime import MODEL, REVISION
import catalog_registry as registry
import system_diagnostics as files


def content_blocks(text, page_idx, tables=True):
    """Preserve literal cells; do not invent bounding boxes or model confidence."""
    blocks = []
    pattern = re.compile(r"<table\b.*?</table>", re.I | re.S)
    if len(re.findall(r"<table\b", text, re.I)) != len(pattern.findall(text)):
        raise ValueError(
            "Unlimited-OCR returned an incomplete table; rerun extraction with a larger output limit."
        )
    last = 0
    for match in pattern.finditer(text):
        if text[last : match.start()].strip():
            blocks.append(
                {
                    "type": "text",
                    "text": text[last : match.start()].strip(),
                    "page_idx": page_idx,
                }
            )
        if tables:
            # Parse only to verify structure, then retain original HTML as RAW.
            node = markup.fromstring(match[0])
            if not node.xpath(".//tr"):
                raise ValueError("Unlimited-OCR returned a table without rows.")
            blocks.append(
                {"type": "table", "table_body": match[0], "page_idx": page_idx}
            )
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": markup.fromstring(match[0]).text_content(),
                    "page_idx": page_idx,
                }
            )
        last = match.end()
    if text[last:].strip():
        blocks.append(
            {"type": "text", "text": text[last:].strip(), "page_idx": page_idx}
        )
    # Markdown tables are converted without numeric coercion or guessed geometry.
    result = []
    for block in blocks:
        if block["type"] != "text" or not tables:
            result.append(block)
            continue
        lines = block["text"].splitlines()
        i = 0
        prose = []
        while i < len(lines):
            if (
                i + 1 < len(lines)
                and "|" in lines[i]
                and re.fullmatch(
                    r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[i + 1]
                )
            ):
                if prose:
                    result.append(
                        {"type": "text", "text": "\n".join(prose), "page_idx": page_idx}
                    )
                    prose = []
                rows = [lines[i]]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(lines[i])
                    i += 1
                rendered = []
                for row in rows:
                    cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
                    rendered.append(
                        "<tr>"
                        + "".join(
                            "<td>"
                            + html.escape(cell.strip().replace("\\|", "|"))
                            + "</td>"
                            for cell in cells
                        )
                        + "</tr>"
                    )
                result.append(
                    {
                        "type": "table",
                        "table_body": "<table>" + "".join(rendered) + "</table>",
                        "page_idx": page_idx,
                    }
                )
            else:
                prose.append(lines[i])
                i += 1
        if prose:
            result.append(
                {"type": "text", "text": "\n".join(prose), "page_idx": page_idx}
            )
    return result


def parse_one(args, source):
    import fitz

    if (
        not 100 <= args.ocr_dpi <= 300
        or not 256 <= args.ocr_output_tokens <= 8192
        or not 0 <= args.cpu_threads <= 4096
    ):
        raise ValueError("CPU OCR parameters are outside supported limits.")
    started = time.monotonic()
    sha = registry.digest(source)
    page = args.start_page if args.start_page is not None else 0
    if args.end_page not in (None, page):
        raise ValueError("CPU OCR extraction requires one bounded page at a time.")
    folder = (
        args.output
        / source.stem
        / sha
        / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + "-"
            + uuid.uuid4().hex
        )
    )
    folder.mkdir(parents=True)
    try:
        image = folder / "page.png"
        with fitz.open(source) as pdf:
            pdf[page].get_pixmap(dpi=args.ocr_dpi).save(image)
            count = len(pdf)
        transcript = baidu_ocr.check(
            image,
            folder / "ocr.json",
            args.api_url,
            args.timeout,
            args.ocr_output_tokens,
            args.cpu_threads,
            getattr(args, "progress_file", None),
        )
        config = {
            "backend": "baidu-cpu",
            "model": MODEL,
            "revision": REVISION,
            "device": "cpu",
            "dpi": args.ocr_dpi,
            "output_tokens": args.ocr_output_tokens,
            "table_enable": args.tables,
            "formula_enable": args.formula,
            "temperature": 0,
            "adapter_version": 1,
        }
        config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest()
        key = hashlib.sha256(
            f"baidu-cpu-v1\0{sha}\0{page}\0{config_hash}".encode()
        ).hexdigest()
        metadata = {
            "cache_key": key,
            "source_sha256": sha,
            "source_file": str(source),
            "source_page_idx": page,
            "pages_total": count,
            "config_digest": config_hash,
            "parser_version": "Unlimited-OCR@" + REVISION,
            "parser_backend": "baidu-cpu",
            "config": config,
            "processing_time": time.monotonic() - started,
        }
        root = folder / "files" / source.stem
        root.mkdir(parents=True)
        files.atomic(root / "cache_manifest.json", metadata)
        files.atomic(
            root / (source.stem + "_content_list.json"),
            content_blocks(transcript["text"], page, args.tables),
        )
        files.atomic(
            root / (source.stem + "_middle.json"),
            {"pdf_info": [], "confidence_available": False},
        )
        (root / (source.stem + ".md")).write_text(transcript["text"])
        files.atomic(root / "baidu-transcript.json", transcript)
        target = folder / "response.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.iterdir()):
                archive.write(path, path.relative_to(folder / "files"))
        result = {
            "file": str(source),
            "source_sha256": sha,
            "status": "success",
            "page_idx": page,
            "response_zip": str(target),
            "response_sha256": registry.digest(target),
            "processing_time": time.monotonic() - started,
            "parser_backend": "baidu-cpu",
            "model_revision": REVISION,
        }
    except Exception as exc:
        result = {
            "file": str(source),
            "source_sha256": sha,
            "status": "error",
            "page_idx": page,
            "processing_time": time.monotonic() - started,
            "error": str(exc),
        }
    files.atomic(folder / "manifest.json", result)
    return result
