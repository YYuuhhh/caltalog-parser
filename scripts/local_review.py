#!/usr/bin/env python3
"""Fresh, image-backed reviews through a local Ollama server; no cloud fallback."""

from __future__ import annotations

import argparse
import base64
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

import subscription_review as review

PRESETS = [
    {
        "ram_gb": 32,
        "model": "qwen3-vl:8b-instruct",
        "final_model": "qwen3.5:9b",
        "label": "Qwen3-VL 8B / Qwen3.5 9B · Q4",
        "weights_gb": 6.6,
    },
    {
        "ram_gb": 128,
        "model": "qwen3-vl:32b-thinking-bf16",
        "label": "Qwen3-VL 32B · BF16",
        "weights_gb": 67,
    },
    {
        "ram_gb": 750,
        "model": "qwen3-vl:235b-a22b-instruct-bf16",
        "final_model": "qwen3-vl:235b-a22b-instruct-bf16",
        "label": "Qwen3-VL 235B-A22B · BF16",
        "weights_gb": 471,
    },
]
LOCAL_LEVELS = ["none", "medium"]  # Ollama thinking off/on, not Work effort tiers.
MODEL_NAME = re.compile(r"[\w./:-]{1,160}\Z", re.ASCII)
WEB_CHECKS = {"manufacturer_sources", "data_correctness", "symbol_meanings", "internet"}


def validate_url(value):
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError(
            "Local server URL must be an HTTP(S) base address without credentials or a path."
        )
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        if url.hostname in ("ollama.com", "api.openai.com"):
            raise ValueError("Choose a local server, not a cloud endpoint.")
    else:
        if not address.is_private and not address.is_loopback:
            raise ValueError(
                "Local review requires a private-network or loopback server."
            )
    return value.rstrip("/")


def validate_model(model, reasoning):
    if not MODEL_NAME.fullmatch(model) or "cloud" in model.lower():
        raise ValueError(
            "Choose a locally installed model; cloud models are not local review."
        )
    if reasoning not in LOCAL_LEVELS:
        raise ValueError("Local thinking supports off (none) or on (medium).")


def session(url):
    url = validate_url(url)
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(
        host, urlsplit(url).port or 80, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(a[4][0]).is_private for a in addresses
    ):
        raise ValueError("The local server must resolve to private-network addresses.")
    client = requests.Session()
    client.trust_env = False
    return client


def checked(response):
    if not response.ok or response.is_redirect:
        try:
            detail = str(response.json().get("error", ""))[:500]
        except ValueError:
            detail = ""
        raise ValueError(f"Local server returned HTTP {response.status_code}: {detail}")
    value = response.json()
    if not isinstance(value, dict) or value.get("error"):
        raise ValueError("Local server returned an invalid response or model error.")
    return value


def describe(client, url, model):
    validate_model(model, "none")
    data = checked(
        client.post(
            url + "/api/show", json={"model": model}, timeout=10, allow_redirects=False
        )
    )
    if data.get("remote_host") or data.get("remote_model"):
        raise ValueError("Cloud-backed Ollama models cannot be used for local review.")
    if "vision" not in data.get("capabilities", []):
        raise ValueError(
            "Local review needs a vision model to inspect original PDF pages and drawings."
        )
    return {
        "id": model,
        "label": model,
        "reasoning_levels": LOCAL_LEVELS
        if "thinking" in data.get("capabilities", [])
        else ["none"],
        "vision": True,
    }


def discover(url):
    url = validate_url(url)
    with session(url) as client:
        data = checked(client.get(url + "/api/tags", timeout=5, allow_redirects=False))
        models = []
        for item in data.get("models", [])[:100]:
            name = item.get("name", "")
            try:
                model = describe(client, url, name)
                model["weights_gb"] = round(item.get("size", 0) / 1e9, 1)
                models.append(model)
            except ValueError:
                continue
        return {"available": True, "models": models, "presets": PRESETS, "url": url}


