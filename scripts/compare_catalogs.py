#!/usr/bin/env python3
"""Compare canonical releases by stable product keys without claiming unverified deletions."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from openpyxl import load_workbook
import catalog_etl as etl

IDENTIFIERS = ("article_number", "order_number", "size_code", "series")
ATTRIBUTE_VALUES = (
    "value_type",
    "value_decimal",
    "value_min",
    "value_max",
    "tolerance_lower",
    "tolerance_upper",
    "canonical_unit",
    "fit_class",
)
CHANGE_COLUMNS = [
    "kind",
    "product_key",
    "new_product_key",
    "field",
    "old_value",
    "new_value",
    "matching_basis",
    "reason",
]


def read_release(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    manifest = etl.validate_workbook(path)["manifest"]
    workbook = load_workbook(path, read_only=True, data_only=False)
    sheets = {}
    try:
        for name in ("products", "attributes", "relations"):
            if name not in workbook.sheetnames:
                raise etl.ConfigurationError(f"{path} lacks canonical sheet {name}.")
            iterator = workbook[name].iter_rows(values_only=True)
            headers = next(iterator)
            sheets[name] = [
                dict(zip(headers, row))
                for row in iterator
                if any(value is not None for value in row)
            ]
    finally:
        workbook.close()
    return manifest, sheets


def compare(
    old_path: Path,
    new_path: Path,
    *,
    key_map: Mapping[str, str] | None = None,
    new_qa_path: Path | None = None,
) -> dict[str, Any]:
    old_meta, old = read_release(old_path)
    new_meta, new = read_release(new_path)
    if old_meta.get("catalog_id") != new_meta.get("catalog_id") or old_meta.get(
        "catalog_role"
    ) != new_meta.get("catalog_role"):
        raise etl.ConfigurationError(
            "Releases must belong to the same logical catalog and role."
        )

    def indexes(release: Mapping[str, list[dict[str, Any]]]):
        products = defaultdict(list)
        attributes = defaultdict(list)
        relations = defaultdict(list)
        versions = {
            row["product_version_id"]: row["product_key"] for row in release["products"]
        }
        for row in release["products"]:
            products[row["product_key"]].append(row)
        for row in release["attributes"]:
            value = {key: row.get(key) for key in ATTRIBUTE_VALUES}
            if row.get("value_decimal") is None and row.get("value_min") is None:
                value["value_text"] = row.get("value_text")
            attributes[versions.get(row["product_version_id"])].append(
                (row["canonical_field"], value)
            )
        for row in release["relations"]:
            relations[versions.get(row["source_product_version_id"])].append(
                (
                    row["relation_type"],
                    row.get("target_product_key"),
                    row.get("target_external_reference"),
                    row.get("review_status"),
                )
            )
        return products, attributes, relations

    op, oa, orr = indexes(old)
    np, na, nrr = indexes(new)
    mapping = dict(key_map or {})
    if len(set(mapping.values())) != len(mapping) or any(
        a not in op or b not in np for a, b in mapping.items()
    ):
        raise etl.ConfigurationError(
            "Reviewed key map must be one-to-one and reference existing products."
        )
    if any(key in mapping and key in np and mapping[key] != key for key in op):
        raise etl.ConfigurationError(
            "Reviewed key map conflicts with a stable key in the new release."
        )
    verified = False
    if new_qa_path:
        qa = etl.load_json(new_qa_path)
        verified = (
            qa.get("status") == "success"
            and qa.get("completeness", {}).get("status") == "passed"
            and qa.get("results", {}).get("blocking_issues") == 0
            and qa.get("outputs", {}).get("master_canonical.xlsx", {}).get("sha256")
            == etl.sha256_file(new_path)
        )
        if not verified:
            raise etl.ConfigurationError(
                "New QA report must pass completeness, have no blockers, and match the canonical file hash."
            )
    changes = []

    def add(
        kind,
        key,
        *,
        new_key=None,
        field=None,
        before=None,
        after=None,
        basis="stable product_key",
        reason=None,
    ):
        changes.append(
            {
                "kind": kind,
                "product_key": key,
                "new_product_key": new_key,
                "field": field,
                "old_value": before,
                "new_value": after,
                "matching_basis": basis,
                "reason": reason,
            }
        )

    consumed = set()
    for key in sorted(op):
        target = mapping.get(key, key)
        before = op[key]
        after = np.get(target, [])
        if key.startswith("provisional_") or len(before) != 1 or len(after) > 1:
            add(
                "unmatched",
                key,
                new_key=target,
                reason="Provisional or duplicate identity cannot be matched automatically.",
            )
            continue
        if not after:
            add(
                "removed" if verified else "possibly_missing",
                key,
                reason="Complete new release verified."
                if verified
                else "New release completeness is unverified; disappearance may be a parser omission.",
            )
            continue
        if target in consumed:
            raise etl.ConfigurationError(
                "Two old products resolve to the same new product."
            )
        consumed.add(target)
        basis = "reviewed key map" if target != key else "stable product_key"
        for name in IDENTIFIERS:
            if before[0].get(name) != after[0].get(name):
                add(
                    "identifier_changed",
                    key,
                    new_key=target,
                    field=name,
                    before=before[0].get(name),
                    after=after[0].get(name),
                    basis=basis,
                )
        for name in (
            "family",
            "subfamily",
            "item_type",
            "standard",
            "publication_status",
        ):
            if before[0].get(name) != after[0].get(name):
                add(
                    "product_changed",
                    key,
                    new_key=target,
                    field=name,
                    before=before[0].get(name),
                    after=after[0].get(name),
                    basis=basis,
                )
        a = defaultdict(list)
        b = defaultdict(list)
        for name, value in oa.get(key, []):
            a[name].append(value)
        for name, value in na.get(target, []):
            b[name].append(value)
        for name in sorted(set(a) | set(b)):
            old_values = sorted(a[name], key=etl.canonical_bytes)
            new_values = sorted(b[name], key=etl.canonical_bytes)
            if old_values != new_values:
                add(
                    "attribute_changed",
                    key,
                    new_key=target,
                    field=name,
                    before=old_values,
                    after=new_values,
                    basis=basis,
                )
        old_relations = sorted(
            [
                (kind, mapping.get(target_key, target_key), external, status)
                for kind, target_key, external, status in orr.get(key, [])
            ],
            key=etl.canonical_bytes,
        )
        new_relations = sorted(nrr.get(target, []), key=etl.canonical_bytes)
        if old_relations != new_relations:
            add(
                "relations_changed",
                key,
                new_key=target,
                before=old_relations,
                after=new_relations,
                basis=basis,
            )
    for key in sorted(set(np) - consumed):
        if key in op and key not in mapping:
            continue
        if len(np[key]) != 1 or key.startswith("provisional_"):
            add("unmatched", key, reason="Provisional or duplicate new identity.")
        else:
            add("added", key, after=np[key][0])
    return {
        "schema_version": "1.0",
        "catalog_id": new_meta["catalog_id"],
        "old_revision": old_meta.get("catalog_revision"),
        "new_revision": new_meta.get("catalog_revision"),
        "old_sha256": etl.sha256_file(old_path),
        "new_sha256": etl.sha256_file(new_path),
        "removals_verified": verified,
        "changes": changes,
        "counts": {
            kind: sum(row["kind"] == kind for row in changes)
            for kind in sorted({row["kind"] for row in changes})
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--key-map", type=Path)
    parser.add_argument("--new-qa-report", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("/parsed/diffs"))
    args = parser.parse_args(argv)
    try:
        report = compare(
            args.old,
            args.new,
            key_map=etl.load_json(args.key_map) if args.key_map else None,
            new_qa_path=args.new_qa_report,
        )
        run = args.output_root / (etl.timestamp() + "-" + etl.sha256_json(report)[:12])
        run.mkdir(parents=True, exist_ok=False)
        etl.atomic_write_json(run / "comparison.json", report)
        etl.write_validated_workbook(
            run / "comparison.xlsx",
            [("changes", CHANGE_COLUMNS, report["changes"])],
            {key: value for key, value in report.items() if key != "changes"},
        )
        print(json.dumps({**report, "output": str(run)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, etl.ETLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
