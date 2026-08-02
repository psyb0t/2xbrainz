"""Typed validation helpers for JSON data crossing process boundaries."""

from __future__ import annotations

import json
from typing import cast


def decode_json(value: str | bytes) -> object:
    """Decode one JSON document without allowing `Any` into application code."""
    return json.loads(value)


def require_json_object(value: object) -> dict[str, object]:
    """Validate a JSON object and require string keys for application records."""
    if not isinstance(value, dict):
        raise ValueError("JSON value must be an object")

    raw_object = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for raw_key, item in raw_object.items():
        if not isinstance(raw_key, str):
            raise ValueError("JSON object keys must be text")
        result[raw_key] = item
    return result


def require_json_array(value: object) -> list[object]:
    """Validate a JSON array before iterating untrusted external values."""
    if not isinstance(value, list):
        raise ValueError("JSON value must be an array")
    return list(cast(list[object], value))
