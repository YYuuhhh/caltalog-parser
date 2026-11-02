#!/usr/bin/env python3
"""Register immutable PDFs, verify inventory and pin selected RAW page attempts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pypdf import PdfReader


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def resolve_document(item: Mapping[str, Any], manifest_path: Path) -> Path:
    supplied = Path(item["file"])
    candidates = (
        [supplied] if supplied.is_absolute() else [manifest_path.parent / supplied]
    )
    if supplied.is_absolute() and supplied.is_relative_to("/catalogs"):
        candidates.append(
            manifest_path.parent.parent / supplied.relative_to("/catalogs")
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"Registered PDF is absent: {item['file']}")


def validate_manifest(manifest: Mapping[str, Any], schema_path: Path) -> None:
    Draft202012Validator(
        json.loads(schema_path.read_text()), format_checker=FormatChecker()
    ).validate(manifest)


def verify_inventory(
    manifest: Mapping[str, Any], manifest_path: Path
) -> dict[str, Any]:
    seen = set()
    files = set()
    ids = set()
    for item in manifest["documents"]:
        path = resolve_document(item, manifest_path)
        sha = digest(path)
        if sha != item["sha256"]:
            raise ValueError(f"Registered PDF SHA-256 changed: {path}")
        if sha in seen or item["document_id"] in ids:
            raise ValueError("Duplicate registered document or document_id.")
        seen.add(sha)
        ids.add(item["document_id"])
        pages = len(PdfReader(path).pages)
        if pages != item["pages"]:
            raise ValueError(f"Registered PDF page count changed: {path}")
        files.add(path)
        for alias in item.get("aliases", []):
            alias_path = Path(alias)
            if not alias_path.is_absolute():
                alias_path = path.parent / alias_path
            if alias_path.is_file():
                if digest(alias_path) != sha:
                    raise ValueError(
                        f"Duplicate alias has different contents: {alias_path}"
                    )
                files.add(alias_path.resolve())
    if manifest.get("input_root"):
        root = Path(manifest["input_root"])
        if not root.is_absolute():
            root = manifest_path.parent / root
        if not root.is_dir():
            raise ValueError(f"Registered input_root is absent: {root}")
        extra = {
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        } - files
        if extra:
            raise ValueError(
                "Unregistered PDFs: " + ", ".join(str(path) for path in sorted(extra))
            )
    expected = {
        (doc["sha256"], page)
        for doc in manifest["documents"]
        for page in range(doc["pages"])
    }
    states = {}
    for state in manifest.get("page_states", []):
        key = (state["source_sha256"], state["page_idx"])
        if key not in expected or key in states:
            raise ValueError(f"Invalid or duplicate page state: {key}")
        if state["status"] == "empty" and not (
            state.get("reviewer") and state.get("reason")
        ):
            raise ValueError("Empty pages need a reviewer and reason.")
        states[key] = state["status"]
    return {"documents": len(seen), "pages": len(expected), "page_states": states}


def register(
    input_path: Path,
    *,
    catalog_id: str,
    manufacturer: str,
    role: str,
    revision: str,
    mapping_profile: str,
    families: list[str],
    languages: list[str],
) -> dict[str, Any]:
    pdfs = (
        [input_path]
        if input_path.is_file()
        else sorted(
            path
            for path in input_path.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
    )
    if not pdfs:
        raise ValueError(f"No PDFs under {input_path}")
    documents = {}
    for path in pdfs:
        sha = digest(path)
        if sha in documents:
            documents[sha]["aliases"].append(str(path.resolve()))
            continue
        pages = len(PdfReader(path).pages)
        if pages < 1:
            raise ValueError(f"PDF has no pages: {path}")
        documents[sha] = {
            "document_id": "doc-" + sha,
            "file": str(path.resolve()),
            "sha256": sha,
            "pages": pages,
            "source_pages": list(range(1, pages + 1)),
            "aliases": [],
        }
    return {
        "schema_version": "1.0",
        "catalog_id": catalog_id,
        "manufacturer": manufacturer,
        "catalog_role": role,
        "revision": revision,
        "languages": languages,
        "mapping_profile": mapping_profile,
        "tool_families": families,
        "status": "registered",
        "review_policy": "required_for_publish",
        "matcher_contract": {"version": "1.0", "status": "provisional"},
        "documents": list(documents.values()),
        "parse_selection": [],
        "page_states": [
            {"source_sha256": sha, "page_idx": page, "status": "registered"}
            for sha, doc in documents.items()
            for page in range(doc["pages"])
        ],
        **({"input_root": str(input_path.resolve())} if input_path.is_dir() else {}),
    }


def bind_raw(manifest: Mapping[str, Any], raw_path: Path) -> dict[str, Any]:
    import catalog_etl as etl

    _, records, _ = etl.load_raw_workbook(raw_path)
    available: dict[tuple[str, int], set[str]] = {}
    for record in records:
        available.setdefault((record.source_sha256, record.page_idx), set()).add(
            record.cache_key
        )
    result = copy.deepcopy(manifest)
    pinned = {
        (row["source_sha256"], row["page_idx"]): row["cache_key"]
        for row in manifest.get("parse_selection", [])
    }
    empty = {
        (row["source_sha256"], row["page_idx"]): row
        for row in manifest.get("page_states", [])
        if row["status"] == "empty"
    }
    selections = []
    states = []
    for doc in manifest["documents"]:
        for page in range(doc["pages"]):
            key = (doc["sha256"], page)
            candidates = available.get(key, set())
            cache_key = pinned.get(key)
            if cache_key is None and len(candidates) == 1:
                cache_key = next(iter(candidates))
            if key in empty and not candidates:
                states.append(empty[key])
                continue
            if cache_key not in candidates:
                raise ValueError(
                    f"Page {key} has {len(candidates)} attempts; pin an explicit cache_key before binding."
                )
            selections.append(
                {"source_sha256": key[0], "page_idx": page, "cache_key": cache_key}
            )
            states.append(
                {
                    "source_sha256": key[0],
                    "page_idx": page,
                    "status": "parsed",
                    "cache_key": cache_key,
                }
            )
    result.update(parse_selection=selections, page_states=states, status="parsed")
    return result


def save_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    data = yaml.safe_dump(dict(manifest), allow_unicode=True, sort_keys=False).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    history = path.parent / "history" / path.stem
    history.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        old = path.read_bytes()
        previous = history / (hashlib.sha256(old).hexdigest() + ".yaml")
        if not previous.exists():
            previous.write_bytes(old)
    snapshot = history / (hashlib.sha256(data).hexdigest() + ".yaml")
    if not snapshot.exists():
        snapshot.write_bytes(data)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["register", "verify", "bind-raw"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--schema", type=Path, default=Path("/schemas/catalog-manifest.schema.json")
    )
    parser.add_argument("--input", type=Path, default=Path("/catalogs/input"))
    parser.add_argument("--raw", type=Path, default=Path("/parsed/master_raw.xlsx"))
    parser.add_argument("--catalog-id")
    parser.add_argument("--manufacturer")
    parser.add_argument("--role", choices=["SOURCE", "TARGET"])
    parser.add_argument("--revision", default="unknown")
    parser.add_argument("--mapping-profile")
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--language", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            if args.manifest.exists():
                raise ValueError(
                    "Manifest exists; use a new path for a new registration."
                )
            if not all(
                [args.catalog_id, args.manufacturer, args.role, args.mapping_profile]
            ):
                raise ValueError(
                    "Registration requires catalog-id, manufacturer, role and mapping-profile."
                )
            manifest = register(
                args.input,
                catalog_id=args.catalog_id,
                manufacturer=args.manufacturer,
                role=args.role,
                revision=args.revision,
                mapping_profile=args.mapping_profile,
                families=args.family or ["unknown"],
                languages=args.language or ["ru", "en", "de"],
            )
        else:
            manifest = yaml.safe_load(args.manifest.read_text())
        validate_manifest(manifest, args.schema)
        report = verify_inventory(manifest, args.manifest)
        if args.command == "bind-raw":
            manifest = bind_raw(manifest, args.raw)
            validate_manifest(manifest, args.schema)
        if args.command != "verify":
            save_manifest(args.manifest, manifest)
        print(
            json.dumps(
                {
                    "status": "valid",
                    "documents": report["documents"],
                    "pages": report["pages"],
                    "manifest": str(args.manifest),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
