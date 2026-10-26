"""Claude Code launcher environment and auto-mode policy."""

import json

import pytest

import common
import manage


def settings(backend="none"):
    s = common.default_settings()
    s["classifier"]["backend"] = backend
    return s


def test_environment_is_process_scoped_and_unsets_conflicts():
    base = {"ANTHROPIC_API_KEY": "sk-ant-real", "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "abc",
            "LITELLM_MASTER_KEY": "x", "PATH": "/usr/bin", "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5"}
    env = manage.claude_environment(settings(), "sk-kgw-master", base)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-kgw-master"
    assert env["ANTHROPIC_MODEL"] == "kimi-k3"
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK", "LITELLM_MASTER_KEY"):
        assert k not in env
    assert env["PATH"] == "/usr/bin"
    assert base["ANTHROPIC_API_KEY"] == "sk-ant-real"  # caller's environment untouched


def test_sonnet_slot_points_at_classifier_route_never_kimi():
    env = manage.claude_environment(settings("bedrock"), "k", {})
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "kimi-k3"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "kimi-k3"
    assert env["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
    assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"  # stable prompt prefix for Bedrock caching


def test_refuses_inherited_sonnet_mapping_to_kimi():
    with pytest.raises(common.GatewayError) as exc:
        manage.claude_environment(settings(), "k", {"ANTHROPIC_DEFAULT_SONNET_MODEL": "kimi-k3"})
    assert exc.value.code == "classifier_conflict"


def test_auto_mode_disabled_without_ready_classifier():
    args = manage.claude_arguments(settings(), classifier_ready=False, user_args=["-p", "hi"])
    assert args[:2] == ["--settings", json.dumps({"disableAutoMode": "disable"})]
    assert args[-2:] == ["-p", "hi"]
    assert manage.claude_arguments(settings("bedrock"), classifier_ready=True, user_args=[]) == []


def test_structured_output_toggle():
    s = settings()
    s["claude"]["disable_structured_outputs"] = True
    assert manage.claude_environment(s, "k", {})["CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS"] == "1"
    assert "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS" not in manage.claude_environment(settings(), "k", {})


def test_internal_env_refuses_tty(kgw_home, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    with pytest.raises(common.GatewayError):
        manage.cmd_internal_claude_env(None)
