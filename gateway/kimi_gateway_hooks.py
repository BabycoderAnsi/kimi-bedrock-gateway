"""LiteLLM proxy pre-call hooks for the Kimi Bedrock Gateway.

Loaded through ``litellm_settings.callbacks`` in the rendered config. The hook
does three narrowly-scoped things, each covered by tests/test_hooks.py:

1. Kimi K3 request adaptation (primary route only)
   * Removes ``thinking`` / ``redacted_thinking`` blocks (Anthropic format) and
     ``reasoning_content`` / ``thinking_blocks`` (OpenAI format) from earlier
     assistant turns. AWS documents that Kimi K3 on Converse fails with
     InternalServerException when prior-turn reasoning is replayed and that the
     remedy is to remove it. New responses still contain reasoning.
   * Drops regex ``pattern`` keywords that use look-around assertions from tool
     input schemas and structured-output schemas. Kimi K3 on Bedrock compiles these
     schemas for constrained decoding and rejects look-around ("structured output
     schema uses unsupported regex negative look-ahead"). Only the offending
     ``pattern`` keywords are removed (via LiteLLM's own helper when available);
     every other constraint is kept, and Claude Code / the MCP server still
     validate tool inputs before execution. LiteLLM >= 1.106.0 already does this
     for tool schemas on Converse; repeating it here also covers structured
     outputs, which LiteLLM forwards unchanged.

   * Enforces the configured reasoning effort (``KGW_KIMI_EFFORT``, default ``low``).
     Kimi K3 reasons at ``max`` effort unless told otherwise, and Claude Code asks for
     ``high``; a "hello" took 20-40 s. The hook rewrites Claude Code's ``thinking`` /
     ``output_config.effort`` so LiteLLM's Anthropic adapter emits ``reasoning_effort``,
     which Bedrock's Chat Completions API honours (Converse ignores it for Kimi K3).

   * Adds Bedrock explicit prompt-cache breakpoints (``KGW_KIMI_PROMPT_CACHE``) on the
     translated Chat Completions request: one after the system prompt (which follows the
     ~15K-token tool definitions) and one on the latest user message. Claude Code's own
     ``cache_control`` markers are dropped by LiteLLM for non-Claude models, and Bedrock
     ignores ``cache_control`` for Kimi K3; ``prompt_cache_breakpoint`` is the marker
     Bedrock honours (verified live: the second request read 14,585 cached tokens).

2. Auto-mode classifier guard
   Claude Code sends auto-mode classifier calls to ANTHROPIC_BASE_URL with model
   ``claude-sonnet-5`` and, when that fails, retries them on the session model.
   Kimi K3 is not a supported auto-mode classifier, so classifier-shaped requests
   addressed to the Kimi route are rejected (Claude Code then blocks the action:
   fail closed).

3. Classifier route restriction
   When a Claude classifier route is configured, it only accepts
   classifier-shaped requests, so ordinary coding traffic can't silently move off
   Kimi K3 onto Claude.

The hook never logs request content or credentials.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

try:  # pragma: no cover - exercised implicitly when running inside the proxy
    from fastapi import HTTPException
except ImportError:  # unit tests without fastapi

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: Any = None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

try:  # pragma: no cover
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:

    class CustomLogger:  # type: ignore[no-redef]
        pass

try:
    from litellm.litellm_core_utils.prompt_templates.common_utils import (
        drop_lookaround_regex_patterns as _upstream_drop_lookaround,
    )
except ImportError:  # older LiteLLM: use the local implementation below
    _upstream_drop_lookaround = None

log = logging.getLogger("kimi_gateway_hooks")

# Markers of Claude Code's auto-mode classifier request (verified against 2.1.303):
# a dedicated system prompt, no tools, and an XML verdict that stops at </block>.
CLASSIFIER_SYSTEM_MARKER = "You are a security monitor for autonomous AI coding agents"
CLASSIFIER_STOP_SEQUENCE = "</block>"
_LOOKAROUND = re.compile(r"\(\?<?[=!]")
_REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})
_OPENAI_REASONING_KEYS = ("reasoning_content", "thinking_blocks", "reasoning")
REASONING_PLACEHOLDER = "[earlier reasoning omitted by gateway]"
KIMI_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_TOOLS_WITH_REASONING_FLAG = "supports_bedrock_runtime_chat_completions_tools_with_reasoning"


def enable_chat_completions_tools(model_id: str | None = None) -> bool:
    """Let LiteLLM keep tool-calling Kimi requests on Bedrock Chat Completions.

    LiteLLM 1.106.0 only keeps a request with tools on Chat Completions when the model's
    price-map entry has ``supports_bedrock_runtime_chat_completions_tools_with_reasoning``;
    otherwise it silently falls back to Converse (where reasoning_effort is ignored). Kimi K3's
    entry lacks the flag, but AWS serves Kimi K3 tool calls with reasoning on Chat Completions
    (verified live on in.moonshotai.kimi-k3, 2026-10-19). Only the configured model is changed.
    """
    model_id = model_id or os.environ.get("KGW_KIMI_MODEL_ID")
    if not model_id or os.environ.get("KGW_KIMI_API", "chat_completions") != "chat_completions":
        return False
    try:
        import litellm  # noqa: PLC0415
    except ImportError:
        return False
    entry = litellm.model_cost.get(model_id)
    if entry is None:
        litellm.model_cost[model_id] = entry = {"litellm_provider": "bedrock_converse", "mode": "chat",
                                                "supports_function_calling": True, "supports_reasoning": True}
    entry[_TOOLS_WITH_REASONING_FLAG] = True
    return True


def _primary_alias() -> str:
    return os.environ.get("KGW_PRIMARY_ALIAS", "kimi-k3")


def _classifier_alias() -> str | None:
    return os.environ.get("KGW_CLASSIFIER_ALIAS") or None


def _classifier_on_kimi() -> bool:
    """The unsafe opt-in (classifier.backend = "kimi"): Kimi K3 reviews its own auto-mode actions."""
    return os.environ.get("KGW_CLASSIFIER_BACKEND") == "kimi"


# ------------------------------------------------------------------ detection


def _system_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    system = data.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(b.get("text", "") for b in system if isinstance(b, dict))
    for msg in data.get("messages") or []:  # OpenAI format keeps system in messages
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n".join(parts)


def _request_headers(data: dict[str, Any]) -> dict[str, str]:
    for container in (data.get("proxy_server_request"), data.get("metadata"), data.get("litellm_metadata")):
        if isinstance(container, dict) and isinstance(container.get("headers"), dict):
            return {str(k).lower(): str(v) for k, v in container["headers"].items()}
    return {}


def is_classifier_request(data: dict[str, Any]) -> bool:
    """Heuristic match for a Claude Code auto-mode classifier call."""
    if data.get("tools"):
        return False
    if CLASSIFIER_SYSTEM_MARKER in _system_text(data):
        return True
    stops = data.get("stop_sequences") or data.get("stop") or []
    if isinstance(stops, str):
        stops = [stops]
    request_class = _request_headers(data).get("x-claude-code-request-class")
    return CLASSIFIER_STOP_SEQUENCE in stops and request_class in (None, "auxiliary")


# --------------------------------------------------------------- adaptation


def _local_drop_lookaround(schema: Any) -> Any:
    """Fallback mirroring LiteLLM's ``drop_lookaround_regex_patterns`` semantics."""
    if isinstance(schema, list):
        return [_local_drop_lookaround(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    dropped_value_schemas: list[Any] = []
    for key, value in schema.items():
        if key == "pattern" and isinstance(value, str) and _LOOKAROUND.search(value):
            continue
        if key == "patternProperties" and isinstance(value, dict):
            kept = {k: _local_drop_lookaround(v) for k, v in value.items() if not _LOOKAROUND.search(k)}
            dropped_value_schemas.extend(v for k, v in value.items() if _LOOKAROUND.search(k))
            if kept:
                out[key] = kept
            continue
        out[key] = _local_drop_lookaround(value)
    if dropped_value_schemas and out.get("additionalProperties") is False:
        # A closed object keeps accepting the names the dropped pattern allowed.
        out["additionalProperties"] = _local_drop_lookaround(dropped_value_schemas[0])
    return out


def drop_lookaround(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    if _upstream_drop_lookaround is not None:
        return copy.deepcopy(dict(_upstream_drop_lookaround(schema)))
    return _local_drop_lookaround(schema)


def schema_has_lookaround(schema: Any) -> bool:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "pattern" and isinstance(value, str) and _LOOKAROUND.search(value):
                return True
            if key == "patternProperties" and isinstance(value, dict) and any(_LOOKAROUND.search(k) for k in value):
                return True
            if schema_has_lookaround(value):
                return True
    elif isinstance(schema, list):
        return any(schema_has_lookaround(v) for v in schema)
    return False


def _strip_reasoning(messages: list[Any]) -> int:
    removed = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for key in _OPENAI_REASONING_KEYS:
            if key in msg:
                del msg[key]
                removed += 1
        content = msg.get("content")
        if isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in _REASONING_BLOCK_TYPES)]
            if len(kept) != len(content):
                removed += len(content) - len(kept)
                msg["content"] = kept or [{"type": "text", "text": REASONING_PLACEHOLDER}]
    return removed


def _strip_cache_control(data: dict[str, Any]) -> int:
    """Remove Anthropic ``cache_control`` markers from system, messages and tools.

    On Converse LiteLLM turns them into ``cachePoint`` blocks unless it recognises the model as
    non-Claude. Behind an application-inference-profile ARN it cannot, and Kimi K3 rejects
    cachePoint with a 400 (verified live, 2026-10-26).
    """
    removed = 0
    blocks: list[Any] = list(data.get("tools") or [])
    if isinstance(data.get("system"), list):
        blocks += data["system"]
    for msg in data.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            blocks += msg["content"]
    for block in blocks:
        if isinstance(block, dict) and block.pop("cache_control", None) is not None:
            removed += 1
    return removed


def _sanitize_schemas(data: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for tool in data.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        holder, key = (tool, "input_schema") if "input_schema" in tool else (tool.get("function") or {}, "parameters")
        schema = holder.get(key) if isinstance(holder, dict) else None
        if schema_has_lookaround(schema):
            holder[key] = drop_lookaround(schema)
            changed.append(f"tool:{tool.get('name') or (tool.get('function') or {}).get('name')}")
    fmt = (data.get("output_config") or {}).get("format") if isinstance(data.get("output_config"), dict) else None
    if isinstance(fmt, dict) and schema_has_lookaround(fmt.get("schema")):
        fmt["schema"] = drop_lookaround(fmt["schema"])
        changed.append("output_config.format")
    rf = data.get("response_format")
    if isinstance(rf, dict) and isinstance(rf.get("json_schema"), dict) and \
            schema_has_lookaround(rf["json_schema"].get("schema")):
        rf["json_schema"]["schema"] = drop_lookaround(rf["json_schema"]["schema"])
        changed.append("response_format")
    return changed


def apply_reasoning_effort(data: dict[str, Any], effort: str | None = None) -> str | None:
    """Rewrite the request so LiteLLM sends ``reasoning_effort=<effort>`` to Kimi.

    Anthropic format: ``thinking: adaptive`` + ``output_config.effort`` (LiteLLM's adapter turns
    that into ``reasoning_effort`` for non-Claude targets; ``thinking.display`` is dropped because
    it would turn reasoning_effort into a dict that Bedrock rejects). ``none`` becomes
    ``thinking: disabled``. OpenAI format: ``reasoning_effort`` directly. ``client`` keeps the
    caller's effort and only normalises the thinking block.
    """
    effort = effort or os.environ.get("KGW_KIMI_EFFORT", "low")
    is_anthropic = "system" in data or "thinking" in data or "output_config" in data or any(
        isinstance(m, dict) and isinstance(m.get("content"), list) for m in data.get("messages") or [])
    if effort == "client":
        thinking = data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
            data["thinking"] = {"type": "adaptive"}
        oc = data.get("output_config")
        return oc.get("effort") if isinstance(oc, dict) else data.get("reasoning_effort")
    if effort not in KIMI_EFFORTS:
        return None
    if is_anthropic and "reasoning_effort" not in data:
        output_config = {k: v for k, v in (data.get("output_config") or {}).items() if k != "effort"}
        if effort == "none":
            data["thinking"] = {"type": "disabled"}
        else:
            data["thinking"] = {"type": "adaptive"}
            output_config["effort"] = effort
        if output_config:
            data["output_config"] = output_config
        else:
            data.pop("output_config", None)
    else:
        data.pop("thinking", None)
        data["reasoning_effort"] = effort
    return effort


def adapt_kimi_request(data: dict[str, Any]) -> dict[str, Any]:
    # Kimi K3 rejects stop sequences ("This model doesn't support the stopSequences field", verified
    # live on Converse 2026-10-26); Claude Code's classifier sends ["</block>"] and parses the verdict anyway.
    data.pop("stop_sequences", None)
    data.pop("stop", None)
    apply_reasoning_effort(data)
    messages = data.get("messages")
    if isinstance(messages, list):
        removed = _strip_reasoning(messages)
        if removed:
            log.debug("kimi adapter: removed %d prior reasoning block(s)", removed)
    if os.environ.get("KGW_KIMI_API", "chat_completions") == "converse":
        _strip_cache_control(data)
    changed = _sanitize_schemas(data)
    if changed:
        # Names only: never the schema contents.
        log.warning("kimi adapter: dropped look-around regex patterns from %s", ", ".join(changed))
    return data


# ------------------------------------------------------------- prompt caching

CACHE_BREAKPOINT = {"mode": "explicit"}


def _mark_last_text_part(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        message["content"] = [{"type": "text", "text": content, "prompt_cache_breakpoint": dict(CACHE_BREAKPOINT)}]
        return True
    if isinstance(content, list):
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                part["prompt_cache_breakpoint"] = dict(CACHE_BREAKPOINT)
                part.pop("cache_control", None)
                return True
    return False


def add_prompt_cache_breakpoints(messages: list[Any]) -> int:
    """Mark the end of the system prompt and the latest user message (OpenAI message format).

    Bedrock allows up to four cache writes per request; this uses at most two.
    """
    marked = 0
    systems = [m for m in messages if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    if systems and _mark_last_text_part(systems[-1]):
        marked += 1
    last = messages[-1] if messages else None
    if isinstance(last, dict) and last.get("role") == "user" and (not systems or last is not systems[-1]) \
            and _mark_last_text_part(last):
        marked += 1
    return marked


def stable_prompt_cache_key(kwargs: dict[str, Any]) -> str:
    """A cache-routing key shared by every request with the same model, tools and system prompt.

    LiteLLM sets ``prompt_cache_key`` to Claude Code's per-session id, so Bedrock never shared the
    ~15K-token tools+system cache between sessions (verified live: identical prefixes, 0 cached
    tokens across sessions; 16,773 cached when the key matched). A content hash keeps identical
    prefixes together while different prompts still get different keys. It reveals nothing beyond
    what Bedrock already receives.
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    systems = [m.get("content") for m in kwargs.get("messages") or []
               if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    material = json.dumps([str(kwargs.get("model")), kwargs.get("tools") or [], systems], sort_keys=True, default=str)
    return "kgw-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _is_kimi_chat_completions_call(kwargs: dict[str, Any]) -> bool:
    model = str(kwargs.get("model") or "")
    kimi_id = os.environ.get("KGW_KIMI_MODEL_ID") or "moonshotai.kimi"
    return (os.environ.get("KGW_KIMI_API", "chat_completions") == "chat_completions"
            and kimi_id in model and "chat_completions" in model)


# --------------------------------------------------------------------- hook


def _timing_line(kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any, ok: bool) -> str:
    """One metadata-only line per upstream call: never prompt, response or key material."""
    params = kwargs.get("litellm_params") or {}
    api_base = str(params.get("api_base") or "")
    api = "chat-completions" if "chat/completions" in api_base else (
        "converse" if "converse" in api_base else ("invoke" if "invoke" in api_base else api_base.rsplit("/", 1)[-1]))
    try:
        seconds = (end_time - start_time).total_seconds()
    except Exception:  # noqa: BLE001
        seconds = float("nan")
    usage = getattr(response_obj, "usage", None) or (response_obj or {}).get("usage", {}) if ok else None
    get = (lambda k: getattr(usage, k, None) if not isinstance(usage, dict) else usage.get(k)) if usage else (lambda k: None)
    details = get("prompt_tokens_details")
    cached = getattr(details, "cached_tokens", None) if details is not None and not isinstance(details, dict) else (
        (details or {}).get("cached_tokens") if isinstance(details, dict) else None)
    effort = (kwargs.get("optional_params") or {}).get("reasoning_effort")
    return (f"kgw timing: model={kwargs.get('model')} api={api} effort={effort} status={'ok' if ok else 'error'} "
            f"seconds={seconds:.1f} prompt_tokens={get('prompt_tokens')} cached_tokens={cached} "
            f"completion_tokens={get('completion_tokens')}")


class KimiGatewayHooks(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        """Runs on LiteLLM's inner OpenAI-format call, after the Anthropic adapter."""
        if os.environ.get("KGW_KIMI_PROMPT_CACHE", "1") != "1" or not isinstance(kwargs, dict):
            return None
        messages = kwargs.get("messages")
        if not isinstance(messages, list) or not _is_kimi_chat_completions_call(kwargs):
            return None
        kwargs["prompt_cache_key"] = stable_prompt_cache_key(kwargs)  # before markers: hash the bare prefix
        add_prompt_cache_breakpoints(messages)
        return kwargs

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log.warning(_timing_line(kwargs, response_obj, start_time, end_time, True))
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log.warning(_timing_line(kwargs, None, start_time, end_time, False))
        except Exception:  # noqa: BLE001
            pass

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: D401
        if not isinstance(data, dict):
            return data
        model = data.get("model")
        primary = _primary_alias()
        classifier = _classifier_alias()
        if model == primary:
            if is_classifier_request(data):
                log.warning("refused an auto-mode classifier request addressed to %s", primary)
                raise HTTPException(
                    status_code=403,
                    detail=(f"kgw_classifier_refused: '{primary}' (Kimi K3) is not a supported Claude Code "
                            "auto-mode classifier. Configure a Claude classifier route "
                            "(manage.py classifier enable) or use a non-auto permission mode."),
                )
            return adapt_kimi_request(data)
        if classifier and model == classifier:
            if not is_classifier_request(data):
                raise HTTPException(
                    status_code=403,
                    detail=(f"kgw_classifier_only: the '{classifier}' route serves Claude Code auto-mode classifier "
                            f"requests only. Use '{primary}' for coding requests."),
                )
            if _classifier_on_kimi():
                log.warning("auto-mode classifier request served by Kimi K3 (unsafe opt-in: Kimi reviews its "
                            "own actions)")
                return adapt_kimi_request(data)
        return data


proxy_handler_instance = KimiGatewayHooks()
enable_chat_completions_tools()
