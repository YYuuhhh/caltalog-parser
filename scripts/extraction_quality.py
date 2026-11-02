"""CPU extraction quality signals and bounded, provenance-preserving retries."""

from __future__ import annotations
import json
import re
from pathlib import Path
from statistics import mean
from lxml import html
from pypdf import PdfReader


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def artifacts(state):
    artifact_path = state.get("artifact_path")
    if not artifact_path:
        raise ValueError(
            f"PDF page {state['page_idx'] + 1} has no saved extraction artifact reference. "
            "Restore the selected artifact or rerun extraction before review."
        )
    root = Path(artifact_path).parent / "files"
    files = sorted(root.glob("**/*_content_list.json"))
    if len(files) != 1:
        raise ValueError("Pinned page needs exactly one extracted content list.")
    content = json.loads(files[0].read_text())
    middle = next(root.glob("**/*_middle.json"), None)
    return content, json.loads(middle.read_text()) if middle else {}


def measure(native_text, content, middle):
    """A diagnostic confidence score, not calibrated probability of correctness."""
    text, cells, tables, scores = [], 0, 0, []
    for record in walk(content):
        if isinstance(record.get("text"), str):
            text.append(record["text"])
        if record.get("type") == "table" and record.get("table_body"):
            tables += 1
            table = html.fromstring(record["table_body"])
            cells += len(table.xpath("//td|//th"))
            text.append(table.text_content())
    for record in walk(middle):
        # Layout detection scores do not measure text recognition quality.
        if record.get("type") == "text" and isinstance(
            record.get("score"), (float, int)
        ):
            scores.append(max(0, min(1, record["score"])))

    def token(text):
        return set(re.findall(r"\w+", text.casefold()))

    native, extracted = token(native_text), token(" ".join(text))
    coverage = len(native & extracted) / len(native) if native else None
    ocr = mean(scores) if scores else None
    available = [v for v in (coverage, ocr) if v is not None]
    confidence = min(available) if available else 0.0
    return {
        "confidence": round(confidence, 4),
        "native_text_coverage": coverage,
        "ocr_mean_confidence": ocr,
        "ocr_spans": len(scores),
        "table_cells": cells,
        "tables": tables,
        "extracted_tokens": len(extracted),
        "confidence_kind": "diagnostic; mandatory model review determines correctness",
    }


def assess(source, state):
    reader = PdfReader(source)
    content, middle = artifacts(state)
    return measure(
        reader.pages[state["page_idx"]].extract_text() or "", content, middle
    )


def retry_method(method, quality, pass_index):
    if method != "ocr":
        return "ocr"
    if quality["native_text_coverage"] is not None:
        return "txt"
    return None  # Repeating identical OCR settings is not a quality improvement.


def keep_candidate(original, candidate):
    # A retry must not lose previously captured table cells just to increase a
    # text-coverage score. Both immutable artifacts remain available for review.
    return (
        candidate["confidence"] > original["confidence"]
        and candidate["table_cells"] >= original["table_cells"]
    )
