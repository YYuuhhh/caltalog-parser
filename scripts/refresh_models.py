#!/usr/bin/env python3
"""Refresh local account/API model metadata. No generation or Work inference."""

from __future__ import annotations
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import requests
from model_catalog import MODEL_ID, reasoning_levels
from subscription_review import atomic_json


def refresh(destination, cache, api=False):
    data = json.loads(Path(cache).read_text())
    models = [
        {
            "id": m["slug"],
            "label": m.get("display_name", m["slug"]),
            "account_verified": True,
            "reasoning_levels": reasoning_levels({"id": m["slug"], **m}),
        }
        for m in data.get("models", [])
        if MODEL_ID.fullmatch(m.get("slug", ""))
        and m.get("visibility") == "list"
        and "image" in m.get("input_modalities", [])
        and reasoning_levels({"id": m["slug"], **m})
    ]
    result = {
        "work_models": models,
        "api_models": [],
        "source": "local account model metadata",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "generation_calls": 0,
    }
    if Path(destination).is_file() and not api:
        previous = json.loads(Path(destination).read_text())
        result["api_models"] = previous.get("api_models", [])
        result["api_updated_at"] = previous.get("api_updated_at")
    if api:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            env = Path(__file__).resolve().parents[1] / ".env"
            for line in env.read_text().splitlines() if env.is_file() else []:
                if line.startswith("OPENAI_API_KEY="):
                    key = line.partition("=")[2].strip().strip("\"'")
                    break
        if not key:
            raise ValueError("OPENAI_API_KEY is required only for API model discovery.")
        response = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer " + key},
            timeout=20,
        )
        if not response.ok:
            raise ValueError(
                f"API model discovery returned HTTP {response.status_code}; cached metadata was preserved."
            )
        result["api_models"] = sorted(m["id"] for m in response.json()["data"])
        result["api_updated_at"] = result["updated_at"]
        result["source"] += " + OpenAI GET /v1/models (API availability only)"
    atomic_json(Path(destination), result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true")
    parser.add_argument(
        "--cache", default=str(Path.home() / ".codex/models_cache.json")
    )
    parser.add_argument(
        "--output",
        default=str(
            Path(__file__).resolve().parents[1] / "catalogs/library/models.json"
        ),
    )
    args = parser.parse_args()
    try:
        result = refresh(args.output, args.cache, args.api)
        print(
            json.dumps(
                {
                    "work_models": [m["id"] for m in result["work_models"]],
                    "api_model_count": len(result["api_models"]),
                    "generation_calls": 0,
                    "output": args.output,
                }
            )
        )
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
