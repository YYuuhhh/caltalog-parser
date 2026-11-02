"""Cross-manufacturer regression fixtures for the README's import contract."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from openpyxl import load_workbook
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import catalog_etl as etl
import catalog_registry as registry
import compare_catalogs
import export_raw
import openai_process as op
import page_completeness as pc
import parse_catalogs

SCHEMAS = Path("/schemas") if Path("/schemas").is_dir() else ROOT / "schemas"


def pdf(path: Path, pages: int = 2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    with path.open("wb") as handle:
        writer.write(handle)


def fixture(
    tmp_path: Path,
    *,
    relation_target: str = "D-002",
    revision: str = "2026",
    role: str = "TARGET",
):
    source = tmp_path / "catalog.pdf"
    pdf(source)
    sha = registry.digest(source)
    artifact = tmp_path / "extract"
    artifact.mkdir()
    metadata = {
        "source_file": str(source),
        "source_sha256": sha,
        "mineru_version": "test",
        "parser_backend": "pipeline",
        "cache_key": "a" * 64,
    }
    (artifact / "manifest.json").write_text(json.dumps(metadata))
    tables = [
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [20, 30, 900, 950],
            "table_body": "<table><tr><th>Заказной номер</th><th>d1</th><th>Пластина</th></tr>"
            f"<tr><td>D-001</td><td>1,25</td><td>{relation_target}</td></tr></table>",
        },
        {
            "type": "table",
            "page_idx": 1,
            "bbox": [20, 30, 900, 950],
            "table_body": "<table><tr><th>Заказной номер</th><th>d1</th><th>Пластина</th></tr>"
            "<tr><td>D-002</td><td>0,75</td><td></td></tr></table>",
        },
    ]
    (artifact / "catalog_content_list.json").write_text(
        json.dumps(tables, ensure_ascii=False)
    )
    raw = tmp_path / "raw.xlsx"
    export_raw.write_workbook(export_raw.scan_outputs(artifact), raw)
    manifest = registry.register(
        source,
        catalog_id="fixture-catalog",
        manufacturer="PROMATOOL",
        role=role,
        revision=revision,
        mapping_profile="fixture",
        families=["drills"],
        languages=["ru"],
    )
    manifest.update(
        status="published",
        review_policy="optional",
        matcher_contract={"version": "1.0", "status": "approved"},
        parse_selection=[
            {"source_sha256": sha, "page_idx": index, "cache_key": "a" * 64}
            for index in range(2)
        ],
        golden_expectations={
            "metric": "source_product_rows",
            "total_expected": 2,
            "pages": [
                {
                    "source_sha256": sha,
                    "page_idx": index,
                    "expected_products": 1,
                    "known_missing_order_numbers": [],
                }
                for index in range(2)
            ],
        },
    )
    manifest["page_states"] = [
        {
            "source_sha256": sha,
            "page_idx": index,
            "status": "parsed",
            "cache_key": "a" * 64,
        }
        for index in range(2)
    ]
    manifest_path = tmp_path / "manifest.yaml"
    registry.save_manifest(manifest_path, manifest)
    mapping = {
        "mapping_schema_version": "1.0",
        "mapping_profile": "fixture",
        "mapping_version": "1.0",
        "manufacturer": "PROMATOOL",
        "matcher_contract": {
            "version": "1.0",
            "status": "approved",
            "wide_fields": ["cutting_diameter"],
        },
        "series": [
            {"article_number": "D", "family": "drills", "item_type": "component"}
        ],
        "families": [{"name": "drills", "required_fields": ["cutting_diameter"]}],
        "identifiers": [
            {
                "pattern": r"(?P<order>(?P<series>D)-\d{3})",
                "raw_headers": ["Заказной номер"],
            }
        ],
        "rules": [
            {
                "id": "diameter-rule",
                "families": ["drills"],
                "raw_headers": ["d1"],
                "canonical_field": "cutting_diameter",
                "data_type": "decimal",
                "source_unit": "cm",
                "canonical_unit": "mm",
                "criticality": "hard",
                "minimum": "0.1",
                "maximum": "100",
            }
        ],
        "relation_rules": [
            {
                "id": "insert-rule",
                "families": ["drills"],
                "raw_headers": ["Пластина"],
                "relation_type": "uses_insert",
                "review_required": False,
            }
        ],
    }
    mapping_path = tmp_path / "mapping.yaml"
    mapping_path.write_text(yaml.safe_dump(mapping, allow_unicode=True))
    config = etl.Config(
        raw,
        manifest_path,
        mapping_path,
        SCHEMAS / "catalog-manifest.schema.json",
        SCHEMAS / "field-mapping.schema.json",
        SCHEMAS / "review-decision.schema.json",
        tmp_path / "decisions.jsonl",
        tmp_path / "openai",
        tmp_path / "etl",
        tmp_path / "etl/latest",
        tmp_path / "logs",
        False,
        False,
        False,
    )
    return config, manifest, mapping


def sheet(path: Path, name: str):
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        iterator = wb[name].iter_rows(values_only=True)
        headers = next(iterator)
        return [dict(zip(headers, row)) for row in iterator]
    finally:
        wb.close()


def reconstruction_entry(manifest, *, status="accepted", replace_rows=None):
    return {
        "schema_version": "1.0",
        "reconstruction_id": "repair-first-row",
        "source_sha256": manifest["documents"][0]["sha256"],
        "page_idx": 0,
        "cache_key": "a" * 64,
        "table_index": 0,
        "replace_rows": [1] if replace_rows is None else replace_rows,
        "article_number": "D",
        "status": status,
        "reviewer": "fixture-engineer",
        "reason": "Read the original row and checked its header.",
        "evidence": "The first PDF table row states D-001 with diameter 0,5 cm.",
        "decided_at": "2026-10-07T00:00:00Z",
        "rows": [
            {
                "bbox": [20, 50, 900, 100],
                "cells": [
                    {
                        "column_index": 0,
                        "header_path": ["Заказной номер"],
                        "value": "D-001",
                    },
                    {"column_index": 1, "header_path": ["d1"], "value": "0,5"},
                    {"column_index": 2, "header_path": ["Пластина"], "value": "D-002"},
                ],
            }
        ],
    }


def test_reviewed_row_reconstruction_keeps_raw_and_decision_provenance(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    reconstruction = tmp_path / "reconstruction.jsonl"
    reconstruction.write_text(json.dumps(reconstruction_entry(manifest)) + "\n")
    config = replace(config, reconstructions_path=reconstruction)
    before = registry.digest(config.raw_path)
    report = etl.run_etl(config)
    assert report["results"]["matcher_published_rows"] == 2
    assert report["results"]["superseded_raw_cells"] == 3
    attributes = sheet(config.publish_root / "master_canonical.xlsx", "attributes")
    corrected = [row for row in attributes if row["value_decimal"] == "5"]
    assert len(corrected) == 1 and corrected[0]["decision_source"] == "reviewer"
    provenance = sheet(config.publish_root / "master_canonical.xlsx", "provenance")
    assert any(
        row["evidence_type"] == "reviewed_reconstruction"
        and "first PDF" in row["evidence"]
        for row in provenance
    )
    history = sheet(config.publish_root / "master_reviewed.xlsx", "decisions")
    assert corrected[0]["review_decision_id"] in {
        row["review_decision_id"] for row in history
    }
    assert registry.digest(config.raw_path) == before
    assert etl.run_etl(config)["cache_hit"]


def test_missing_product_can_be_added_only_through_reviewed_reconstruction(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    content = tmp_path / "extract/catalog_content_list.json"
    blocks = json.loads(content.read_text())
    blocks[0]["table_body"] = (
        "<table><tr><th>Заказной номер</th><th>d1</th><th>Пластина</th></tr></table>"
    )
    content.write_text(json.dumps(blocks))
    export_raw.write_workbook(export_raw.scan_outputs(content.parent), config.raw_path)
    assert etl.run_etl(config)["completeness"]["detected_total"] == 1
    path = tmp_path / "reconstruction.jsonl"
    entry = reconstruction_entry(manifest, replace_rows=[])
    path.write_text(json.dumps(entry))
    report = etl.run_etl(replace(config, reconstructions_path=path))
    assert report["completeness"]["detected_total"] == 2
    assert report["results"]["matcher_published_rows"] == 2


@pytest.mark.parametrize(
    "damage", ["cache", "page", "header", "duplicate_cell", "bbox", "blank_evidence"]
)
def test_invalid_reconstruction_is_rejected_before_publication(tmp_path, damage):
    config, manifest, _ = fixture(tmp_path)
    entry = reconstruction_entry(manifest)
    if damage == "cache":
        entry["cache_key"] = "b" * 64
    elif damage == "page":
        entry["page_idx"] = 8
    elif damage == "header":
        entry["replace_rows"] = [0]
    elif damage == "duplicate_cell":
        entry["rows"][0]["cells"].append(entry["rows"][0]["cells"][0])
    elif damage == "bbox":
        entry["rows"][0]["bbox"] = [200, 50, 20, 100]
    elif damage == "blank_evidence":
        entry["evidence"] = " "
    path = tmp_path / "reconstruction.jsonl"
    path.write_text(json.dumps(entry))
    with pytest.raises(etl.ConfigurationError):
        etl.run_etl(replace(config, reconstructions_path=path))
    assert not config.publish_root.exists()


def test_draft_and_rejected_reconstruction_history_does_not_override_raw(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    path = tmp_path / "reconstruction.jsonl"
    draft = reconstruction_entry(manifest, status="draft")
    path.write_text(json.dumps(draft) + "\n")
    config = replace(config, reconstructions_path=path)
    first = etl.run_etl(config)
    assert first["results"]["reconstructed_cells"] == 0
    assert first["results"]["matcher_published_rows"] == 0
    rejected = {**draft, "status": "rejected", "decided_at": "2026-10-07T00:01:00Z"}
    path.write_text(json.dumps(draft) + "\n" + json.dumps(rejected) + "\n")
    second = etl.run_etl(config)
    assert not second["cache_hit"] and second["results"]["matcher_published_rows"] == 2
    assert (
        len(sheet(config.publish_root / "master_reviewed.xlsx", "reconstructions")) == 2
    )


def openai_config(config: etl.Config) -> op.Config:
    return op.Config(
        input_path=config.raw_path,
        output_root=config.openai_root,
        logs_root=config.logs_root,
        catalogs_root=config.catalog_manifest_path.parent,
        api_base="https://api.openai.com/v1",
        api_key=None,
        model="test-model",
        reasoning_effort="medium",
        batch_rows=1,
        timeout_seconds=1,
        max_retries=0,
        validation_retries=1,
        max_output_tokens=2000,
        include_source_page=True,
        selection_manifest=config.catalog_manifest_path,
        allow_insecure_api_base=False,
        force=False,
        source_filters=(),
        source_sha_filters=(),
        source_pdf_overrides=(),
    )


class FakeCompleteness:
    def __init__(self, missing: bool = False, invalid_first: bool = False):
        self.calls = []
        self.missing = missing
        self.invalid_first = invalid_first

    def create(self, payload):
        self.calls.append(payload)
        task = json.loads(
            next(
                item["text"]
                for item in payload["input"][0]["content"]
                if item["type"] == "input_text"
            )
        )
        assert task["task"] == "page_completeness"
        result = {
            "page_quality": "warning" if self.missing else "pass",
            "summary": "Visual page audited.",
            "tables": [],
            "missing_elements": [
                {
                    "element_type": "row",
                    "proposed_value": "D-003",
                    "bbox": [10, 20, 30, 40],
                    "evidence": "D-003 printed at the bottom.",
                    "confidence": 0.99,
                }
            ]
            if self.missing
            else [],
        }
        if self.invalid_first and len(self.calls) == 1:
            result["missing_elements"] = [
                {
                    "element_type": "row",
                    "proposed_value": "D-003",
                    "bbox": [30, 20, 10, 40],
                    "evidence": "visible",
                    "confidence": 0.99,
                }
            ]
        return {
            "id": f"resp-{len(self.calls)}",
            "status": "completed",
            "model": "test-snapshot",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }, {"request_id": "req-test"}


@pytest.mark.parametrize("role", ["SOURCE", "TARGET"])
def test_multimanufacturer_export_preserves_identity_units_and_relations(
    tmp_path, role
):
    config, _, _ = fixture(tmp_path, role=role)
    before = registry.digest(config.raw_path)
    report = etl.run_etl(config)
    assert report["results"]["unique_order_numbers"] == 2
    assert report["results"]["matcher_published_rows"] == 2
    assert report["results"]["relations"] == 1
    products = sheet(config.publish_root / "master_canonical.xlsx", "products")
    assert {row["order_number"] for row in products} == {"D-001", "D-002"}
    assert {row["catalog_role"] for row in products} == {role}
    attrs = sheet(config.publish_root / "master_canonical.xlsx", "attributes")
    assert {row["value_decimal"] for row in attrs} == {"12.5", "7.5"}
    assert all(
        row["raw_unit"] == "cm" and row["canonical_unit"] == "mm" for row in attrs
    )
    relations = sheet(config.publish_root / "master_matcher.xlsx", "relations")
    assert len(relations) == 1 and relations[0]["relation_type"] == "uses_insert"
    assert (
        relations[0]["target_product_version_id"] == products[1]["product_version_id"]
    )
    assert report["results"]["table_links"] == 1
    assert registry.digest(config.raw_path) == before
    assert etl.run_etl(config)["cache_hit"]


def test_unresolved_component_blocks_source_and_never_publishes_dangling_link(tmp_path):
    config, _, _ = fixture(tmp_path, relation_target="D-999")
    report = etl.run_etl(config)
    assert report["results"]["matcher_published_rows"] == 1
    assert not sheet(config.publish_root / "master_matcher.xlsx", "relations")
    issues = sheet(config.publish_root / "master_canonical.xlsx", "issues")
    assert any(row["issue_code"] == "relation_target_unresolved" for row in issues)


def test_registration_deduplicates_bytes_and_detects_changed_or_unregistered_pdf(
    tmp_path,
):
    inputs = tmp_path / "input"
    inputs.mkdir()
    pdf(inputs / "a.pdf", 1)
    (inputs / "b.pdf").write_bytes((inputs / "a.pdf").read_bytes())
    manifest = registry.register(
        inputs,
        catalog_id="fixture-catalog",
        manufacturer="Other",
        role="SOURCE",
        revision="2026",
        mapping_profile="fixture",
        families=["drills"],
        languages=["en"],
    )
    assert len(manifest["documents"]) == 1
    registry.verify_inventory(manifest, tmp_path / "manifest.yaml")
    pdf(inputs / "extra.pdf", 2)
    with pytest.raises(ValueError, match="Unregistered"):
        registry.verify_inventory(manifest, tmp_path / "manifest.yaml")
    (inputs / "extra.pdf").unlink()
    pdf(inputs / "a.pdf", 3)
    with pytest.raises(ValueError, match="SHA-256 changed"):
        registry.verify_inventory(manifest, tmp_path / "manifest.yaml")


def test_manifest_binding_requires_unambiguous_attempt_and_retains_snapshots(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    bound = registry.bind_raw(manifest, config.raw_path)
    assert bound["status"] == "parsed" and len(bound["parse_selection"]) == 2
    registry.save_manifest(config.catalog_manifest_path, bound)
    assert len(list((tmp_path / "history/manifest").glob("*.yaml"))) == 2
    registry.validate_manifest(bound, config.manifest_schema_path)


def test_incomplete_golden_page_inventory_blocks_entire_release(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    manifest["golden_expectations"]["pages"].pop()
    registry.save_manifest(config.catalog_manifest_path, manifest)
    report = etl.run_etl(config)
    assert report["results"]["matcher_published_rows"] == 0
    assert report["completeness"]["status"] == "failed"


def test_visual_review_is_page_wide_cached_and_proposals_do_not_become_products(
    tmp_path,
):
    config, manifest, _ = fixture(tmp_path)
    manifest["require_page_completeness"] = True
    registry.save_manifest(config.catalog_manifest_path, manifest)
    fake = FakeCompleteness(missing=True)
    report = pc.process_pages(
        openai_config(config), client=fake, input_price=1, output_price=2
    )
    assert report["api_calls"] == 2 and len(report["proposals"]) == 2
    assert report["estimated_cost_this_run"] == pytest.approx(0.00028)
    for request in fake.calls:
        assert request["store"] is False
        task = json.loads(request["input"][0]["content"][-1]["text"])
        assert (
            len(task["rows"]) > 1
        )  # row batch size=1 must not truncate completeness review
        assert any(
            item["type"] == "input_file" for item in request["input"][0]["content"]
        )
    cached = pc.process_pages(openai_config(config), client=fake)
    assert cached["api_calls"] == 0 and cached["cache_hits"] == 2
    assert len(fake.calls) == 2
    # Load visual review without requiring a row-review run for this independent gate.
    issues = etl.IssueRegistry(manifest["catalog_id"], "now")
    proposed, meta = etl.load_page_completeness(
        replace(config, use_openai=True),
        manifest,
        {
            (row["source_sha256"], row["page_idx"]): row["cache_key"]
            for row in manifest["parse_selection"]
        },
        registry.digest(config.raw_path),
        issues,
    )
    assert len(proposed) == 2 and meta["status"] == "loaded"
    assert any(row["blocks_publication"] for row in issues.rows)
    exported = etl.run_etl(config)
    assert exported["results"]["identified_products"] == 2
    assert exported["results"]["matcher_published_rows"] == 0


@pytest.mark.parametrize("mode", ["row_validation", "page_completeness"])
def test_openai_dry_run_checks_real_payloads_without_key_or_network(tmp_path, mode):
    config, _, _ = fixture(tmp_path)
    before = registry.digest(config.raw_path)
    with patch.object(
        op.ResponsesClient,
        "create",
        side_effect=AssertionError("Paid request in dry run"),
    ):
        report = op.plan_requests(openai_config(config), mode)
    assert report["status"] == "dry_run" and report["api_calls"] == 0
    assert sum(request["rows"] for request in report["requests"]) == 6
    assert all(
        request["source_pdf_attached"]
        and request["strict_schema"]
        and request["store"] is False
        for request in report["requests"]
    )
    if mode == "page_completeness":
        assert len(report["requests"]) == 2
    assert not (config.openai_root / "latest.json").exists()
    assert registry.digest(config.raw_path) == before


def test_accepted_missing_proposal_requires_reconstruction_and_explicit_decision(
    tmp_path,
):
    config, manifest, _ = fixture(tmp_path)
    content = tmp_path / "extract/catalog_content_list.json"
    blocks = json.loads(content.read_text())
    blocks[0]["table_body"] = (
        "<table><tr><th>Заказной номер</th><th>d1</th><th>Пластина</th></tr></table>"
    )
    content.write_text(json.dumps(blocks))
    export_raw.write_workbook(export_raw.scan_outputs(content.parent), config.raw_path)

    class MissingFirstRow(FakeCompleteness):
        def create(self, payload):
            response, meta = super().create(payload)
            result = json.loads(response["output"][0]["content"][0]["text"])
            result["tables"] = [
                {
                    "table_index": 0,
                    "visual_product_rows": 1,
                    "visual_columns": 3,
                    "evidence": "One printed product row.",
                }
            ]
            if len(self.calls) == 1:
                result["missing_elements"][0].update(
                    proposed_value="D-001", evidence="The first product row is D-001."
                )
            else:
                result.update(page_quality="pass", missing_elements=[])
            response["output"][0]["content"][0]["text"] = json.dumps(result)
            return response, meta

    visual = pc.process_pages(
        openai_config(config), client=MissingFirstRow(missing=True)
    )
    proposal = visual["proposals"][0]["proposal_id"]
    page = {
        "source_sha256": manifest["documents"][0]["sha256"],
        "page_idx": 0,
        "action": "approve_page",
        "reviewer": "engineer",
        "reason": "The missing row is being restored.",
        "decided_at": "2026-10-07T00:00:00Z",
    }
    accept = {
        "proposal_id": proposal,
        "action": "accept_proposal",
        "reviewer": "engineer",
        "reason": "Confirmed the missing product in the PDF.",
        "decided_at": "2026-10-07T00:00:00Z",
    }
    config.decisions_path.write_text(
        json.dumps(page) + "\n" + json.dumps(accept) + "\n"
    )
    config = replace(config, use_page_completeness=True)
    pending = etl.run_etl(config)
    assert pending["results"]["matcher_published_rows"] == 0
    assert (
        sheet(config.publish_root / "master_reviewed.xlsx", "proposed_missing")[0][
            "review_status"
        ]
        == "accepted_pending_reconstruction"
    )
    reconstruction = tmp_path / "reconstruction.jsonl"
    reconstruction.write_text(
        json.dumps(
            {
                **reconstruction_entry(manifest, replace_rows=[]),
                "proposal_ids": [proposal],
            }
        )
    )
    config = replace(config, reconstructions_path=reconstruction)
    config.decisions_path.write_text(json.dumps(page) + "\n")
    with pytest.raises(etl.ConfigurationError, match="explicit accept_proposal"):
        etl.run_etl(config)
    config.decisions_path.write_text(
        json.dumps(page) + "\n" + json.dumps(accept) + "\n"
    )
    restored = etl.run_etl(config)
    assert restored["results"]["matcher_published_rows"] == 2
    assert (
        sheet(config.publish_root / "master_reviewed.xlsx", "proposed_missing")[0][
            "review_status"
        ]
        == "accepted"
    )


def test_corrupt_completeness_cache_retries_only_bad_page_and_keeps_old_attempt(
    tmp_path,
):
    config, _, _ = fixture(tmp_path)
    fake = FakeCompleteness(invalid_first=True)
    first = pc.process_pages(openai_config(config), client=fake)
    assert first["api_calls"] == 3
    root = config.openai_root / "completeness/cache"
    successes = list(root.rglob("success.json"))
    pointer = json.loads(successes[0].read_text())
    result = successes[0].parent / pointer["attempt"] / "result.json"
    result.write_text("{}")
    second = pc.process_pages(openai_config(config), client=fake)
    assert second["api_calls"] == 1 and second["cache_hits"] == 1
    assert result.is_file()


def test_visual_review_includes_page_with_no_extracted_rows(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    source = Path(manifest["documents"][0]["file"])
    pdf(source, 3)
    new_sha = registry.digest(source)
    # Write fresh RAW provenance for two populated pages of a three-page PDF.
    artifact = tmp_path / "extract/manifest.json"
    data = json.loads(artifact.read_text())
    data["source_sha256"] = new_sha
    artifact.write_text(json.dumps(data))
    export_raw.write_workbook(export_raw.scan_outputs(artifact.parent), config.raw_path)
    manifest["documents"][0].update(sha256=new_sha, pages=3, source_pages=[1, 2, 3])
    for item in manifest["parse_selection"]:
        item["source_sha256"] = new_sha
    manifest["parse_selection"].append(
        {"source_sha256": new_sha, "page_idx": 2, "cache_key": "a" * 64}
    )
    manifest["page_states"] = []
    registry.save_manifest(config.catalog_manifest_path, manifest)
    fake = FakeCompleteness()
    report = pc.process_pages(openai_config(config), client=fake)
    assert len(report["pages"]) == 3
    last = json.loads(fake.calls[-1]["input"][0]["content"][-1]["text"])
    assert last["page_idx"] == 2 and last["rows"] == []


def test_revision_comparison_tracks_attributes_and_withholds_unverified_deletions(
    tmp_path,
):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_config, _, _ = fixture(old_dir, revision="2025")
    new_config, _, _ = fixture(new_dir, revision="2026")
    etl.run_etl(old_config)
    etl.run_etl(new_config)
    old_path = old_config.publish_root / "master_canonical.xlsx"
    new_path = new_config.publish_root / "master_canonical.xlsx"
    assert compare_catalogs.compare(old_path, new_path)["changes"] == []
    # Construct a new validated canonical release with a changed diameter and a lost SKU.
    old_meta, new_sheets = compare_catalogs.read_release(new_path)
    removed = new_sheets["products"].pop()
    new_sheets["attributes"] = [
        row
        for row in new_sheets["attributes"]
        if row["product_version_id"] != removed["product_version_id"]
    ]
    new_sheets["attributes"][0]["value_decimal"] = "13"
    changed = tmp_path / "changed.xlsx"
    etl.write_validated_workbook(
        changed,
        [
            ("products", etl.PRODUCT_COLUMNS, new_sheets["products"]),
            ("attributes", etl.ATTRIBUTE_COLUMNS, new_sheets["attributes"]),
            ("relations", etl.RELATION_COLUMNS, []),
        ],
        {
            "catalog_id": old_meta["catalog_id"],
            "catalog_role": old_meta["catalog_role"],
            "catalog_revision": "2026",
        },
    )
    diff = compare_catalogs.compare(old_path, changed)
    assert diff["counts"]["possibly_missing"] == 1
    assert diff["counts"]["attribute_changed"] == 1
    assert "removed" not in diff["counts"]


def test_header_paths_and_conflicting_transform_rules_require_exact_context(tmp_path):
    config, manifest, mapping = fixture(tmp_path)
    base = mapping["rules"][0]
    base["raw_header_paths"] = [["Cutting", "d1"]]
    second = {
        **base,
        "id": "shank-rule",
        "canonical_field": "shank_diameter",
        "raw_header_paths": [["Shank", "d1"]],
    }
    mapping["rules"].append(second)
    config.mapping_path.write_text(yaml.safe_dump(mapping, allow_unicode=True))
    profile = etl.load_mapping(config, manifest)
    assert (
        profile.resolve("drills", "D", "d1", ["Cutting", "d1"]).canonical_field
        == "cutting_diameter"
    )
    assert (
        profile.resolve("drills", "D", "d1", ["Shank", "d1"]).canonical_field
        == "shank_diameter"
    )
    assert profile.resolve("drills", "D", "d1") is None
    mapping["rules"].append({**base, "id": "conflict-rule", "transform": "integer"})
    config.mapping_path.write_text(yaml.safe_dump(mapping, allow_unicode=True))
    with pytest.raises(etl.ConfigurationError, match="Conflicting"):
        etl.load_mapping(config, manifest)


def test_invalid_ranges_units_and_bounds_are_rejected(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    rule = etl.load_mapping(config, manifest).rules[0]
    converted = etl.normalize_value("1 ± 0,01", rule)
    assert converted["value_decimal"] == "10" and converted["tolerance_upper"] == "0.1"
    with pytest.raises(ValueError, match="minimum"):
        etl.normalize_value("5–1", replace(rule, data_type="range"))
    with pytest.raises(ValueError, match="conversion"):
        etl.normalize_value("1", replace(rule, source_unit="kg"))
    with pytest.raises(ValueError, match="bounds"):
        etl.normalize_value("0", rule)


def test_review_history_resolves_warning_without_changing_raw(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    _, records, _ = etl.load_raw_workbook(config.raw_path)
    record = next(item for item in records if item.values["raw_parameter_name"] == "d1")
    decision = {
        "raw_element_id": record.raw_element_id,
        "action": "accept_raw",
        "reviewer": "engineer",
        "reason": "Checked PDF",
        "decided_at": "2026-10-06T10:00:00Z",
    }
    newer = {
        **decision,
        "decided_at": "2026-10-06T11:00:00Z",
        "reason": "Reviewed again",
    }
    config.decisions_path.write_text(
        json.dumps(decision) + "\n" + json.dumps(newer) + "\n"
    )
    decisions, _ = etl.load_review_decisions(config, records)
    assert decisions[record.raw_element_id]["reason"] == "Reviewed again"
    profile = etl.load_mapping(config, manifest)
    _, docs, _ = etl.load_manifest(config)
    issues = etl.IssueRegistry(manifest["catalog_id"], "now")
    products, *_ = etl.reconstruct_catalog(
        manifest=manifest,
        documents=docs,
        mapping=profile,
        tables=etl.build_tables(records, profile),
        openai_reviews={
            record.openai_row_id: {
                "openai_verdict": "incorrect",
                "openai_issues": ["possible OCR error"],
            }
        },
        decisions=decisions,
        issues=issues,
        selected_records=records,
    )
    assert not any(
        row["issue_code"] == "openai_attribute_warning" for row in issues.rows
    )


def test_parse_manifest_resumes_valid_download_and_does_not_skip_corrupt_page(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    manifest.update(status="registered", page_states=[], parse_selection=[])
    registry.save_manifest(config.catalog_manifest_path, manifest)
    args = parse_catalogs.build_parser().parse_args(
        [
            "--manifest",
            str(config.catalog_manifest_path),
            "--schema",
            str(config.manifest_schema_path),
            "--output",
            str(tmp_path / "downloads"),
            "--logs-root",
            str(tmp_path / "logs"),
        ]
    )
    calls = []

    def parse(args, path):
        calls.append(args.start_page)
        run = args.output / f"page-{args.start_page}-attempt-{len(calls)}"
        (run / "files").mkdir(parents=True)
        archive = run / "response.zip"
        archive.write_bytes(b"validated-download")
        (run / "files/cache_manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": registry.digest(path),
                    "source_page_idx": args.start_page,
                    "cache_key": "a" * 64,
                }
            )
        )
        return {
            "status": "success",
            "response_zip": str(archive),
            "response_sha256": registry.digest(archive),
        }

    with patch.object(parse_catalogs, "parse_one", parse):
        assert parse_catalogs.parse_manifest(args)["status"] == "parsed"
        assert calls == [0, 1]
        assert parse_catalogs.parse_manifest(args)["status"] == "parsed" and calls == [
            0,
            1,
        ]
        saved = yaml.safe_load(config.catalog_manifest_path.read_text())
        Path(saved["page_states"][0]["artifact_path"]).write_bytes(b"corrupt")
        assert parse_catalogs.parse_manifest(args)["status"] == "parsed"
        assert calls == [0, 1, 0]


@pytest.mark.parametrize("action", ["resume", "stop"])
@pytest.mark.parametrize("force", [False, True])
def test_parser_pauses_after_committing_page_and_reuses_it_on_resume(
    tmp_path, action, force
):
    config, manifest, _ = fixture(tmp_path)
    manifest.update(status="registered", page_states=[], parse_selection=[])
    registry.save_manifest(config.catalog_manifest_path, manifest)
    control = tmp_path / "control.json"
    args = parse_catalogs.build_parser().parse_args(
        [
            "--manifest",
            str(config.catalog_manifest_path),
            "--schema",
            str(config.manifest_schema_path),
            "--output",
            str(tmp_path / "downloads"),
            "--control-file",
            str(control),
        ]
    )
    args.force = force
    args.resume_completed = True
    calls, errors = [], []

    def parse(args, path):
        calls.append(args.start_page)
        run = args.output / str(args.start_page)
        (run / "files").mkdir(parents=True)
        archive = run / "response.zip"
        archive.write_bytes(b"saved-page")
        (run / "files/cache_manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": registry.digest(path),
                    "source_page_idx": args.start_page,
                    "cache_key": "a" * 64,
                }
            )
        )
        if len(calls) == 1:
            parse_catalogs.atomic_json(control, {"paused": True})
        return {
            "status": "success",
            "response_zip": str(archive),
            "response_sha256": registry.digest(archive),
        }

    def run():
        try:
            parse_catalogs.parse_manifest(args)
        except Exception as exc:
            errors.append(exc)

    with patch.object(parse_catalogs, "parse_one", parse):
        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while (
                not (tmp_path / "paused.json").exists() and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert (tmp_path / "paused.json").exists()
            assert calls == [0]
            saved = yaml.safe_load(config.catalog_manifest_path.read_text())
            page = saved["page_states"][0]
            assert (
                page["status"] == "parsed"
                and registry.digest(Path(page["artifact_path"]))
                == page["artifact_sha256"]
            )
            parse_catalogs.atomic_json(
                control, {"paused": False, "stopped": action == "stop"}
            )
            worker.join(3)
            assert not worker.is_alive()
            assert len(errors) == (1 if action == "stop" else 0)
            if action == "stop":
                assert isinstance(errors[0], InterruptedError)
                assert calls == [0]
                parse_catalogs.atomic_json(control, {"paused": False, "stopped": False})
                assert parse_catalogs.parse_manifest(args)["status"] == "parsed"
            assert calls == [0, 1]
        finally:
            parse_catalogs.atomic_json(control, {"stopped": True})
            worker.join(3)


def test_page_context_keeps_note_evidence_and_invalidates_cache_when_source_artifact_changes(
    tmp_path,
):
    config, manifest, mapping = fixture(tmp_path)
    artifact = tmp_path / "extract/catalog_content_list.json"
    blocks = json.loads(artifact.read_text())
    blocks.append(
        {
            "type": "text",
            "page_idx": 0,
            "text": "Материал: Carbide",
            "bbox": [20, 20, 30, 30],
        }
    )
    blocks[0]["table_footnote"] = ["Покрытие: TiN"]
    artifact.write_text(json.dumps(blocks, ensure_ascii=False))
    mapping["context_rules"] = [
        {
            "id": "material-context",
            "families": ["drills"],
            "series": ["D"],
            "pattern": r"Материал: (?P<value>Carbide|HSS)",
            "canonical_field": "tool_material",
            "data_type": "enum",
            "allowed_values": ["Carbide", "HSS"],
            "criticality": "hard",
            "review_required": False,
        }
    ]
    mapping["context_rules"].append(
        {
            "id": "coating-context",
            "families": ["drills"],
            "series": ["D"],
            "pattern": r"Покрытие: (?P<value>TiN)",
            "canonical_field": "coating",
            "data_type": "string",
            "criticality": "hard",
            "review_required": False,
        }
    )
    config.mapping_path.write_text(yaml.safe_dump(mapping, allow_unicode=True))
    first = etl.run_etl(config)
    attributes = sheet(config.publish_root / "master_canonical.xlsx", "attributes")
    assert any(
        row["canonical_field"] == "tool_material" and row["value_text"] == "Carbide"
        for row in attributes
    )
    evidence = sheet(config.publish_root / "master_reviewed.xlsx", "context_evidence")
    assert evidence[0]["raw_value"] == "Материал: Carbide"
    assert any(row["raw_value"] == "Покрытие: TiN" for row in evidence)
    assert any(
        row["canonical_field"] == "coating" and row["value_text"] == "TiN"
        for row in attributes
    )
    blocks[-1]["text"] = "Материал: HSS"
    artifact.write_text(json.dumps(blocks, ensure_ascii=False))
    second = etl.run_etl(config)
    assert not second["cache_hit"] and second["cache_key"] != first["cache_key"]
    assert any(
        row["canonical_field"] == "tool_material" and row["value_text"] == "HSS"
        for row in sheet(config.publish_root / "master_canonical.xlsx", "attributes")
    )


def test_headerless_continuation_needs_geometry_and_preserves_header_provenance(
    tmp_path,
):
    config, manifest, _ = fixture(tmp_path)
    artifact = tmp_path / "extract/catalog_content_list.json"
    blocks = json.loads(artifact.read_text())
    blocks[1]["table_body"] = (
        "<table><tr><td>D-002</td><td>0,75</td><td></td></tr></table>"
    )
    artifact.write_text(json.dumps(blocks, ensure_ascii=False))
    export_raw.write_workbook(export_raw.scan_outputs(artifact.parent), config.raw_path)
    result = etl.run_etl(config)
    assert (
        result["results"]["table_links"] == 1
        and result["results"]["matcher_published_rows"] == 2
    )
    assert any(
        row["evidence_type"] == "inherited_header" and row["page_idx"] == 0
        for row in sheet(config.publish_root / "master_canonical.xlsx", "provenance")
    )
    blocks[1]["bbox"] = [200, 30, 800, 950]
    artifact.write_text(json.dumps(blocks, ensure_ascii=False))
    export_raw.write_workbook(export_raw.scan_outputs(artifact.parent), config.raw_path)
    second = etl.run_etl(config)
    assert second["results"]["table_links"] == 0
    assert second["results"]["matcher_published_rows"] == 0


def test_changed_source_pdf_is_rejected_even_when_etl_cache_exists(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    etl.run_etl(config)
    pdf(Path(manifest["documents"][0]["file"]), 3)
    with pytest.raises(etl.ConfigurationError, match="SHA-256 changed"):
        etl.run_etl(config)


def test_manual_missing_element_rejection_and_page_approval_are_audited(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    manifest["require_page_completeness"] = True
    registry.save_manifest(config.catalog_manifest_path, manifest)
    report = pc.process_pages(
        openai_config(config), client=FakeCompleteness(missing=True)
    )
    decisions = []
    for proposal in report["proposals"]:
        decisions.append(
            {
                "proposal_id": proposal["proposal_id"],
                "action": "reject",
                "reviewer": "engineer",
                "reason": "The printed item is an example, not a product.",
                "decided_at": "2026-10-06T10:00:00Z",
            }
        )
    for page in report["pages"]:
        decisions.append(
            {
                "source_sha256": page["source_sha256"],
                "page_idx": page["page_idx"],
                "action": "approve_page",
                "reviewer": "engineer",
                "reason": "All products accounted for.",
                "decided_at": "2026-10-06T10:00:00Z",
            }
        )
    config.decisions_path.write_text(
        "".join(json.dumps(row) + "\n" for row in decisions)
    )
    result = etl.run_etl(replace(config, use_page_completeness=True))
    assert result["results"]["matcher_published_rows"] == 2
    assert all(
        row["review_status"] == "rejected"
        for row in sheet(
            config.publish_root / "master_reviewed.xlsx", "proposed_missing"
        )
    )
    assert len(sheet(config.publish_root / "master_reviewed.xlsx", "decisions")) == 4
    # A damaged visual result must never be masked by a previously successful ETL cache.
    pointer = json.loads((config.openai_root / "completeness/latest.json").read_text())
    result_path = (
        config.openai_root / "completeness/runs" / pointer["run_id"] / "result.json"
    )
    result_path.write_text("{}")
    with pytest.raises(etl.ConfigurationError, match="corrupt"):
        etl.run_etl(replace(config, use_page_completeness=True))


class FakeRowReview:
    def __init__(self):
        self.calls = []

    def create(self, payload):
        self.calls.append(payload)
        task = json.loads(payload["input"][0]["content"][-1]["text"])
        reviews = []
        for row in task["rows"]:
            reviews.append(
                {
                    "row_id": row["row_id"],
                    "verdict": "suspicious"
                    if payload["model"] == "test-model"
                    and row["raw_value"] == "D-002"
                    and row["raw_parameter_name"] == "Заказной номер"
                    else "ok",
                    "confidence": 0.9,
                    "issues": [],
                    "processed_product_family": None,
                    "processed_article_number": None,
                    "processed_parameter_name": row["raw_parameter_name"],
                    "processed_value": row["raw_value"],
                    "evidence": "Visible source text.",
                }
            )
        result = {
            "document_quality": "warning",
            "summary": "Audited",
            "table_findings": [],
            "rows": reviews,
        }
        return {
            "id": f"resp-{len(self.calls)}",
            "status": "completed",
            "model": payload["model"],
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }, {"request_id": "req-test"}


def test_cascade_sends_only_conflicts_to_senior_and_reuses_both_model_caches(tmp_path):
    config, _, _ = fixture(tmp_path)
    fake = FakeRowReview()
    ai_config = replace(
        openai_config(config),
        batch_rows=50,
        escalation_model="senior-model",
        api_key="test-key",
    )
    first = op.process_workbook(ai_config, client=fake)
    assert first["api_calls"] == 3
    senior = [call for call in fake.calls if call["model"] == "senior-model"]
    assert len(senior) == 1
    task = json.loads(senior[0]["input"][0]["content"][-1]["text"])
    assert len(task["rows"]) == 1 and task["rows"][0]["raw_value"] == "D-002"
    second = op.process_workbook(replace(ai_config, api_key=None), client=fake)
    assert second["api_calls"] == 0
    assert len(fake.calls) == 3


def test_verified_revision_comparison_can_report_removal(tmp_path):
    config, _, _ = fixture(tmp_path)
    etl.run_etl(config)
    original = config.publish_root / "master_canonical.xlsx"
    metadata, data = compare_catalogs.read_release(original)
    data["products"].pop()
    changed = tmp_path / "new-release.xlsx"
    etl.write_validated_workbook(
        changed,
        [
            ("products", etl.PRODUCT_COLUMNS, data["products"]),
            ("attributes", etl.ATTRIBUTE_COLUMNS, []),
            ("relations", etl.RELATION_COLUMNS, []),
        ],
        {
            "catalog_id": metadata["catalog_id"],
            "catalog_role": metadata["catalog_role"],
            "catalog_revision": "2027",
        },
    )
    qa = tmp_path / "qa.json"
    # QA is tied to the exact new canonical bytes, not merely a passed status.
    qa.write_text(
        json.dumps(
            {
                "status": "success",
                "completeness": {"status": "passed"},
                "results": {"blocking_issues": 0},
                "outputs": {
                    "master_canonical.xlsx": {"sha256": registry.digest(changed)}
                },
            }
        )
    )
    diff = compare_catalogs.compare(original, changed, new_qa_path=qa)
    assert diff["counts"]["removed"] == 1 and diff["removals_verified"]
    qa.write_text(
        json.dumps(
            {
                "status": "success",
                "completeness": {"status": "passed"},
                "results": {"blocking_issues": 0},
                "outputs": {"master_canonical.xlsx": {"sha256": "0" * 64}},
            }
        )
    )
    with pytest.raises(etl.ConfigurationError, match="QA report"):
        compare_catalogs.compare(original, changed, new_qa_path=qa)


def test_all_empty_pages_have_an_explicit_reviewed_disposition(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    source = Path(manifest["documents"][0]["file"])
    pdf(source, 1)
    sha = registry.digest(source)
    artifact = tmp_path / "extract"
    (artifact / "manifest.json").write_text(
        json.dumps(
            {"source_file": str(source), "source_sha256": sha, "cache_key": "a" * 64}
        )
    )
    (artifact / "catalog_content_list.json").write_text("[]")
    export_raw.write_workbook(export_raw.scan_outputs(artifact), config.raw_path)
    manifest["documents"][0].update(sha256=sha, pages=1, source_pages=[1])
    manifest["parse_selection"] = [
        {"source_sha256": sha, "page_idx": 0, "cache_key": "a" * 64}
    ]
    manifest["page_states"] = [
        {
            "source_sha256": sha,
            "page_idx": 0,
            "status": "empty",
            "reviewer": "engineer",
            "reason": "Blank divider page.",
        }
    ]
    manifest["golden_expectations"] = {
        "metric": "source_product_rows",
        "total_expected": 0,
        "pages": [
            {
                "source_sha256": sha,
                "page_idx": 0,
                "expected_products": 0,
                "known_missing_order_numbers": [],
            }
        ],
    }
    registry.save_manifest(config.catalog_manifest_path, manifest)
    result = etl.run_etl(config)
    assert result["completeness"]["status"] == "passed"
    assert result["quality_metrics"]["reviewed_empty_pages"] == 1
    review = pc.process_pages(openai_config(config), client=FakeCompleteness())
    assert review["api_calls"] == 1 and len(review["pages"]) == 1


def test_corrupt_etl_pointer_and_cached_quality_gate_do_not_hide_failure(tmp_path):
    config, _, _ = fixture(tmp_path, relation_target="D-999")
    first = etl.run_etl(config)
    cached = etl.run_etl(replace(config, fail_on_quality_gate=True))
    assert cached["cache_hit"] and cached["quality_gate_failed"]
    pointer = config.output_root / "cache/v1" / first["cache_key"] / "success.json"
    pointer.write_text("{broken")
    second = etl.run_etl(config)
    assert not second["cache_hit"] and second["run_id"] != first["run_id"]
    assert Path(first["attempt_path"]).is_dir()


def test_fit_and_same_sign_tolerances_remain_separate_machine_values(tmp_path):
    config, manifest, _ = fixture(tmp_path)
    rule = etl.load_mapping(config, manifest).rules[0]
    same_sign = etl.normalize_value("1 -0,005/-0,015", rule)
    assert (
        same_sign["tolerance_lower"] == "-0.15"
        and same_sign["tolerance_upper"] == "-0.05"
    )
    fit = etl.normalize_value("1 h6", replace(rule, allow_fit=True))
    assert (
        fit["value_decimal"] == "10"
        and fit["fit_class"] == "h6"
        and fit["value_text"] == "1 h6"
    )
    with pytest.raises(ValueError):
        etl.normalize_value("1 h6", rule)


def test_canonical_audit_explains_field_mapping_and_source_location(tmp_path):
    import audit_catalog

    config, _, _ = fixture(tmp_path)
    etl.run_etl(config)
    result = audit_catalog.trace(
        config.publish_root / "master_canonical.xlsx",
        order_number="D-001",
        canonical_field="cutting_diameter",
    )
    assert len(result["products"]) == 1 and len(result["attributes"]) == 1
    assert result["attributes"][0]["mapping_rule_id"] == "diameter-rule"
    assert result["attributes"][0]["value_decimal"] == "12.5"
    assert all(item["page_idx"] == 0 for item in result["source_context"])
    assert any(item["evidence"] == "1,25" for item in result["source_context"])


def test_nested_page_cache_context_uses_original_physical_page_index(tmp_path):
    attempt = tmp_path / "attempt"
    extraction = attempt / "extracted/doc/auto"
    extraction.mkdir(parents=True)
    artifact = extraction / "doc_content_list.json"
    artifact.write_text(
        json.dumps([{"type": "text", "page_idx": 0, "text": "Материал: Carbide"}])
    )
    (attempt / "manifest.json").write_text(
        json.dumps({"source_sha256": "a" * 64, "page": 5, "cache_key": "b" * 64})
    )
    record = etl.RawRecord(
        "raw",
        2,
        {
            "source_sha256": "a" * 64,
            "page_idx": 5,
            "cache_key": "b" * 64,
            "artifact_path": str(artifact),
        },
        "raw-id",
        "ai-id",
    )
    context = etl.collect_context_records([record], tmp_path)
    assert len(context) == 1 and context[0].page_idx == 5
