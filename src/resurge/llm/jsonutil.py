"""Structured output that works across models of very different quality.

* ``schema_hint`` renders a compact, TypeScript-like shape from a Pydantic model
  (far fewer tokens than a full JSON Schema - matters under free-tier TPM caps).
* ``extract_json`` strips reasoning traces / code fences, finds the JSON payload
  and repairs it (``json_repair``) when a small model emits almost-JSON.
* ``LLMModel`` is a forgiving base for LLM-facing schemas: ignores extra keys,
  turns ``null`` lists into ``[]``, single strings into one-item lists, and
  bare strings into ``{"text": ...}`` objects where an object was expected.
"""

from __future__ import annotations

import json
import math
import re
import types
import typing
from typing import Any, Union, get_args, get_origin

import json_repair
from pydantic import BaseModel, ConfigDict, model_validator

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


class JSONExtractionError(ValueError):
    pass


def strip_reasoning(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if "<think>" in text and "</think>" not in text:  # truncated reasoning
        text = text.split("<think>")[0]
    return text.strip()


def extract_json(text: str) -> Any:
    """Return the first JSON object/array found in ``text`` (repairing if needed)."""
    text = strip_reasoning(text)
    if not text:
        raise JSONExtractionError("empty response")
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
    for c in candidates:
        try:
            repaired = json_repair.loads(c)
            if isinstance(repaired, (dict, list)) and repaired:
                return repaired
        except Exception:  # noqa: BLE001
            continue
    raise JSONExtractionError("no parsable JSON in response")


# --------------------------------------------------------------------------- #
# Forgiving base model
# --------------------------------------------------------------------------- #
def _is_list(ann: Any) -> bool:
    origin = get_origin(ann)
    if origin in (list, typing.List):  # noqa: UP006
        return True
    if origin in (Union, types.UnionType):
        return any(_is_list(a) for a in get_args(ann) if a is not type(None))
    return False


def _list_item(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin in (list, typing.List):  # noqa: UP006
        args = get_args(ann)
        return args[0] if args else Any
    if origin in (Union, types.UnionType):
        for a in get_args(ann):
            if _is_list(a):
                return _list_item(a)
    return Any


def _is_model(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _union_args(ann: Any) -> tuple[list[Any], bool]:
    """(non-None member types, allows None) for an annotation."""
    origin = get_origin(ann)
    if origin in (Union, types.UnionType):
        args = list(get_args(ann))
        return [a for a in args if a is not type(None)], type(None) in args
    return [ann], ann is Any


def _coerce_scalar(v: Any, members: list[Any], allows_none: bool) -> tuple[bool, Any]:
    """Return (changed, value) for common small-model mistakes on scalar fields."""
    if isinstance(v, float) and not math.isfinite(v):  # NaN / Infinity would poison charts and arithmetic
        return True, None if allows_none else _DROP
    lits = [a for a in members if get_origin(a) is typing.Literal]
    if lits and isinstance(v, str):
        allowed = [x for lit in lits for x in get_args(lit)]
        low = v.strip().lower()
        for a in allowed:
            if isinstance(a, str) and a.lower() == low:
                return True, a
        for a in allowed:  # "Supported." / "partially supported" -> closest literal by prefix
            if isinstance(a, str) and (low.startswith(a.lower()) or a.lower().startswith(low[:4])):
                return True, a
        return True, _DROP
    if set(members) <= {int, float} and members and isinstance(v, str):
        m = _NUM_RE.search(v.replace(",", ""))
        if m:
            num = float(m.group(0))
            return True, int(round(num)) if members == [int] else num
        return True, None if allows_none else _DROP
    if members == [bool] and isinstance(v, str):
        return True, v.strip().lower() in {"true", "yes", "y", "1"}
    return False, v


_DROP = object()


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _forgive(cls, data: Any) -> Any:
        if isinstance(data, str) and "text" in cls.model_fields:
            return {"text": data}
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name, f in cls.model_fields.items():
            keys = [name] + ([f.alias] if f.alias else [])
            key = next((k for k in keys if k in data), None)
            if key is None:
                continue
            v = data[key]
            ann = f.annotation
            members, allows_none = _union_args(ann)
            if v is None and not allows_none and not _is_list(ann):
                if not f.is_required():
                    data.pop(key)  # let the field default apply
                elif str in members:
                    data[key] = ""
                continue
            if not _is_list(ann) and v is not None:
                changed, nv = _coerce_scalar(v, members, allows_none)
                if changed:
                    if nv is _DROP:
                        if not f.is_required():
                            data.pop(key)
                    else:
                        data[key] = nv
                    continue
            if _is_list(ann):
                if v is None:
                    data[key] = []
                elif isinstance(v, (str, dict)) and not isinstance(v, list):
                    data[key] = [v]
                item_t = _list_item(ann)
                if _is_model(item_t) and "text" in item_t.model_fields and isinstance(data[key], list):
                    data[key] = [{"text": x} if isinstance(x, str) else x for x in data[key]]
                elif item_t is str and isinstance(data[key], list):
                    data[key] = [x if isinstance(x, str) else (x.get("text") or x.get("name") or json.dumps(x))
                                 if isinstance(x, dict) else str(x) for x in data[key] if x is not None]
            elif ann is str and v is None and f.is_required():
                data[key] = ""
            elif ann is str and isinstance(v, (list, dict)):
                data[key] = "; ".join(map(str, v)) if isinstance(v, list) else json.dumps(v)
        return data


def coerce_root(schema: type[BaseModel], data: Any) -> Any:
    """Fix common shape mistakes: bare list for a one-list-field object, or a wrapper key."""
    fields = schema.model_fields
    if isinstance(data, list):
        list_fields = [n for n, f in fields.items() if _is_list(f.annotation)]
        if len(list_fields) >= 1:
            return {list_fields[0]: data}
    if isinstance(data, dict) and len(data) == 1:
        (k, v), = data.items()
        if k not in fields and isinstance(v, dict) and any(x in fields for x in v):
            return v
    return data


# --------------------------------------------------------------------------- #
# Compact schema rendering
# --------------------------------------------------------------------------- #
def schema_hint(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    notes: list[str] = []

    def render(s: dict, depth: int = 0, path: str = "") -> str:
        if "$ref" in s:
            return render(defs[s["$ref"].split("/")[-1]], depth, path)
        if "allOf" in s and len(s["allOf"]) == 1:
            return render(s["allOf"][0], depth, path)
        if "anyOf" in s:
            parts = [render(x, depth, path) for x in s["anyOf"]]
            return " | ".join(dict.fromkeys(parts))
        if "enum" in s:
            return " | ".join(json.dumps(v) for v in s["enum"])
        if "const" in s:
            return json.dumps(s["const"])
        t = s.get("type")
        if t == "object" and "properties" in s:
            if depth > 6:
                return "object"
            items = []
            for k, v in s["properties"].items():
                desc = v.get("description")
                if desc and len(notes) < 40:
                    notes.append(f"- {path + k}: {desc}")
                items.append(f'"{k}": {render(v, depth + 1, path + k + ".")}')
            return "{" + ", ".join(items) + "}"
        if t == "object":
            return "object"
        if t == "array":
            return "[" + render(s.get("items", {}), depth + 1, path) + ", ...]"
        return {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean", "null": "null"}.get(t or "", "any")

    shape = render(schema)
    out = f"Respond with ONE JSON object of this shape (types shown in place of values):\n{shape}"
    if notes:
        out += "\nField notes:\n" + "\n".join(notes)
    return out
