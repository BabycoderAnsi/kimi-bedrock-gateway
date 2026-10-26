"""Kimi adapter and auto-mode classifier guard (gateway/kimi_gateway_hooks.py)."""

import asyncio
import copy
import json

import pytest

import kimi_gateway_hooks as hooks

CLASSIFIER = {
    "model": "claude-sonnet-5", "max_tokens": 64, "stop_sequences": ["</block>"], "thinking": {"type": "disabled"},
    "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.303"},
               {"type": "text", "text": "You are a security monitor for autonomous AI coding agents.\n\n## Context"}],
    "messages": [{"role": "user", "content": [{"type": "text", "text": "<transcript>...</transcript>"}]}],
}
CODING = {
    "model": "kimi-k3", "max_tokens": 32000, "system": "You are Claude Code",
    "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "s"},
                                          {"type": "text", "text": "hello"}]},
        {"role": "user", "content": [{"type": "text", "text": "again"}]},
    ],
}


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    monkeypatch.setenv("KGW_PRIMARY_ALIAS", "kimi-k3")
    monkeypatch.setenv("KGW_CLASSIFIER_ALIAS", "claude-sonnet-5")


def run(data):
    return asyncio.run(hooks.proxy_handler_instance.async_pre_call_hook(None, None, data, "anthropic_messages"))


def test_detects_classifier_request():
    assert hooks.is_classifier_request(CLASSIFIER)
    assert not hooks.is_classifier_request(CODING)


def test_structural_detection_without_marker_needs_stop_and_no_tools():
    data = copy.deepcopy(CLASSIFIER)
    data["system"] = "renamed prompt"
    assert hooks.is_classifier_request(data)  # </block> stop + no tools
    data["proxy_server_request"] = {"headers": {"x-claude-code-request-class": "main"}}
    assert not hooks.is_classifier_request(data)  # main-loop hint header wins
    data.pop("proxy_server_request")
    data["tools"] = [{"name": "x"}]
    assert not hooks.is_classifier_request(data)


def test_classifier_request_to_kimi_is_refused():
    data = copy.deepcopy(CLASSIFIER)
    data["model"] = "kimi-k3"
    with pytest.raises(hooks.HTTPException) as exc:
        run(data)
    assert exc.value.status_code == 403
    assert "kgw_classifier_refused" in exc.value.detail


def test_classifier_route_accepts_classifier_and_refuses_coding():
    assert run(copy.deepcopy(CLASSIFIER))["model"] == "claude-sonnet-5"
    data = copy.deepcopy(CODING)
    data["model"] = "claude-sonnet-5"
    with pytest.raises(hooks.HTTPException) as exc:
        run(data)
    assert "kgw_classifier_only" in exc.value.detail


def test_classifier_route_absent_means_no_restriction(monkeypatch):
    monkeypatch.delenv("KGW_CLASSIFIER_ALIAS")
    data = copy.deepcopy(CODING)
    data["model"] = "some-other-model"
    assert run(data)["model"] == "some-other-model"


def test_prior_reasoning_is_stripped_for_kimi_only():
    out = run(copy.deepcopy(CODING))
    assert [b["type"] for b in out["messages"][1]["content"]] == ["text"]
    other = copy.deepcopy(CODING)
    other["model"] = "another-model"
    assert run(other)["messages"][1]["content"][0]["type"] == "thinking"


def test_reasoning_only_turn_gets_placeholder_and_tool_use_is_kept():
    data = copy.deepcopy(CODING)
    data["messages"][1]["content"] = [{"type": "redacted_thinking", "data": "x"}]
    data["messages"].insert(2, {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]})
    out = run(data)
    assert out["messages"][1]["content"] == [{"type": "text", "text": hooks.REASONING_PLACEHOLDER}]
    assert [b["type"] for b in out["messages"][2]["content"]] == ["tool_use"]


def test_openai_format_reasoning_fields_removed():
    data = {"model": "kimi-k3", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "reasoning_content": "secret thoughts", "thinking_blocks": [{}]},
        {"role": "user", "content": "again"}]}
    out = run(data)
    assert "reasoning_content" not in out["messages"][1] and "thinking_blocks" not in out["messages"][1]


LOOKAROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": "^(?!rm\\b).+$", "minLength": 1, "description": "keep me"},
        "tag": {"type": "string", "pattern": "^[a-z]+$"},
        "after": {"type": "string", "pattern": "(?<=x)y"},
    },
    "required": ["name"],
}


def test_only_lookaround_patterns_are_dropped_from_tool_schemas():
    data = copy.deepcopy(CODING)
    data["tools"].append({"name": "mcp__demo", "input_schema": copy.deepcopy(LOOKAROUND_SCHEMA)})
    out = run(data)
    props = out["tools"][1]["input_schema"]["properties"]
    assert "pattern" not in props["name"] and "pattern" not in props["after"]
    assert props["name"]["minLength"] == 1 and props["name"]["description"] == "keep me"
    assert props["tag"]["pattern"] == "^[a-z]+$"  # supported regex untouched
    assert out["tools"][1]["input_schema"]["required"] == ["name"]
    assert out["tools"][0] == CODING["tools"][0]


