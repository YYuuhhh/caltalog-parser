"""Confidence diagnostics, two-sheet manufacturer lists, and review evidence."""

from __future__ import annotations
import math
import statistics
import uuid
from pathlib import Path
from openpyxl import Workbook


def confidence(state):
    by_page = {
        q["page"]: {"page": q["page"], "extraction": q["confidence"], "reviews": []}
        for q in state.get("quality", [])
    }
    for task in state.get("review_tasks", []):
        value = task.get("confidence")
        if (
            isinstance(value, (int, float))
            and math.isfinite(value)
            and task["page"] in by_page
        ):
            by_page[task["page"]]["reviews"].append(value)
    pages = [
        {**p, "confidence": min([p["extraction"], *p["reviews"]])}
        for p in by_page.values()
    ]
    worst = min(pages, key=lambda p: p["confidence"]) if pages else None
    total = state.get("pages", {}).get("total", 0)
    return {
        "average": round(statistics.mean(p["confidence"] for p in pages), 4)
        if pages
        else None,
        "worst": worst["confidence"] if worst else None,
        "worst_page": worst["page"] if worst else None,
        "assessed_pages": len(pages),
        "total_pages": total,
        "coverage": len(pages) / total if total else 0,
        "pages": pages,
        "kind": "Conservative diagnostic: minimum extraction/review score per assessed page. Not a calibrated probability; missing pages and skipped checks remain unvalidated.",
    }


def value(attribute):
    for field in (
        "value_decimal",
        "value_text",
        "value_min",
        "value_integer",
        "value_boolean",
    ):
        if attribute.get(field) is not None:
            result = str(attribute[field])
            if field == "value_min" and attribute.get("value_max") is not None:
                result += "–" + str(attribute["value_max"])
            return result
    return ""


def safe_cell(item):
    # Catalog text is untrusted: Excel formula prefixes must remain plain text.
    return (
        "'" + item
        if isinstance(item, str) and item.startswith(("=", "+", "-", "@"))
        else item
    )


def manufacturer_workbook(lib, manufacturer_id, destination):
    products = lib.products(manufacturer_id)
    mapping = lib.mapping(manufacturer_id)
    fields = sorted({a["canonical_field"] for p in products for a in p["attributes"]})
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "products"
    headers = [
        "manufacturer",
        "catalog_id",
        "catalog_name",
        "product_version_id",
        "article_number",
        "order_number",
        "identity_status",
        "family",
        "series_name",
    ]
    suffixes = [
        "",
        " unit",
        " fit",
        " tolerance lower",
        " tolerance upper",
        " printed letter",
        " review",
    ]
    sheet.append(headers + [field + suffix for field in fields for suffix in suffixes])
    for product in products:
        row = [
            product.get(h, mapping["manufacturer"] if h == "manufacturer" else "")
            for h in headers
        ]
        by_field = {}
        for attribute in product["attributes"]:
            by_field.setdefault(attribute["canonical_field"], []).append(attribute)
        for field in fields:
            attributes = by_field.get(field, [])
            keys = [
                None,
                "canonical_unit",
                "fit_class",
                "tolerance_lower",
                "tolerance_upper",
                "raw_parameter_name",
                "review_status",
            ]
            row += [
                " | ".join(
                    dict.fromkeys(
                        value(a)
                        if key is None
                        else str(a.get(key) if a.get(key) is not None else "")
                        for a in attributes
                    )
                )
                for key in keys
            ]
        sheet.append([safe_cell(cell) for cell in row])
    dictionary = workbook.create_sheet("dictionary")
    columns = [
        "symbol",
        "canonical_field",
        "description",
        "unit",
        "family",
        "series",
        "status",
        "source_page",
        "source_url",
    ]
    dictionary.append(columns)
    for definition in mapping.get("symbol_dictionary", []):
        dictionary.append(
            [
                safe_cell(
                    ", ".join(definition.get(column, []))
                    if column == "series"
                    else definition.get(column, "")
                )
                for column in columns
            ]
        )
    for page in workbook:
        page.freeze_panes = "A2"
        page.auto_filter.ref = page.dimensions
        for column in page.columns:
            page.column_dimensions[column[0].column_letter].width = min(
                55, max(14, max(len(str(c.value or "")) for c in column) + 2)
            )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(uuid.uuid4().hex + ".xlsx")
    workbook.save(temporary)
    workbook.close()
    temporary.replace(destination)
    return destination


def manual_packets(
    products, attributes, provenance, reviews, catalog_id, source_sha256
):
    by_product, by_provenance = {}, {p["provenance_id"]: p for p in provenance}
    for a in attributes:
        by_product.setdefault(a["product_version_id"], []).append(a)
    packets = []
    for product in products:
        attrs = by_product.get(product["product_version_id"], [])
        pages = sorted(
            {
                int(by_provenance[a["provenance_id"]]["page_idx"]) + 1
                for a in attrs
                if a.get("provenance_id") in by_provenance
                and by_provenance[a["provenance_id"]].get("page_idx") is not None
            }
        )
        primary = by_provenance.get(product.get("primary_provenance_id"), {})
        if primary.get("page_idx") is not None:
            pages = sorted(set(pages) | {int(primary["page_idx"]) + 1})
        sources = []
        findings = []
        for review in reviews:
            if review["page"] not in pages:
                continue
            findings += review["result"].get("findings", [])
            for check in review["result"].get("checks", []):
                if check.get("source_url") and check.get("source_url").startswith(
                    "https://"
                ):
                    entry = {
                        "url": check["source_url"],
                        "evidence": check["evidence"],
                        "status": check["status"],
                        "page": review["page"],
                        "scope": "Page review evidence; not independent approval of this product.",
                    }
                    if entry not in sources:
                        sources.append(entry)
        packets.append(
            {
                "product": product,
                "parameters": attrs,
                "pages": pages,
                "internet_sources": sources,
                "findings": findings,
                "catalog_id": catalog_id,
                "source_sha256": source_sha256,
                "manual_status": "pending",
            }
        )
    return packets
