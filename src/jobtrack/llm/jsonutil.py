"""Tolerant JSON extraction for small-model output (code fences, chatter)."""

from __future__ import annotations

import json
from typing import Any


def extract_json_value(text: str, opener: str, closer: str) -> Any:
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON found in model output")
    return json.loads(text[start : end + 1])


def extract_json_object(text: str) -> dict[str, Any]:
    value = extract_json_value(text, "{", "}")
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def extract_json_array(text: str) -> list[Any]:
    value = extract_json_value(text, "[", "]")
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return value