def test_structured_output_schemas_sanitized():
    data = copy.deepcopy(CODING)
    data["output_config"] = {"effort": "high", "format": {"type": "json_schema",
                                                          "schema": copy.deepcopy(LOOKAROUND_SCHEMA)}}
    data["response_format"] = {"type": "json_schema", "json_schema": {"name": "x",
                                                                      "schema": copy.deepcopy(LOOKAROUND_SCHEMA)}}
    out = run(data)
    assert not hooks.schema_has_lookaround(out["output_config"]["format"]["schema"])
    assert not hooks.schema_has_lookaround(out["response_format"]["json_schema"]["schema"])
    assert out["output_config"]["effort"] == "low"  # gateway default overrides Claude Code's "high"


def test_schemas_without_lookaround_are_untouched():
    data = copy.deepcopy(CODING)
    before = copy.deepcopy(data["tools"])
    assert run(data)["tools"] == before


def test_local_fallback_matches_upstream_semantics():
    schema = {"type": "object", "additionalProperties": False,
              "patternProperties": {"^(?!_).*$": {"type": "string"}, "^x_": {"type": "integer"}},
              "properties": {"a": {"type": "string", "pattern": "(?=a)a"}}}
    out = hooks._local_drop_lookaround(schema)
    assert list(out["patternProperties"]) == ["^x_"]
    assert out["additionalProperties"] == {"type": "string"}  # names the dropped pattern allowed stay allowed
    assert "pattern" not in out["properties"]["a"]
    assert not hooks.schema_has_lookaround(out)
    if hooks._upstream_drop_lookaround is not None:
        upstream = hooks.drop_lookaround(schema)
        assert not hooks.schema_has_lookaround(upstream)


def test_hook_never_logs_content(caplog):
    data = copy.deepcopy(CODING)
    data["tools"].append({"name": "mcp__demo", "input_schema": copy.deepcopy(LOOKAROUND_SCHEMA)})
    with caplog.at_level("DEBUG", logger="kimi_gateway_hooks"):
        run(data)
    text = caplog.text
    assert "mcp__demo" in text and "(?!" not in text and "hello" not in text


# ------------------------------------------------------------ reasoning effort

def test_default_effort_low_rewrites_claude_code_thinking():
    data = copy.deepcopy(CODING)
    data["thinking"] = {"type": "adaptive", "display": "omitted"}
    data["output_config"] = {"effort": "high"}
    out = run(data)
    assert out["thinking"] == {"type": "adaptive"}  # display dropped: it would make reasoning_effort a dict
    assert out["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("effort", ["none", "medium", "max"])
def test_configured_effort(monkeypatch, effort):
    monkeypatch.setenv("KGW_KIMI_EFFORT", effort)
    data = copy.deepcopy(CODING)
    data["thinking"] = {"type": "adaptive"}
    data["output_config"] = {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    out = run(data)
    if effort == "none":
        assert out["thinking"] == {"type": "disabled"} and "effort" not in out["output_config"]
    else:
        assert out["output_config"]["effort"] == effort
    assert out["output_config"]["format"]["type"] == "json_schema"  # other output_config keys kept


def test_client_effort_passthrough(monkeypatch):
    monkeypatch.setenv("KGW_KIMI_EFFORT", "client")
    data = copy.deepcopy(CODING)
    data["thinking"] = {"type": "adaptive", "display": "omitted"}
    data["output_config"] = {"effort": "xhigh"}
    out = run(data)
    assert out["output_config"]["effort"] == "xhigh" and out["thinking"] == {"type": "adaptive"}


def test_openai_format_gets_reasoning_effort():
    data = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}
    assert run(data)["reasoning_effort"] == "low"


def test_effort_not_applied_to_classifier_route():
    out = run(copy.deepcopy(CLASSIFIER))
    assert out["thinking"] == {"type": "disabled"} and "output_config" not in out


def test_chat_completions_tools_flag_only_for_configured_model(monkeypatch):
    litellm = pytest.importorskip("litellm")
    monkeypatch.setenv("KGW_KIMI_API", "chat_completions")
    entry = dict(litellm.model_cost["in.moonshotai.kimi-k3"])
    try:
        assert hooks.enable_chat_completions_tools("in.moonshotai.kimi-k3")
        assert litellm.model_cost["in.moonshotai.kimi-k3"][hooks._TOOLS_WITH_REASONING_FLAG] is True
        assert not litellm.model_cost["us.moonshotai.kimi-k3"].get(hooks._TOOLS_WITH_REASONING_FLAG)
        from litellm.llms.bedrock.common_utils import bedrock_request_needs_converse
        assert not bedrock_request_needs_converse("chat_completions/in.moonshotai.kimi-k3",
                                                  {"tools": [{"type": "function"}], "reasoning_effort": "low"})
        monkeypatch.setenv("KGW_KIMI_API", "converse")
        assert not hooks.enable_chat_completions_tools("in.moonshotai.kimi-k3")
    finally:
        litellm.model_cost["in.moonshotai.kimi-k3"] = entry


# ------------------------------------------------------------ prompt caching

def _openai_messages():
    return [{"role": "system", "content": [{"type": "text", "text": "billing"}, {"type": "text", "text": "SYSTEM"}]},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "ctx"}, {"type": "text", "text": "latest"}]}]


