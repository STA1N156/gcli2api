"""Keep request IDs and trace labels tied to the existing conversation identity."""

import hashlib
import json
import uuid
from collections import OrderedDict
from pathlib import Path


_model_catalogs = OrderedDict()
_session_usage = OrderedDict()
_executions = OrderedDict()
_default_model_limits = json.loads(Path(__file__).with_name("antigravity_model_limits.json").read_text())


def _put_bounded(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    if len(cache) > limit:
        cache.popitem(last=False)


def cache_model_enums(account_id, models):
    if account_id:
        _put_bounded(_model_catalogs, account_id, {
            name: {key: entry[key] for key in ("model", "maxOutputTokens") if key in entry}
            for name, entry in models.items() if isinstance(entry, dict)
        }, 8192)


def model_output_limit(model, account_id=None):
    catalog = _model_catalogs.get(account_id, {})
    value = catalog.get(model, {}).get("maxOutputTokens", _default_model_limits.get(model))
    return value if type(value) is int and value > 0 else None


def _trace_uuid(kind, identity):
    digest = hashlib.sha256(f"antigravity:{kind}:{identity}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def complete_execution(request, response):
    response = response.get("response", response)
    candidates = response.get("candidates") or []
    key = (request.get("labels") or {}).get("request_id")
    if key and not response.get("error") and candidates and candidates[0].get("finishReason"):
        if key not in _executions:
            _put_bounded(_executions, key, str(uuid.uuid4()), 32768)


class ExecutionStream:
    def __init__(self, request):
        self.request = request
        self.pending = b""

    def observe(self, chunk):
        raw = chunk if isinstance(chunk, bytes) else (chunk + "\n").encode()
        lines = (self.pending + raw).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            if line.startswith(b"data:") and b'"finishReason"' in line:
                try:
                    complete_execution(self.request, json.loads(line[5:]))
                except (ValueError, AttributeError, TypeError):
                    pass


_JSON_ORDER = {
    "": ("project", "requestId", "request", "model", "userAgent", "requestType"),
    "request": ("contents", "systemInstruction", "tools", "labels", "generationConfig", "sessionId"),
    "request.contents[]": ("role", "parts"),
    "request.systemInstruction": ("role", "parts"),
    "request.generationConfig": ("maxOutputTokens", "thinkingConfig"),
    "request.generationConfig.thinkingConfig": ("includeThoughts", "thinkingLevel", "thinkingBudget"),
    "request.tools[]": ("functionDeclarations",),
    "request.tools[].functionDeclarations[]": ("name", "description", "parameters"),
    "request.tools[].functionDeclarations[].parameters": ("type", "properties", "required"),
}


def order_request(value, path=""):
    if isinstance(value, list) and path + "[]" in _JSON_ORDER:
        return [order_request(item, path + "[]") for item in value]
    if not isinstance(value, dict) or (path not in _JSON_ORDER and path != "request.labels"):
        return value
    keys = sorted(value) if path == "request.labels" else dict.fromkeys((*_JSON_ORDER[path], *value))
    return {key: order_request(value[key], f"{path}.{key}" if path else key)
            for key in keys if key in value}


def bind_request_identity(request, model="", account_id=None):
    labels = dict(request.get("labels") or {})
    cascade = str(labels.get("cascade_id") or "").strip() or _trace_uuid("cascade", request["sessionId"])
    trajectory = str(labels.get("trajectory_id") or "").strip() or _trace_uuid("trajectory", cascade)

    # A planner reply is one model request; each tool result is a separate step.
    steps = requests = 0
    for content in request.get("contents") or []:
        responses = sum("functionResponse" in part for part in content.get("parts") or [])
        steps += max(1, responses)
        if not responses and content.get("role") in ("model", "assistant"):
            requests += 1
    try:
        step = int(labels["last_step_index"])
    except (KeyError, TypeError, ValueError):
        step = -1
    if step < 0:
        step = max(0, steps - 1)
    labels.update(cascade_id=cascade, trajectory_id=trajectory,
                  root_cascade_id=labels.get("root_cascade_id") or cascade,
                  last_step_index=str(step))
    if not str(labels.get("request_id") or "").strip():
        labels["request_id"] = f"{trajectory}-{requests}"
    previous = _executions.get(f"{trajectory}-{requests - 1}") if requests else None
    if previous and not labels.get("last_execution_id"):
        labels["last_execution_id"] = previous
    used = _session_usage.get(cascade, 0)
    if "claude" in model.lower() or labels.get("used_claude") == "true" or labels.get("used_claude_conservative") == "true":
        used |= 1
    if (model and not model.lower().startswith("gemini-")) or labels.get("used_non_gemini_model") == "true":
        used |= 2
    _put_bounded(_session_usage, cascade, used, 16384)
    labels["used_claude"] = labels["used_claude_conservative"] = str(bool(used & 1)).lower()
    labels["used_non_gemini_model"] = str(bool(used & 2)).lower()
    catalog = _model_catalogs.get(account_id)
    if catalog is not None:
        _model_catalogs.move_to_end(account_id)
        if catalog.get(model, {}).get("model"):
            labels["model_enum"] = catalog[model]["model"]
    request["labels"] = labels
    return cascade, trajectory, step + 1
