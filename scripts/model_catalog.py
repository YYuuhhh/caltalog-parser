"""Subscription model capabilities; discovering metadata never runs inference."""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Literal

WORK_MODELS = ["gpt-6-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]
MODEL_ID = re.compile(r"gpt-\d+(?:\.\d+)?-(?:sol|astra|luna)\Z")
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def reasoning_levels(model):
    # Ultra delegates automatically, which conflicts with isolated review tasks.
    advertised = model.get("reasoning_levels")
    if advertised is None:
        supported = model.get("supported_reasoning_levels")
        if supported is not None:
            advertised = [level["effort"] for level in supported]
    if advertised is None:
        advertised = (
            ["low", "medium", "high", "xhigh", "max"]
            if model["id"] in WORK_MODELS
            else ["high"]
        )
    return [level for level in REASONING_LEVELS if level in advertised]


def catalog(root):
    path = Path(root) / "models.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {
            "work_models": [{"id": m, "account_verified": False} for m in WORK_MODELS],
            "api_models": [],
            "updated_at": None,
            "source": "official documentation baseline",
        }
    data.pop("chat_models", None)
    for model in data["work_models"]:
        model["reasoning_levels"] = reasoning_levels(model)
    data["note"] = (
        "Models and reasoning levels come from Work/Codex account metadata. Unconfirmed choices use the official documentation baseline. Model discovery makes no generation calls."
    )
    data["docs_url"] = "https://learn.chatgpt.com/docs/models"
    return data


def work_ids(root):
    return {
        m["id"] for m in catalog(root)["work_models"] if MODEL_ID.fullmatch(m["id"])
    }


def validate_choice(root, model, reasoning):
    selected = next((m for m in catalog(root)["work_models"] if m["id"] == model), None)
    if selected is None or not MODEL_ID.fullmatch(model):
        raise ValueError("Choose a Work model from the discovered model catalog.")
    if reasoning not in selected["reasoning_levels"]:
        raise ValueError(f"Reasoning level {reasoning!r} is not supported by {model}.")
