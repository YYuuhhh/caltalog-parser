#!/usr/bin/env python3
"""Trace canonical fields to mapping and PDF evidence, with optional RAGFlow retrieval."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from openpyxl import load_workbook
import catalog_etl as etl


def trace(
    path: Path,
    *,
    order_number: str | None = None,
    product_key: str | None = None,
    canonical_field: str | None = None,
) -> dict[str, Any]:
    metadata = etl.validate_workbook(path)["manifest"]
    workbook = load_workbook(path, read_only=True, data_only=False)

    def rows(name):
        iterator = workbook[name].iter_rows(values_only=True)
        headers = next(iterator)
        return [
            dict(zip(headers, row))
            for row in iterator
            if any(value is not None for value in row)
        ]

    try:
        products = [
            row
            for row in rows("products")
            if (order_number is None or row["order_number"] == order_number)
            and (product_key is None or row["product_key"] == product_key)
        ]
        versions = {row["product_version_id"] for row in products}
        attributes = [
            row
            for row in rows("attributes")
            if row["product_version_id"] in versions
            and (canonical_field is None or row["canonical_field"] == canonical_field)
        ]
        evidence_ids = {row["provenance_id"] for row in attributes}
        evidence_ids.update(row["primary_provenance_id"] for row in products)
        provenance = [
            row for row in rows("provenance") if row["provenance_id"] in evidence_ids
        ]
        return {
            "catalog_id": metadata.get("catalog_id"),
            "catalog_revision": metadata.get("catalog_revision"),
            "canonical_sha256": etl.sha256_file(path),
            "products": products,
            "attributes": attributes,
            "provenance": provenance,
            "source_context": [
                {
                    "source_file": row["source_file"],
                    "document_id": row["document_id"],
                    "page_idx": row["page_idx"],
                    "pdf_page": row["page"],
                    "previous_pdf_page": max(1, (row["page"] or 1) - 1),
                    "next_pdf_page": (row["page"] or 1) + 1,
                    "bbox": row["bbox"],
                    "evidence": row["evidence"],
                }
                for row in provenance
            ],
        }
    finally:
        workbook.close()


def retrieve(
    base_url: str,
    api_key: str,
    question: str,
    dataset_ids: list[str],
    *,
    session: Any = requests,
) -> dict[str, Any]:
    if not api_key or not dataset_ids:
        raise ValueError(
            "RAGFlow retrieval needs RAGFLOW_API_KEY and at least one dataset ID."
        )
    response = session.post(
        base_url.rstrip("/") + "/api/v1/retrieval",
        headers={"Authorization": "Bearer " + api_key},
        json={
            "question": question,
            "dataset_ids": dataset_ids,
            "page": 1,
            "page_size": 20,
            "highlight": True,
        },
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("code") not in (None, 0):
        raise ValueError(
            "RAGFlow retrieval failed: " + str(result.get("message", "unknown error"))
        )
    return result.get("data", {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canonical",
        type=Path,
        default=Path("/parsed/etl/latest/master_canonical.xlsx"),
    )
    parser.add_argument("--order-number")
    parser.add_argument("--product-key")
    parser.add_argument("--field")
    parser.add_argument("--query")
    parser.add_argument("--dataset-id", action="append", default=[])
    parser.add_argument(
        "--ragflow-url", default=os.getenv("RAGFLOW_URL", "http://ragflow")
    )
    args = parser.parse_args(argv)
    try:
        if not (args.order_number or args.product_key or args.query):
            raise ValueError("Specify order-number, product-key or query.")
        report = (
            trace(
                args.canonical,
                order_number=args.order_number,
                product_key=args.product_key,
                canonical_field=args.field,
            )
            if (args.order_number or args.product_key)
            else {}
        )
        if args.query:
            report["ragflow_context"] = retrieve(
                args.ragflow_url,
                os.getenv("RAGFLOW_API_KEY", ""),
                args.query,
                args.dataset_id,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, etl.ETLError, requests.RequestException) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