def test_breakpoints_on_system_end_and_latest_user():
    msgs = _openai_messages()
    assert hooks.add_prompt_cache_breakpoints(msgs) == 2
    assert msgs[0]["content"][-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in msgs[0]["content"][0]
    assert msgs[3]["content"][-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert msgs[1]["content"] == "first"  # earlier turns untouched


def test_string_system_content_becomes_marked_part():
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    hooks.add_prompt_cache_breakpoints(msgs)
    assert msgs[0]["content"] == [{"type": "text", "text": "SYS", "prompt_cache_breakpoint": {"mode": "explicit"}}]
    assert msgs[1]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}


def test_tool_result_turn_marks_only_system():
    msgs = [{"role": "system", "content": "SYS"}, {"role": "tool", "tool_call_id": "1", "content": "out"}]
    assert hooks.add_prompt_cache_breakpoints(msgs) == 1


def test_deployment_hook_only_for_kimi_chat_completions(monkeypatch):
    monkeypatch.setenv("KGW_KIMI_MODEL_ID", "in.moonshotai.kimi-k3")
    monkeypatch.setenv("KGW_KIMI_API", "chat_completions")
    call = lambda kw: asyncio.run(hooks.proxy_handler_instance.async_pre_call_deployment_hook(kw, "acompletion"))
    kw = {"model": "bedrock/chat_completions/in.moonshotai.kimi-k3", "messages": _openai_messages()}
    assert call(kw)["messages"][0]["content"][-1].get("prompt_cache_breakpoint")
    other = {"model": "bedrock/in.anthropic.claude-sonnet-5", "messages": _openai_messages()}
    assert call(other) is None and "prompt_cache_breakpoint" not in json.dumps(other)
    monkeypatch.setenv("KGW_KIMI_PROMPT_CACHE", "0")
    off = {"model": "bedrock/chat_completions/in.moonshotai.kimi-k3", "messages": _openai_messages()}
    assert call(off) is None


def test_stable_cache_key_ignores_session_and_follows_prefix(monkeypatch):
    monkeypatch.setenv("KGW_KIMI_MODEL_ID", "in.moonshotai.kimi-k3")
    monkeypatch.setenv("KGW_KIMI_API", "chat_completions")
    call = lambda kw: asyncio.run(hooks.proxy_handler_instance.async_pre_call_deployment_hook(kw, "acompletion"))
    base = {"model": "bedrock/chat_completions/in.moonshotai.kimi-k3", "tools": [{"type": "function"}]}
    a = call({**base, "prompt_cache_key": "session-A", "messages": _openai_messages()})
    b = call({**base, "prompt_cache_key": "session-B", "messages": _openai_messages()[:2]})
    assert a["prompt_cache_key"] == b["prompt_cache_key"] and a["prompt_cache_key"].startswith("kgw-")
    assert len(a["prompt_cache_key"]) <= 64
    changed = _openai_messages()
    changed[0]["content"][1]["text"] = "OTHER SYSTEM"
    c = call({**base, "messages": changed})
    assert c["prompt_cache_key"] != a["prompt_cache_key"]


def test_kimi_self_review_opt_in_adapts_classifier_route_only(monkeypatch):
    monkeypatch.setenv("KGW_CLASSIFIER_BACKEND", "kimi")
    out = run(copy.deepcopy(CLASSIFIER))
    assert out["model"] == "claude-sonnet-5" and "stop_sequences" not in out  # Kimi rejects stopSequences
    data = copy.deepcopy(CODING)
    data["model"] = "claude-sonnet-5"
    with pytest.raises(hooks.HTTPException) as exc:  # still classifier-only
        run(data)
    assert "kgw_classifier_only" in exc.value.detail
    data = copy.deepcopy(CLASSIFIER)
    data["model"] = "kimi-k3"
    with pytest.raises(hooks.HTTPException):  # the coding route still refuses classifier calls
        run(data)


def test_claude_classifier_route_keeps_stop_sequences(monkeypatch):
    monkeypatch.delenv("KGW_CLASSIFIER_BACKEND", raising=False)
    assert run(copy.deepcopy(CLASSIFIER))["stop_sequences"] == ["</block>"]
