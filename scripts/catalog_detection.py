"""Small, local upload preflight; never calls a language model or external API."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

import fitz
from langdetect import DetectorFactory, detect_langs

DetectorFactory.seed = 0
ALIASES = {
    "guhring": ["guhring", "guehring", "gühring", "гюринг"],
    "sandvik-coromant": ["sandvik coromant", "sandvik"],
    "walter": ["walter tools", "walter-tools", "walter"],
    "kennametal": ["kennametal"],
    "osg": ["osg corporation", "osg", "osgtool"],
    "sumitomo": ["sumitomo electric", "sumitomo"],
}


def normalized(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(
        r"[^\w]+", " ", "".join(c for c in text if not unicodedata.combining(c))
    ).strip()


def identify(texts, manufacturers, filename=""):
    """Repeated source evidence wins; competing brands remain a suggestion."""
    candidates = []
    for maker in manufacturers:
        aliases = {
            normalized(maker["name"]),
            normalized(maker["id"]),
            *map(normalized, ALIASES.get(maker["id"], [])),
        }
        aliases = {a for a in aliases if len(a) >= 3}
        pages = [
            i + 1
            for i, text in enumerate(texts)
            if any(
                re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", normalized(text))
                for a in aliases
            )
        ]
        file_match = any(
            re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", normalized(filename))
            for a in aliases
        )
        if pages or file_match:
            candidates.append(
                {
                    "id": maker["id"],
                    "name": maker["name"],
                    "pages": pages,
                    "confidence": min(0.98, 0.82 + 0.07 * len(pages))
                    if pages
                    else 0.45,
                }
            )
    candidates.sort(key=lambda c: (-len(c["pages"]), -c["confidence"], c["name"]))
    best = candidates[0] if candidates else None
    certain = (
        best
        and best["pages"]
        and (len(candidates) == 1 or len(best["pages"]) > len(candidates[1]["pages"]))
    )
    return {
        "id": best["id"] if certain else None,
        "name": best["name"] if certain else None,
        "confidence": best["confidence"] if certain else 0,
        "candidates": candidates[:5],
    }


def languages(texts):
    votes = {}
    for text in texts:
        cleaned = re.sub(r"https?://\S+|\b\d+[\d.,/-]*\b", " ", text)
        if len(re.findall(r"[^\W\d_]", cleaned)) < 60:
            continue
        try:
            for result in detect_langs(cleaned[:12000]):
                if result.prob >= 0.2:
                    votes[result.lang] = votes.get(result.lang, 0) + result.prob
        except Exception:
            continue
    total = sum(votes.values())
    ranked = sorted(votes, key=votes.get, reverse=True)
    chosen = [code for code in ranked if votes[code] >= max(0.35, total * 0.15)]
    return {
        "codes": chosen,
        "confidence": round(votes[ranked[0]] / total, 4) if total else 0,
        "method": "local language detection",
    }


def ocr(page, folder, index, available):
    image = folder / f"page-{index}.png"
    page.get_pixmap(dpi=120, alpha=False).save(image)
    groups = [code for code in ("eng", "deu", "rus") if code in available]
    process = subprocess.run(
        [
            "tesseract",
            str(image),
            "stdout",
            "-l",
            "+".join(groups) or "eng",
            "--psm",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return process.stdout if process.returncode == 0 else ""


def inspect(source, manufacturers, cache_root, *, allow_ocr=True):
    started = time.monotonic()
    source = Path(source)
    directory_hash = hashlib.sha256(
        json.dumps(
            [(m["id"], m["name"]) for m in manufacturers], sort_keys=True
        ).encode()
    ).hexdigest()
    # This cache only serves upload suggestions; run provenance still hashes the PDF.
    stamp = {"size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns}
    cache_index = Path(cache_root) / (
        "source-"
        + hashlib.sha256(str(source.resolve()).encode()).hexdigest()
        + "-"
        + directory_hash[:12]
        + ".json"
    )

    def usable(value):
        return value.get("total_pages") is not None and (
            value.get("ocr_attempted")
            or not allow_ocr
            or value.get("manufacturer", {}).get("id")
            and value.get("language", {}).get("codes")
        )

    try:
        entry = json.loads(cache_index.read_text())
        if entry["stamp"] == stamp and usable(entry["result"]):
            return {**entry["result"], "cached": True}
    except (OSError, ValueError, KeyError):
        pass
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    path = Path(cache_root) / f"{sha}-{directory_hash[:12]}.json"
    if path.is_file():
        cached = json.loads(path.read_text())
        if usable(cached):
            save_cache(cache_index, {"stamp": stamp, "result": cached})
            return {**cached, "cached": True}
    texts, used_ocr, warnings = [], [], []
    with fitz.open(source) as pdf:
        total_pages = len(pdf)
        # Only the cover and two opening pages: bounded independently of size.
        indices = list(range(min(3, len(pdf))))
        texts = [pdf[i].get_text()[:18000] for i in indices]
        native = identify(texts, manufacturers, source.name)
        lang = languages(texts)
        if (
            allow_ocr
            and (not native["id"] or not lang["codes"])
            and shutil.which("tesseract")
        ):
            try:
                available = subprocess.run(
                    ["tesseract", "--list-langs"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.split()
                with tempfile.TemporaryDirectory(prefix="catalog-preflight-") as folder:
                    for index in indices:
                        if (
                            len(re.findall(r"\w+", texts[index])) < 20
                            or index == 0
                            and not native["id"]
                        ):
                            texts[index] += "\n" + ocr(
                                pdf[index], Path(folder), index, available
                            )
                            used_ocr.append(index + 1)
            except (subprocess.TimeoutExpired, OSError):
                warnings.append(
                    "Fast OCR was incomplete; choose missing metadata manually."
                )
        elif allow_ocr and not shutil.which("tesseract"):
            warnings.append("Fast OCR is unavailable; native PDF text was checked.")
    result = {
        "source_sha256": sha,
        "total_pages": total_pages,
        "manufacturer": identify(texts, manufacturers, source.name),
        "language": languages(texts),
        "sampled_pages": [i + 1 for i in indices],
        "ocr_pages": used_ocr,
        "ocr_attempted": allow_ocr,
        "method": "native text + fast CPU OCR" if used_ocr else "native PDF text",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "warnings": warnings,
        "cached": False,
    }
    save_cache(path, result)
    save_cache(cache_index, {"stamp": stamp, "result": result})
    return result


def save_cache(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def ocr_group(codes):
    if set(codes) & {"ru", "uk", "be"}:
        return "east_slavic"
    if set(codes) & {"ar", "fa", "ur"}:
        return "arabic"
    if "ko" in codes:
        return "korean"
    return "ch"