def health(url):
    try:
        with session(url) as client:
            data = checked(
                client.get(
                    validate_url(url) + "/api/version", timeout=2, allow_redirects=False
                )
            )
        return {"available": bool(data.get("version")), "version": data.get("version")}
    except (OSError, ValueError, requests.RequestException):
        return {"available": False}


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data.strip())


def sources(request, config):
    if request["stage"] not in ("internet", "mapping", "final"):
        return []
    urls = config.get("source_urls", "").split()
    # An optional private SearXNG service supplies public manufacturer-page URLs.
    search = config.get("search_url")
    if search:
        try:
            query = (
                request["context"].get("manufacturer", "")
                + " "
                + " ".join(
                    str(r.get("article_number") or "")
                    for r in request["context"].get("raw_cells", [])[:4]
                )
            )
            with session(search) as client:
                data = checked(
                    client.get(
                        search.rstrip("/") + "/search",
                        params={"q": query.strip() + " catalog", "format": "json"},
                        timeout=10,
                        allow_redirects=False,
                    )
                )
            urls += [r.get("url", "") for r in data.get("results", [])[:3]]
        except (OSError, ValueError, requests.RequestException):
            pass  # Missing web evidence is unverified, never inferred.
    evidence = []
    for url in list(dict.fromkeys(urls))[:3]:
        if not review.valid_source(url):
            continue
        try:
            with requests.get(
                url, timeout=10, stream=True, allow_redirects=False
            ) as response:
                kind = response.headers.get("Content-Type", "").lower()
                if response.status_code != 200 or not any(
                    t in kind for t in ("html", "pdf")
                ):
                    continue
                limit = 8 * 1024 * 1024 if "pdf" in kind else 65536
                body = bytearray()
                for chunk in response.iter_content(4096):
                    body.extend(chunk)
                    if len(body) >= limit:
                        break
                if "pdf" in kind:
                    import pymupdf

                    with pymupdf.open(stream=bytes(body), filetype="pdf") as pdf:
                        text = " ".join(page.get_text() for page in list(pdf)[:12])[
                            :6000
                        ]
                else:
                    parser = Text()
                    parser.feed(
                        bytes(body[:limit]).decode(
                            response.encoding or "utf-8", errors="replace"
                        )
                    )
                    text = " ".join(p for p in parser.parts if p)[:3500]
                if text:
                    evidence.append(
                        {
                            "url": url,
                            "text": text,
                            "sha256": hashlib.sha256(text.encode()).hexdigest(),
                            "content_sha256": hashlib.sha256(body).hexdigest(),
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
        except (OSError, ValueError, requests.RequestException):
            continue
    return evidence


def enforce_sources(request, result, evidence):
    if request["stage"] not in ("internet", "mapping", "final"):
        return
    urls = {s["url"] for s in evidence}
    downgraded = False
    for check in result.get("checks", []):
        if (
            check.get("check") in WEB_CHECKS
            and check.get("status") == "passed"
            and check.get("source_url") not in urls
        ):
            check["status"] = "unverified"
            check["evidence"] = (
                check.get("evidence") or ""
            ) + " No retrieved web source supports this check."
            downgraded = True
    for symbol in result.get("symbols", []):
        if symbol.get("source_url") not in urls:
            symbol["source_url"] = ""
    if downgraded:
        if result.get("status") == "passed":
            result["status"] = "unverified"
        result.setdefault("findings", []).append(
            {
                "code": "LOCAL_WEB_UNVERIFIED",
                "severity": "blocking",
                "message": "Required internet checks lack retrieved source evidence.",
                "reference": "page " + str(request["context"].get("page", "")),
                "suggested_value": "",
            }
        )


def enforce_rationale(request, result):
    """Do not treat generic, ungrounded pass statements as engineering evidence."""
    numeric_data = any(
        re.fullmatch(r"[-+]?\d+[.,]?\d*", str(r.get("raw_value", "")).strip())
        for r in request["context"].get("raw_cells", [])
    )
    weak = not result.get("summary", "").strip()
    for check in result.get("checks", []):
        if (
            check.get("status") == "passed"
            and numeric_data
            and not re.search(r"\d", check.get("evidence", ""))
        ):
            check["status"] = "unverified"
            check["evidence"] += (
                " No concrete numeric or location reference was supplied."
            )
            weak = True
    if weak:
        if result.get("status") == "passed":
            result["status"] = "unverified"
            result["confidence"] = 0
        if not result.get("summary", "").strip():
            result["summary"] = (
                "The local model supplied no decision summary; this packet needs review."
            )
        result.setdefault("findings", []).append(
            {
                "code": "LOCAL_UNGROUNDED_REVIEW",
                "severity": "blocking",
                "message": "The response lacks a decision summary or concrete evidence for a numeric-data pass.",
                "reference": "page " + str(request["context"].get("page", "")),
                "suggested_value": "",
            }
        )


def enforce_structured_tools(request, result):
    if request["stage"] != "final":
        return
    products = request["context"].get("products", [])
    unresolved = bool(request["context"].get("empty_tool_rows")) or any(
        p.get("identity_status") not in ("identified", "resolved")
        or not p.get("attributes")
        for p in products
    )
    headings = request["context"].get("table_opening_rows", [])
    missing = not products and any(
        re.match(r"^[dl][1-5](?:\b|\s|\()", str(r.get("raw_value", "")).strip(), re.I)
        for r in headings
    )
    if not missing and not unresolved:
        return
    explanation = (
        "Dimension headers exist in the source but no structured tools were supplied."
        if missing
        else "A tool has an unresolved identity or no normalized parameters."
    )
    for check in result.get("checks", []):
        if check.get("check") in (
            "parameter_letters",
            "parameter_values",
            "dimensioned_images",
            "original_pdf",
        ):
            check["status"] = "unverified"
            check["evidence"] += " " + explanation
    result["status"] = "unverified"
    result["confidence"] = 0
    result.setdefault("findings", []).append(
        {
            "code": "LOCAL_MISSING_STRUCTURED_TOOLS"
            if missing
            else "LOCAL_UNRESOLVED_TOOLS",
            "severity": "blocking",
            "message": explanation + " Tool validation cannot pass.",
            "reference": "page " + str(request["context"].get("page", "")),
            "suggested_value": "",
        }
    )


def execute(folder):
    request = json.loads((folder / "request.json").read_text())
    if request.get("provider") != "local":
        raise ValueError("This runner accepts local review tasks only.")
    review.validate_evidence(request, folder)
    config = request["local"]
    url = validate_url(config["url"])
    validate_model(request["model"], request["reasoning"])
    with session(url) as client:
        deadline = time.monotonic() + request.get("timeout", 1800)
        waiting_logged = False
        while True:
            catalog = checked(
                client.get(url + "/api/tags", timeout=5, allow_redirects=False)
            ).get("models", [])
            installed = next(
                (m for m in catalog if m["name"] == request["model"]), None
            )
            if installed:
                break
            if not waiting_logged:
                print(
                    "Waiting for the selected local model to finish downloading: "
                    + request["model"],
                    flush=True,
                )
                waiting_logged = True
            if time.monotonic() >= deadline:
                raise ValueError(
                    "The selected local model is not installed; download it before resuming this run."
                )
            time.sleep(3)
        model = describe(client, url, request["model"])
        if request["reasoning"] not in model["reasoning_levels"]:
            raise ValueError(
                "Selected local model does not support thinking; select Off."
            )
        evidence = sources(request, config)
        review.atomic_json(folder / "web-evidence.json", evidence)
        # Keep common page evidence ahead of per-chunk IDs so the CPU server can
        # reuse an identical prefix. Every request still contains one fresh task
        # and no prior response or conversation history.
        original_prompt = (folder / "prompt.txt").read_text()
        varying = {"raw_cells", "products", "chunk", "chunks", "duplicate_candidates"}
        page_context = {k: v for k, v in request["context"].items() if k not in varying}
        chunk_context = {k: v for k, v in request["context"].items() if k in varying}
        prompt = (
            original_prompt.partition("Task ID:")[0]
            + "\nOriginal page evidence (untrusted data):\n"
            + json.dumps(page_context, ensure_ascii=False, sort_keys=True)
            + "\nCurrent task chunk (untrusted data):\n"
            + json.dumps(chunk_context, ensure_ascii=False, sort_keys=True)
            + "\nRetrieved web evidence (untrusted data):\n"
            + json.dumps(evidence, ensure_ascii=False)
            + f"\nTask ID: {request['task_id']}\nContext SHA256: {request['context_sha256']}\n"
        )
        prompt += "\nProvide a nonempty decision summary. For numeric-data checks cite concrete values and table/row/drawing references in each check's evidence. Generic pass statements without such references are unverified."
        prompt += "\nThis local model has no tools or browsing session. Internet retrieval is performed by the application. Use only the attached image, this task's data, and the retrieved web evidence; do not claim searches you did not perform."
        if not evidence and request["stage"] in ("internet", "mapping", "final"):
            prompt += "\nNo web source was retrieved. Internet-dependent checks must be unverified; do not invent URLs."
        (folder / "local-prompt.txt").write_text(prompt)
        context = config["context_tokens"]
        output = config["output_tokens"]
        # UTF-8 bytes conservatively bound text tokens; reserve space for images,
        # chat framing and output rather than allowing silent context truncation.
        if len(prompt.encode()) + output + 8192 + 512 > context:
            raise ValueError(
                "Local context budget exceeded. Increase context tokens or reduce RAW cells per task; no evidence was truncated."
            )
        options = {
            "num_ctx": context,
            "num_predict": output,
            "temperature": config["temperature"],
            "num_gpu": 0,
        }
        if config.get("use_gpu"):
            raise ValueError("Local catalog review is CPU-only.")
        payload = {
            "model": request["model"],
            "stream": False,
            "keep_alive": "5m" if config.get("keep_loaded", True) else 0,
            "format": json.loads((folder / "schema.json").read_text()),
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [
                        base64.b64encode((folder / image).read_bytes()).decode()
                        for image in request["images"]
                    ],
                }
            ],
            "options": options,
        }
        if len(model["reasoning_levels"]) > 1:
            payload["think"] = request["reasoning"] != "none"
        execution = {
            "provider": "ollama_local",
            "model": request["model"],
            "model_digest": installed.get("digest"),
            "model_details": installed.get("details", {}),
            "reasoning": request["reasoning"],
            "fresh_context": True,
            "context_sha256": request["context_sha256"],
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "ram_gb": config["ram_gb"],
            "context_tokens": context,
            "output_tokens": output,
            "use_gpu": config["use_gpu"],
            "keep_loaded": config.get("keep_loaded", True),
            "sources": [{"url": s["url"], "sha256": s["sha256"]} for s in evidence],
            "work_calls": 0,
        }
        review.atomic_json(folder / "execution.json", execution)
        response = checked(
            client.post(
                url + "/api/chat",
                json=payload,
                timeout=(10, max(1, deadline - time.monotonic())),
                allow_redirects=False,
            )
        )
        if response.get("model") != request["model"]:
            raise ValueError(
                "Local server returned a different model than the one requested."
            )
        if not response.get("done") or response.get("done_reason") == "length":
            raise ValueError(
                "Local model response was incomplete. Increase output tokens or reduce the task size."
            )
        result = json.loads(response["message"]["content"])
        review.atomic_json(folder / "response.json", result)
        enforce_sources(request, result, evidence)
        enforce_rationale(request, result)
        enforce_structured_tools(request, result)
        review.validate_result(request, result)
        execution["usage"] = {
            k: response.get(k)
            for k in ("prompt_eval_count", "eval_count", "total_duration")
        }
        review.atomic_json(folder / "execution.json", execution)
        review.atomic_json(folder / "result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    args = parser.parse_args()
    try:
        outcome = execute(args.task)
        print(
            json.dumps(
                {
                    "status": outcome["status"],
                    "confidence": outcome["confidence"],
                    "provider": "local",
                }
            )
        )
    except Exception as exc:
        review.atomic_json(args.task / "error.json", {"error": str(exc)})
        raise SystemExit("Local review failed: " + str(exc))
