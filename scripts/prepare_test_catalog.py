#!/usr/bin/env python3
"""Download the official Gühring catalog and create a reproducible page subset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

from pypdf import PdfReader, PdfWriter


DEFAULT_URL = "https://guehring.com/wp-content/downloads/DE/Kataloge-und-Sonderprogramme/GUE_Gesamtkatalog_DE.pdf"
DEFAULT_SOURCE_SHA256 = "8f700e0d591cb37b4ca60cc5c72f542c4efbb1a901b746e16057a8c3c1957b02"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_once(url: str, output: Path) -> None:
    if output.is_file() and output.stat().st_size > 0:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with urllib.request.urlopen(url, timeout=120) as response:
            while chunk := response.read(1024 * 1024):
                temporary.write(chunk)
    os.replace(temporary_path, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--expected-source-sha256",
        default=DEFAULT_SOURCE_SHA256,
        help="Refuse a silently changed upstream catalog (pass an empty value to disable).",
    )
    parser.add_argument("--source", type=Path, default=Path("/catalogs/input/GUE_Gesamtkatalog_DE.pdf"))
    parser.add_argument("--output", type=Path, default=Path("/catalogs/test/guhring_catalog_test.pdf"))
    parser.add_argument(
        "--pages",
        default="1309",
        help="Comma-separated one-based PDF page numbers (default: 1309)",
    )
    args = parser.parse_args()
    download_once(args.url, args.source)
    source_sha256 = sha256_file(args.source)
    if args.expected_source_sha256 and source_sha256 != args.expected_source_sha256:
        raise RuntimeError(
            f"source SHA-256 mismatch: expected {args.expected_source_sha256}, got {source_sha256}"
        )
    selected = [int(item.strip()) for item in args.pages.split(",") if item.strip()]
    reader = PdfReader(str(args.source))
    writer = PdfWriter()
    for one_based_page in selected:
        if not 1 <= one_based_page <= len(reader.pages):
            raise ValueError(f"page {one_based_page} is outside 1..{len(reader.pages)}")
        writer.add_page(reader.pages[one_based_page - 1])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    with temporary.open("wb") as handle:
        writer.write(handle)
    os.replace(temporary, args.output)
    metadata = {
        "source_url": args.url,
        "source_file": str(args.source),
        "source_sha256": source_sha256,
        "source_pages": selected,
        "test_file": str(args.output),
        "test_sha256": sha256_file(args.output),
        "test_pages": len(selected),
    }
    metadata_path = args.output.with_suffix(".source.json")
    metadata_temporary = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.tmp")
    metadata_temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(metadata_temporary, metadata_path)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
