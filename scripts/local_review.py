#!/usr/bin/env python3
"""Fresh, image-backed reviews through a local Ollama server; no cloud fallback."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from jsonschema import ValidationError

import subscription_review as review
import system_diagnostics as diagnostics

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
        "model": "qwen3-vl:8b-thinking-q4_K_M",
        "final_model": "qwen3-vl:32b-thinking-q4_K_M",
        "label": "Qwen3-VL 8B / 32B Thinking · Q4 · CPU",
        "weights_gb": 20.9,
        "model_weights_gb": 6.2,
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
    endpoint = urlsplit(getattr(response, "url", "")).path or "local API"
    try:
        value = response.json()
    except ValueError as exc:
        preview = repr(getattr(response, "text", "")[:300])
        raise ValueError(
            f"{endpoint} returned HTTP {response.status_code} without valid JSON "
            f"(body: {preview}). Check the Ollama server log."
        ) from exc
    if not response.ok or response.is_redirect:
        detail = str(value.get("error", ""))[:500] if isinstance(value, dict) else ""
        raise ValueError(f"{endpoint} returned HTTP {response.status_code}: {detail}")
    if not isinstance(value, dict) or value.get("error"):
        detail = str(value.get("error", ""))[:500] if isinstance(value, dict) else ""
        raise ValueError(
            f"{endpoint} returned an invalid response or model error: {detail}"
        )
    return value


def json_answer(text):
    """Accept one JSON object, optionally wrapped in a single Markdown fence."""
    text = text.strip().removeprefix("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.S | re.I)
    if fenced:
        text = fenced[1].strip()

    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON field: {key}")
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    value = json.loads(
        text, object_pairs_hook=unique_fields, parse_constant=invalid_constant
    )
    if not isinstance(value, dict):
        raise ValueError("The review answer must be a JSON object.")
    return value


def parse_answer(response, request, model, diagnostics):
    """Separate protocol failures, final answers and the Qwen parser workaround."""
    if response.get("model") != request["model"]:
        raise ValueError(
            "Local server returned a different model than the one requested."
        )
    if response.get("done") is not True or response.get("done_reason") != "stop":
        raise ValueError(
            "Local model response was incomplete "
            f"(done_reason={response.get('done_reason')!r}). "
            "Increase output tokens or reduce RAW cells per task; a final JSON answer is required."
        )
    message = response.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError(
            "Local server did not return a text answer in message.content."
        )
    if message.get("tool_calls"):
        raise ValueError(
            "Local model returned tool calls instead of a final JSON review."
        )
    content = message["content"]
    if not content.strip():
        # Ollama 0.32.5's Qwen3-VL parser starts in thinking mode even for
        # think=false. A grammar-constrained JSON answer can land entirely in
        # message.thinking. Never extract JSON snippets from actual reasoning:
        # accept only a whole result with this task's exact schema/binding.
        # https://github.com/ollama/ollama/blob/v0.32.5/model/parsers/qwen3vl.go
        thinking = message.get("thinking")
        if (
            request["reasoning"] == "none"
            and model.get("family") == "qwen3vl"
            and isinstance(thinking, str)
            and thinking.lstrip().startswith("{")
        ):
            try:
                result = json_answer(thinking)
                review.validate_result(request, result)
            except (ValueError, TypeError, KeyError, ValidationError):
                pass
            else:
                diagnostics["answer_channel"] = "thinking_json_compatibility"
                return result
        raise ValueError(
            "Local model returned an empty final answer "
            f"({diagnostics['thinking_chars']} thinking characters, "
            f"{response.get('eval_count', 'unknown')} generated tokens). "
            "No review result was accepted. Try thinking On for a Thinking model, "
            "or use an Instruct model for thinking Off. If the output budget was exhausted, "
            "increase output tokens or reduce RAW cells per task."
        )
    try:
        result = json_answer(content)
    except ValueError as exc:
        raise ValueError(
            f"Local model returned invalid review JSON: {exc}. "
            "Return one complete JSON object matching the supplied schema. "
            "See ollama-response.json for the original response."
        ) from exc
    diagnostics["answer_channel"] = "content"
    return result


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
        "family": data.get("details", {}).get("family")
        or data.get("model_info", {}).get("general.architecture"),
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


def load_options(config):
    if config.get("use_gpu"):
        raise ValueError("Local catalog review is CPU-only.")
    options = {
        "num_ctx": config.get("context_tokens", 32768),
        "num_gpu": 0,
        "use_mmap": not config.get("load_into_ram", True),
    }
    if config.get("cpu_threads", 0):
        options["num_thread"] = config["cpu_threads"]
    return options


def stream_answer(client, url, payload, folder, timeout):
    """Retain streamed evidence and publish throughput without displaying reasoning."""
    started, first, count, last_log = time.monotonic(), None, 0, 0
    state = {
        "status": "prefill",
        "started_at": time.time(),
        "model": payload["model"],
        "stage": diagnostics.read_json(folder / "request.json", {}).get("stage"),
        "task_id": folder.name,
        "tokens_estimated": True,
        "chunks": 0,
        "tokens_per_second": None,
        "thinking_chunks": 0,
        "answer_chunks": 0,
        "thinking_chars": 0,
        "answer_chars": 0,
    }
    target = folder / "generation.json"
    diagnostics.atomic(target, state)
    content, thinking, result = [], [], {}
    print(
        f"Local model {payload['model']}: waiting for the first token (prompt/image processing).",
        flush=True,
    )
    response = None
    try:
        response = client.post(
            url + "/api/chat",
            json=payload,
            timeout=(10, timeout),
            allow_redirects=False,
            stream=True,
        )
        if (
            not response.ok
            or response.is_redirect
            or "json" not in response.headers.get("Content-Type", "")
        ):
            try:
                checked(response)
            except ValueError:
                diagnostics.atomic(
                    folder / "ollama-http.json",
                    {
                        "status": response.status_code,
                        "content_type": response.headers.get("Content-Type", ""),
                        "body": response.text[:8192],
                        "truncated": len(response.text) > 8192,
                    },
                )
                raise
        with (folder / "ollama-stream.jsonl").open("w") as saved:
            for line in response.iter_lines(chunk_size=1):
                if time.monotonic() - started > timeout:
                    raise TimeoutError(
                        "Local review response exceeded its total timeout; partial output is saved."
                    )
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    diagnostics.atomic(
                        folder / "ollama-http.json",
                        {
                            "status": response.status_code,
                            "content_type": response.headers.get("Content-Type", ""),
                            "body": line[:8192].decode(errors="replace"),
                            "truncated": len(line) > 8192,
                        },
                    )
                    raise ValueError(
                        f"/api/chat returned HTTP {response.status_code} without valid JSON (body: {line[:300]!r}). Check the Ollama server log."
                    ) from exc
                saved.write(json.dumps(event, ensure_ascii=False) + "\n")
                saved.flush()
                if not isinstance(event, dict) or event.get("error"):
                    raise ValueError(f"Ollama stream error: {str(event)[:500]}")
                message = event.get("message") or {}
                if not isinstance(message, dict):
                    raise ValueError("Ollama stream returned an invalid message.")
                for key, buffer in [("content", content), ("thinking", thinking)]:
                    value = message.get(key, "")
                    if not isinstance(value, str):
                        raise ValueError(
                            f"Ollama stream returned invalid message.{key}."
                        )
                    buffer.append(value)
                    channel = "answer" if key == "content" else "thinking"
                    state[channel + "_chunks"] += bool(value)
                    state[channel + "_chars"] += len(value)
                if message.get("content") or message.get("thinking"):
                    count += 1
                    first = first or time.monotonic()
                result.update({k: v for k, v in event.items() if k != "message"})
                result["message"] = {
                    **message,
                    "content": "".join(content),
                    "thinking": "".join(thinking),
                }
                elapsed = time.monotonic() - started
                state.update(
                    status="generating" if first else "prefill",
                    chunks=count,
                    elapsed_seconds=round(elapsed, 2),
                    first_token_seconds=round(first - started, 2) if first else None,
                    tokens_per_second=round(
                        max(0, count - 1) / max(0.001, time.monotonic() - first), 2
                    )
                    if first and count > 1
                    else None,
                )
                if event.get("done"):
                    duration = event.get("eval_duration")
                    state.update(
                        status="completed",
                        tokens=event.get("eval_count"),
                        tokens_estimated=False,
                        tokens_per_second=round(event["eval_count"] * 1e9 / duration, 3)
                        if duration and event.get("eval_count")
                        else None,
                        prompt_tokens=event.get("prompt_eval_count"),
                        prompt_tokens_per_second=round(
                            event["prompt_eval_count"]
                            * 1e9
                            / event["prompt_eval_duration"],
                            2,
                        )
                        if event.get("prompt_eval_duration")
                        and event.get("prompt_eval_count")
                        else None,
                    )
                diagnostics.atomic(target, state)
                if elapsed - last_log >= 5 or event.get("done"):
                    print(
                        f"Local review: {state['status']} · {elapsed:.0f}s · {state.get('tokens', count)} {'tokens' if not state['tokens_estimated'] else 'token chunks'} · {state['tokens_per_second']} /s",
                        flush=True,
                    )
                    last_log = elapsed
                if event.get("done"):
                    break
        if not result.get("done"):
            raise ValueError(
                "Ollama stream ended without a completed answer; partial output is saved."
            )
        diagnostics.atomic(folder / "ollama-response.json", result)
        return result
    except Exception as exc:
        state.update(
            status="failed",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        diagnostics.atomic(target, state)
        if result:
            diagnostics.atomic(folder / "ollama-response.json", result)
        raise
    finally:
        if response is not None:
            response.close()


def load_model(client, url, model, config):
    """An empty chat loads weights without generating or sharing task context."""
    timeout = config.get("load_timeout", 7200)
    if not isinstance(timeout, (int, float)) or not 10 <= timeout <= 86400:
        raise ValueError("Model load timeout must be between 10 and 86400 seconds.")
    started = time.monotonic()
    try:
        response = checked(
            client.post(
                url + "/api/chat",
                json={
                    "model": model,
                    "messages": [],
                    "stream": False,
                    "keep_alive": -1 if config.get("keep_loaded", True) else "30m",
                    "options": load_options(config),
                },
                timeout=(10, timeout),
                allow_redirects=False,
            )
        )
        if (
            response.get("model") != model
            or not response.get("done")
            or response.get("done_reason") != "load"
        ):
            raise ValueError("The local server did not confirm model loading.")
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(
            f"Loading {model} failed after {time.monotonic() - started:.1f}s: {exc}. "
            "For slow disks, increase OLLAMA_LOAD_TIMEOUT on the inference server and the model load timeout in the UI. Check available RAM and the Ollama log if loading still fails."
        ) from exc
    return time.monotonic() - started


def preload(folder):
    request = json.loads((folder / "request.json").read_text())
    model, config = request["model"], request["local"]
    state = {
        "model": model,
        "status": "loading",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "load_timeout": config.get("load_timeout", 7200),
        "load_into_ram": config.get("load_into_ram", True),
    }
    review.atomic_json(folder / "state.json", state)
    try:
        url = validate_url(config["url"])
        validate_model(model, "none")
        with session(url) as client:
            describe(client, url, model)
            elapsed = load_model(client, url, model, config)
        state.update(
            status="ready",
            elapsed_seconds=round(elapsed, 2),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        state.update(
            status="failed",
            error=str(exc)[:2000],
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        review.atomic_json(folder / "state.json", state)
        raise
    review.atomic_json(folder / "state.json", state)
    return state


def sources(request, config):
    """Review uses the task-bound snapshot; discovery is a pipeline preflight."""
    if request["stage"] not in ("internet", "mapping", "final"):
        return []
    return request.get("context", {}).get("official_sources", {}).get("evidence", [])


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
        deadline = time.monotonic() + config.get("load_timeout", 7200)
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
        if "official_sources" in page_context:
            page_context["official_sources"] = {
                k: v
                for k, v in page_context["official_sources"].items()
                if k != "evidence"
            }
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
        schema = json.loads((folder / "schema.json").read_text())
        prompt += (
            "\nReturn only one JSON object matching this response schema:\n"
            + json.dumps(schema, ensure_ascii=False)
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
            **load_options(config),
            "num_predict": output,
            "temperature": config["temperature"],
        }
        if config.get("use_gpu"):
            raise ValueError("Local catalog review is CPU-only.")
        payload = {
            "model": request["model"],
            "stream": True,
            "keep_alive": -1 if config.get("keep_loaded", True) else 0,
            "format": schema,
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
        execution["load_seconds"] = load_model(client, url, request["model"], config)
        execution["load_options"] = load_options(config)
        execution["load_timeout_seconds"] = config.get("load_timeout", 7200)
        review.atomic_json(folder / "execution.json", execution)
        # Cold HDD loading has its own budget; each fresh review gets its full
        # response timeout after the weights are ready.
        deadline = time.monotonic() + request.get("timeout", 1800)
        response = stream_answer(
            client, url, payload, folder, max(1, deadline - time.monotonic())
        )
        review.atomic_json(folder / "ollama-response.json", response)
        message = response.get("message")
        message = message if isinstance(message, dict) else {}
        diagnostics = {
            "model": response.get("model"),
            "reasoning": request["reasoning"],
            "done": response.get("done"),
            "done_reason": response.get("done_reason"),
            "content_chars": len(message.get("content", ""))
            if isinstance(message.get("content"), str)
            else 0,
            "thinking_chars": len(message.get("thinking", ""))
            if isinstance(message.get("thinking"), str)
            else 0,
            "output_tokens": output,
        }
        execution["usage"] = {
            k: response.get(k)
            for k in (
                "prompt_eval_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
                "total_duration",
                "load_duration",
            )
        }
        diagnostics.update(execution["usage"])
        review.atomic_json(folder / "execution.json", execution)
        try:
            result = parse_answer(response, request, model, diagnostics)
        finally:
            review.atomic_json(folder / "response-diagnostics.json", diagnostics)
        review.atomic_json(folder / "response.json", result)
        enforce_sources(request, result, evidence)
        enforce_rationale(request, result)
        enforce_structured_tools(request, result)
        review.validate_result(request, result)
        execution["answer_channel"] = diagnostics["answer_channel"]
        review.atomic_json(folder / "execution.json", execution)
        review.atomic_json(folder / "result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task", type=Path)
    mode.add_argument("--preload", type=Path)
    args = parser.parse_args()
    try:
        if args.preload:
            print(json.dumps(preload(args.preload)))
            raise SystemExit(0)
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
        if args.task:
            review.atomic_json(args.task / "error.json", {"error": str(exc)})
        raise SystemExit("Local review failed: " + str(exc))
