"""Apply explicitly reviewed row additions/splits above immutable MinerU RAW."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def apply_reconstructions(
    path, schema_path, records, manifest, documents, selections, mapping
):
    import catalog_etl as etl

    if path is None:
        return list(records), [], {}, {}, set()
    path = Path(path)
    if not path.is_file():
        raise etl.ConfigurationError(f"Reconstruction file does not exist: {path}")
    validator = Draft202012Validator(
        etl.load_json(schema_path), format_checker=FormatChecker()
    )
    latest: dict[str, dict[str, Any]] = {}
    history = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            validator.validate(entry)
            moment = datetime.fromisoformat(entry["decided_at"].replace("Z", "+00:00"))
            if moment.utcoffset() is None:
                raise ValueError("decided_at requires a timezone")
            if any(
                not entry[key].strip()
                for key in ("reviewer", "reason", "evidence", "reconstruction_id")
            ):
                raise ValueError(
                    "reviewer, reason, evidence and reconstruction_id must be nonempty"
                )
            for row in entry["rows"]:
                bbox = row["bbox"]
                if (
                    not all(math.isfinite(value) for value in bbox)
                    or bbox[0] >= bbox[2]
                    or bbox[1] >= bbox[3]
                ):
                    raise ValueError(
                        "bbox must be a finite rectangle in page_1000 coordinates"
                    )
                columns = [cell["column_index"] for cell in row["cells"]]
                if len(set(columns)) != len(columns):
                    raise ValueError("duplicate reconstructed cell coordinates")
            headers = [
                {cell["column_index"]: cell["header_path"] for cell in row["cells"]}
                for row in entry["rows"]
            ]
            if any(header != headers[0] for header in headers[1:]):
                raise ValueError(
                    "all reconstructed rows must have the same header paths"
                )
            key = entry["reconstruction_id"]
            if key in latest and moment <= datetime.fromisoformat(
                latest[key]["decided_at"].replace("Z", "+00:00")
            ):
                raise ValueError("reconstruction history must advance decided_at")
            page = (entry["source_sha256"], entry["page_idx"])
            if page[0] not in documents or not 0 <= page[1] < documents[page[0]].pages:
                raise ValueError("reconstruction references an unregistered page")
            if selections.get(page) != entry["cache_key"]:
                raise ValueError(
                    "reconstruction does not match the pinned parser cache key"
                )
        except Exception as exc:
            raise etl.ConfigurationError(
                f"Invalid reconstruction line {line_number}: {exc}"
            ) from exc
        latest[key] = entry
        history.append(entry)

    effective = list(records)
    generated_decisions = {}
    superseded = {}
    resolved_proposals = set()
    replacements_seen = set()
    for entry in sorted(latest.values(), key=lambda item: item["reconstruction_id"]):
        if entry["status"] != "accepted":
            continue
        source, page = entry["source_sha256"], entry["page_idx"]
        source_records = [
            record
            for record in records
            if (record.source_sha256, record.page_idx, record.table_index)
            == (source, page, entry["table_index"])
        ]
        replaced = set(entry["replace_rows"])
        if replaced:
            if not source_records or not replaced.issubset(
                set(etl.analyze_table(source_records, mapping).data_rows)
            ):
                raise etl.ConfigurationError(
                    "Reconstruction may replace only existing data rows, not headers or absent rows."
                )
            coordinates = {
                (source, page, entry["table_index"], row) for row in replaced
            }
            if coordinates & replacements_seen:
                raise etl.ConfigurationError(
                    "Accepted reconstructions replace the same source row."
                )
            replacements_seen |= coordinates
        reconstruction_hash = etl.sha256_json(entry)
        for record in source_records:
            if record.row_index in replaced:
                superseded[record.raw_element_id] = entry["reconstruction_id"]
        effective = [
            record for record in effective if record.raw_element_id not in superseded
        ]
        table_index = (
            max(
                (
                    record.table_index
                    for record in effective
                    if (record.source_sha256, record.page_idx) == (source, page)
                ),
                default=-1,
            )
            + 1
        )
        exemplar = (
            source_records[0].values
            if source_records
            else {name: None for name in etl.export_raw.ALL_COLUMNS}
        )
        columns = sorted(
            entry["rows"][0]["cells"], key=lambda cell: cell["column_index"]
        )
        header = {
            "bbox": entry["rows"][0]["bbox"],
            "cells": [{**cell, "value": cell["header_path"][-1]} for cell in columns],
        }
        for row_index, row in enumerate([header, *entry["rows"]]):
            for cell in row["cells"]:
                values = {
                    **exemplar,
                    "source_file": documents[source].file,
                    "source_sha256": source,
                    "page_idx": page,
                    "page": page + 1,
                    "cache_key": entry["cache_key"],
                    "table_index": table_index,
                    "source_table_index": entry["table_index"],
                    "row_index": row_index,
                    "column_index": cell["column_index"],
                    "raw_value": cell["value"],
                    "raw_parameter_name": cell["header_path"][-1],
                    "header_path": cell["header_path"],
                    "article_number": entry.get("article_number"),
                    "bbox": row["bbox"],
                    "bbox_scope": "row",
                    "bbox_units": "page_1000",
                    "parser": "reviewed_reconstruction",
                    "parser_version": "1.0",
                    "parser_backend": None,
                    "artifact_path": str(path),
                    "reconstruction_id": entry["reconstruction_id"],
                    "evidence": entry["evidence"],
                    "rowspan": 1,
                    "colspan": 1,
                    "cell_tag": "th" if row_index == 0 else "td",
                }
                raw_id = etl.stable_id(
                    "raw", reconstruction_hash, row_index, cell["column_index"]
                )
                effective.append(
                    etl.RawRecord(
                        "reconstructed",
                        row_index,
                        values,
                        raw_id,
                        etl.stable_id("reconstruction_review", raw_id),
                    )
                )
                generated_decisions[raw_id] = {
                    "raw_element_id": raw_id,
                    "action": "accept_raw",
                    "reviewer": entry["reviewer"],
                    "reason": entry["reason"],
                    "decided_at": entry["decided_at"],
                }
        for proposal in entry.get("proposal_ids", []):
            proof = (proposal, source, page, entry["cache_key"])
            if any(item[0] == proposal for item in resolved_proposals):
                raise etl.ConfigurationError(
                    "A missing-element proposal is reconstructed more than once."
                )
            resolved_proposals.add(proof)
    return effective, history, generated_decisions, superseded, resolved_proposals
