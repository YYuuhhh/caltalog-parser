#!/usr/bin/env python3
"""Real local PDF/CPU probe; deliberately contains no model review dispatch."""

from __future__ import annotations
import argparse
import json
import subprocess
import sys
import uuid
import requests
import yaml
import pipeline_ui as ui
import catalog_registry as registry
import catalog_detection as detection
import extraction_quality as quality


def run(source, first, last, api_url, *, force=False):
    source = ui.restricted_path(source, ("catalogs",), ".pdf")
    metadata = detection.inspect(
        source,
        ui.library().manufacturers(),
        ui.ROOTS["catalogs"] / "library/detections",
    )
    maker = metadata["manufacturer"]["id"] or "guhring"
    config = ui.Settings(
        source_pdf=str(source),
        manufacturer_id=maker,
        languages=",".join(metadata["language"]["codes"]) or "und",
        parser_language=detection.ocr_group(metadata["language"]["codes"]),
        page_selection=True,
        first_page=first,
        last_page=last,
        max_quality_passes=1,
        automatic_review=False,
        force_parse=force,
        mineru_url=api_url,
    )
    source, total, mapping = ui.validate_settings(config)
    health = requests.get(api_url.rstrip("/") + "/health", timeout=10)
    health.raise_for_status()
    if health.json().get("device") != "cpu":
        raise ValueError("The local probe requires the CPU service.")
    id = uuid.uuid4().hex
    folder, output = (
        ui.ROOTS["logs"] / "local-tests" / id,
        ui.ROOTS["parsed"] / "local-tests" / id,
    )
    folder.mkdir(parents=True)
    output.mkdir(parents=True)
    manifest = registry.register(
        source,
        catalog_id="local-probe-" + id,
        manufacturer=mapping["manufacturer"],
        role="SOURCE",
        revision="unknown",
        mapping_profile=mapping["mapping_profile"],
        families=["milling_cutters"],
        languages=config.languages.split(","),
    )
    registry.save_manifest(folder / "catalog.yaml", manifest)
    subprocess.run(
        ui.parse_command(config, folder, output, *config.bounds(total)), check=True
    )
    raw = output / "master_raw.xlsx"
    subprocess.run(
        [
            sys.executable,
            str(ui.SCRIPTS / "export_raw.py"),
            "--input-root",
            str(output / "downloads"),
            "--output",
            str(raw),
        ],
        check=True,
    )
    manifest = yaml.safe_load((folder / "catalog.yaml").read_text())
    scores = [
        {"page": p["page_idx"] + 1, **quality.assess(source, p)}
        for p in manifest["page_states"]
        if p["status"] == "parsed"
    ]
    report = {
        "status": "passed_local_cpu_probe",
        "scope": {"first_page": first, "last_page": last, "total_pages": total},
        "metadata": metadata,
        "quality": scores,
        "raw_cells": len(ui.workbook_rows(raw, "raw")),
        "force_extraction": force,
        "raw_workbook": str(raw),
        "work_generation_calls": 0,
        "platform_generation_calls": 0,
        "model_review": "Not executed; this probe does not claim catalog correctness or validation.",
    }
    (folder / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "report": str(folder / "report.json"),
                "raw_cells": report["raw_cells"],
                "work_generation_calls": 0,
            }
        )
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--first-page", type=int, required=True)
    parser.add_argument("--last-page", type=int, required=True)
    parser.add_argument("--api-url", default="http://mineru:8000")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Extract again using CPU instead of a cached page.",
    )
    args = parser.parse_args()
    run(args.source, args.first_page, args.last_page, args.api_url, force=args.force)
