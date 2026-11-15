"""Claude Code launcher environment and auto-mode policy."""

import argparse
import errno
import json
import os
import subprocess

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


# The npm package's bin/claude.exe before its postinstall runs (shape of @anthropic-ai/claude-code 2.1.310).
NPM_PLACEHOLDER = ('echo "Error: claude native binary not installed." >&2\n'
                   'echo "Either postinstall did not run (--ignore-scripts, some pnpm configs)" >&2\nexit 1\n')


def _write_exe(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec semantics")
def test_npm_placeholder_is_detected_and_skipped_for_a_real_install(tmp_path, monkeypatch):
    import validate  # noqa: PLC0415

    npm_bin = _write_exe(tmp_path / "nvm" / "bin" / "claude", NPM_PLACEHOLDER)
    monkeypatch.delenv("KGW_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(npm_bin.parent))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert validate.claude_is_npm_placeholder(str(npm_bin))
    assert validate.find_claude() == str(npm_bin)  # nothing better: returned so callers can report it
    with pytest.raises(OSError) as exc:  # what the launcher used to crash with
        subprocess.run([str(npm_bin)], check=False)
    assert exc.value.errno == errno.ENOEXEC  # "Exec format error"
    real = _write_exe(tmp_path / "home" / ".local" / "bin" / "claude", "#!/bin/sh\necho 2.1.310 (Claude Code)\n")
    assert validate.find_claude() == str(real)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec semantics")
def test_launch_reports_placeholder_instead_of_traceback(kgw_home, tmp_path, monkeypatch):
    npm_bin = _write_exe(tmp_path / "nvm" / "bin" / "claude", NPM_PLACEHOLDER)
    monkeypatch.setenv("KGW_CLAUDE_BIN", str(npm_bin))
    with pytest.raises(common.GatewayError) as exc:
        manage.cmd_launch_claude(argparse.Namespace(claude_args=[], non_interactive=True,
                                                    skip_classifier_check=False))
    assert exc.value.code == "claude_broken"
    assert "placeholder" in str(exc.value) and "install.sh" in exc.value.hint


def test_proxy_runs_in_utf8_mode_even_with_a_legacy_codepage(kgw_home, monkeypatch):
    """Windows writes redirected output in cp1252; LiteLLM's banner then crashed the background proxy."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.setenv("PYTHONUTF8", "0")
    s = common.default_settings()
    common.save_settings(s)
    store = common.store_for(dict(s, credential_backend="file"))
    s["credential_backend"] = "file"
    store.set(common.SECRET_BEDROCK_TOKEN, "test-token-value-123")
    store.set(common.SECRET_MASTER_KEY, "sk-kgw-" + "t" * 30)  # kgw-scan: allow (fake test key)
    env = manage.build_proxy_env(s, store)
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("code,what", [("foreign_listener", "master key"), ("alias_missing", "configuration")])
def test_launcher_does_not_wait_for_a_stale_gateway(kgw_home, monkeypatch, code, what):
    import validate

    calls = []
    monkeypatch.setattr(validate, "proxy_health",
                        lambda *_a, **_k: validate.ProbeResult(False, code, "rejected", None))
    monkeypatch.setattr(manage, "proxy_state", lambda _s: {"ours": True, "pid": 4242, "mode": "background",
                                                          "port_busy": True, "foreign": False})
    monkeypatch.setattr(manage.time, "sleep", lambda _s: pytest.fail("must not wait for a gateway that won't change"))
    monkeypatch.setattr(manage, "stop_proxy", lambda *_a, **_k: calls.append("stop"))
    monkeypatch.setattr(manage, "start_proxy", lambda *_a, **_k: calls.append("start") or 0)
    args = argparse.Namespace(non_interactive=True)
    health = manage.ensure_gateway_for_launch(args, settings(), None, "sk-kgw-new")
    assert health.code == "stale_gateway" and not health.ok and calls == []
    assert f"older {what}" in health.message and "gateway restart" in health.hint

    # Interactive: offers the restart, and restarts only with consent.
    monkeypatch.setattr(manage, "interactive", lambda _a: True)
    monkeypatch.setattr(common, "confirm", lambda *_a, **_k: True)
    manage.ensure_gateway_for_launch(args, settings(), None, "sk-kgw-new")
    assert calls == ["stop", "start"]


def test_launcher_still_waits_while_a_start_is_in_progress(kgw_home, monkeypatch):
    import validate

    answers = iter([validate.ProbeResult(False, "alias_missing", "starting"),
                    validate.ProbeResult(True, "ok", "healthy", detail={"models": ["kimi-k3"]})])
    monkeypatch.setattr(validate, "proxy_health", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(manage, "proxy_state", lambda _s: {"ours": True, "pid": 4242, "mode": "background",
                                                          "port_busy": True, "foreign": False})
    common.ensure_private_dir(common.paths().start_lock.parent)
    common.paths().start_lock.write_text("1")
    assert manage.ensure_gateway_for_launch(argparse.Namespace(non_interactive=True), settings(), None, "k").ok
