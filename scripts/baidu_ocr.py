"""CPU page extraction client; transcripts are source data, never approvals."""

from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import signal
import time

import local_review as local
import system_diagnostics as diagnostics
from baidu_cpu_runtime import MODEL, REVISION

OPTION = "baidu/Unlimited-OCR-cpu"


def status(url):
    try:
        with local.session(url) as client:
            response = client.get(
                local.validate_url(url) + "/health", timeout=3, allow_redirects=False
            )
            data = local.checked(response)
            if (
                data.get("model") != MODEL
                or data.get("device") != "cpu"
                or data.get("revision") != REVISION
            ):
                raise ValueError("The OCR server is not the pinned CPU runtime.")
            return {**data, "available": True}
    except Exception as exc:
        return {"available": False, "installed": False, "error": str(exc)}


def check(image, output, url, timeout, max_tokens, threads, progress_file=None):
    url = local.validate_url(url)
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    prior = diagnostics.read_json(output, {})
    if (
        prior.get("source_sha256") == sha
        and prior.get("revision") == REVISION
        and prior.get("complete")
        and prior.get("max_tokens") == max_tokens
    ):
        return prior
    active = None
    with local.session(url) as client:
        try:
            response = local.checked(
                client.post(
                    url + "/ocr",
                    json={
                        "image": base64.b64encode(image.read_bytes()).decode(),
                        "source_sha256": sha,
                        "max_tokens": max_tokens,
                        "threads": threads,
                    },
                    timeout=10,
                    allow_redirects=False,
                )
            )
            active = response["id"]
            if len(active) != 32 or not all(c in "0123456789abcdef" for c in active):
                raise ValueError("Invalid OCR task identifier.")
            deadline = time.monotonic() + timeout
            last_log = 0
            while time.monotonic() < deadline:
                data = local.checked(
                    client.get(
                        url + "/jobs/" + active, timeout=5, allow_redirects=False
                    )
                )
                diagnostics.atomic(
                    progress_file or output.with_name(output.stem + "-progress.json"),
                    {"model": MODEL, "stage": "parse", **data.get("progress", {})},
                )
                if time.monotonic() - last_log >= 5:
                    progress = data.get("progress", {})
                    print(
                        f"Baidu CPU OCR: {progress.get('status', data['status'])} · {progress.get('tokens', 0)} tokens · {progress.get('tokens_per_second', 'waiting')} tokens/s",
                        flush=True,
                    )
                    last_log = time.monotonic()
                if data["status"] == "failed":
                    raise RuntimeError(data.get("error", "Baidu CPU OCR failed"))
                if data["status"] == "completed":
                    result = data["result"]
                    if (
                        result.get("model") != MODEL
                        or result.get("revision") != REVISION
                        or result.get("device") != "cpu"
                        or result.get("source_sha256") != sha
                        or not result.get("complete")
                    ):
                        raise ValueError(
                            "Baidu OCR result is incomplete or belongs to different evidence."
                        )
                    if (
                        not isinstance(result.get("text"), str)
                        or not result["text"].strip()
                    ):
                        raise ValueError("Baidu OCR returned no text.")
                    result["max_tokens"] = max_tokens
                    diagnostics.atomic(output, result)
                    active = None
                    return result
                time.sleep(1)
            raise TimeoutError("Baidu CPU OCR timed out; its worker will be stopped.")
        finally:
            if active:
                client.delete(url + "/jobs/" + active, timeout=5, allow_redirects=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args()

    def stop(signum, frame):
        raise InterruptedError("CPU OCR stopped by user.")

    signal.signal(signal.SIGTERM, stop)
    try:
        check(
            args.image,
            args.output,
            local.validate_url(args.url),
            args.timeout,
            args.max_tokens,
            args.threads,
        )
    except Exception as exc:
        raise SystemExit("Baidu CPU OCR failed: " + str(exc))
