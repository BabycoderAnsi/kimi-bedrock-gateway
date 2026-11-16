"""Update checks, verification records and the guarded LiteLLM / Claude Code update flows (no network)."""

import argparse
import json
import subprocess
import sys

import pytest

import common
import manage
import updates
from conftest import ROOT

PYPI = {"info": {"version": "1.110.0rc1"}, "releases": {
    "1.106.0": [{"upload_time": "2026-10-18T10:00:00", "yanked": False}],
    "1.109.1": [{"upload_time": "2026-11-12T01:59:54", "yanked": False}],
    "1.108.9": [{"upload_time": "2026-11-14T21:54:43", "yanked": False}],   # older line, newer upload
    "1.109.2": [{"upload_time": "2026-11-15T00:00:00", "yanked": True}],    # yanked
    "1.110.0rc1": [{"upload_time": "2026-11-15T00:00:00", "yanked": False}],  # pre-release
    "1.111.0": [],                                                          # no files
}}


def test_latest_litellm_is_the_highest_stable_unyanked_version():
    assert updates.latest_litellm(PYPI) == {"version": "1.109.1", "released": "2026-11-12"}


def test_claude_code_tags():
    assert updates.claude_code_tags({"stable": "2.1.311", "latest": "2.1.325", "next": "2.1.326"}) == \
        {"stable": "2.1.311", "latest": "2.1.325"}


def test_verified_records(kgw_home):
    assert updates.is_verified("litellm", common.PINNED_LITELLM)  # what this release was tested with
    assert updates.is_verified("claude_code", common.TESTED_CLAUDE_CODE)
    assert not updates.is_verified("claude_code", "9.9.9") and not updates.is_verified("litellm", None)
    updates.record_verified("claude_code", "9.9.9", "offline-e2e")
    assert updates.is_verified("claude_code", "9.9.9")
    assert common.is_private(kgw_home / "verified.json")
    updates.forget_verified("claude_code", "9.9.9")
    assert not updates.is_verified("claude_code", "9.9.9")


def test_check_reports_each_source_independently(kgw_home, monkeypatch):
    monkeypatch.setattr(updates, "latest_litellm", lambda: {"version": "1.109.1", "released": "2026-11-12"})

    def offline():
        raise common.GatewayError("Could not reach registry.npmjs.org (URLError).", code="network_error")

    monkeypatch.setattr(updates, "claude_code_tags", offline)
    report = updates.check("1.106.0", "2.1.325")
    assert report["litellm"] == {"installed": "1.106.0", "verified": True, "latest": "1.109.1",
                                 "released": "2026-11-12", "update_available": True}
    assert report["claude_code"]["error"].startswith("Could not reach") and not report["claude_code"]["verified"]


def test_dev_tools_never_reinstall_the_pinned_litellm():
    reqs = manage.dev_tool_requirements()
    assert reqs and not any(r.startswith("-r") or "litellm" in r for r in reqs)
    assert any(r.startswith("pytest==") for r in reqs)


def test_contract_check_passes_on_the_installed_litellm():
    proc = subprocess.run([str(common.venv_python()), str(ROOT / "gateway" / "litellm_contract.py"),
                           "--model", "in.moonshotai.kimi-k3"], capture_output=True, text=True, timeout=300)
    report = json.loads(proc.stdout)
    assert proc.returncode == 0, report
    assert {c["name"] for c in report["checks"]} >= {"hook_signatures", "lookaround_dropped",
                                                     "tools_on_chat_completions"}


def test_contract_check_flags_a_changed_litellm(monkeypatch):
    sys.path.insert(0, str(ROOT / "gateway"))
    import litellm_contract  # noqa: PLC0415
    from litellm.integrations.custom_logger import CustomLogger  # noqa: PLC0415
    from litellm.llms.bedrock import common_utils  # noqa: PLC0415

    async def renamed(self, kwargs, call_type, extra):  # a signature change in a new release
        return None

    monkeypatch.setattr(CustomLogger, "async_pre_call_deployment_hook", renamed)
    monkeypatch.setattr(common_utils, "bedrock_runtime_chat_completions_serves_tools_with_reasoning",
                        lambda _m: False)
    failed = {c["name"] for c in litellm_contract.run("in.moonshotai.kimi-k3", "chat_completions")["checks"]
              if not c["ok"]}
    assert failed == {"hook_signatures", "tools_on_chat_completions"}


# ------------------------------------------------------------------------ upgrade flow


@pytest.fixture
def fake_upgrade(kgw_home, tmp_path, monkeypatch):
    """A fake venv layout plus stubs for the slow parts (pip, contract check, test suite, proxy)."""
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "marker").write_text("old")
    monkeypatch.setenv("KGW_VENV", str(venv))
    s = common.default_settings()
    s["credential_backend"] = "file"
    common.save_settings(s)
    state = {"versions": {str(venv): "1.106.0"}, "tests_pass": True, "live_ok": True, "calls": []}

    def install(staging, trial):
        (staging / "bin").mkdir(parents=True)
        (staging / "marker").write_text("new")
        state["versions"][str(staging)] = trial["litellm_version"]

    def version_of(py):
        return state["versions"].get(str(py.parent.parent))

    monkeypatch.setattr(manage, "install_requirements", install)
    monkeypatch.setattr(manage, "venv_litellm_version", version_of)
    monkeypatch.setattr(manage, "installed_litellm_version", lambda: version_of(common.venv_python()))
    monkeypatch.setattr(manage, "run_contract_check", lambda py, s: True)
    monkeypatch.setattr(manage, "ensure_test_tools", lambda py: None)
    monkeypatch.setattr(manage, "run_offline_tests",
                        lambda py, venv, marker, env=None: state["calls"].append(("tests", marker)) or state["tests_pass"])
    monkeypatch.setattr(manage, "proxy_state", lambda s: {"ours": False, "pid": None, "mode": None,
                                                         "port_busy": False, "foreign": False})
    monkeypatch.setattr(manage, "stop_proxy", lambda *a, **k: False)
    monkeypatch.setattr(manage, "run_gateway_selftest", lambda *a, **k: state["live_ok"])
    return venv, state


def _upgrade_args(**kw):
    base = dict(litellm="1.109.1", allow_prerelease=False, rollback=False, yes=True, live=False, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_upgrade_switches_only_after_the_checks_pass(fake_upgrade):
    venv, state = fake_upgrade
    assert manage.cmd_upgrade(_upgrade_args()) == 0
    assert (venv / "marker").read_text() == "new"
    assert (venv.with_name(".venv.previous") / "marker").read_text() == "old"
    assert state["calls"] == [("tests", "not live")]  # the full offline suite, e2e included
    assert common.load_settings()["litellm_version"] == "1.109.1"
    assert updates.is_verified("litellm", "1.109.1")


def test_failed_checks_leave_the_current_install_untouched(fake_upgrade):
    venv, state = fake_upgrade
    state["tests_pass"] = False
    assert manage.cmd_upgrade(_upgrade_args()) == 1
    assert (venv / "marker").read_text() == "old" and not venv.with_name(".venv-upgrade").exists()
    assert common.load_settings()["litellm_version"] == common.PINNED_LITELLM
    assert not updates.is_verified("litellm", "1.109.1")


def test_upgrade_refuses_a_staging_env_with_the_wrong_litellm(fake_upgrade, monkeypatch):
    venv, state = fake_upgrade
    monkeypatch.setattr(manage, "venv_litellm_version", lambda py: "1.106.0")  # e.g. pip put the pin back
    assert manage.cmd_upgrade(_upgrade_args()) == 1
    assert (venv / "marker").read_text() == "old" and state["calls"] == []


def test_failed_live_check_rolls_back(fake_upgrade):
    venv, state = fake_upgrade
    state["live_ok"] = False
    assert manage.cmd_upgrade(_upgrade_args(live=True)) == 1
    assert (venv / "marker").read_text() == "old"
    assert common.load_settings()["litellm_version"] == "1.106.0"
    assert not updates.is_verified("litellm", "1.109.1")


def test_upgrade_defaults_to_the_latest_stable_release(fake_upgrade, monkeypatch):
    venv, _ = fake_upgrade
    monkeypatch.setattr(updates, "latest_litellm", lambda: {"version": "1.109.1", "released": "2026-11-12"})
    assert manage.cmd_upgrade(_upgrade_args(litellm=None)) == 0
    assert (venv / "marker").read_text() == "new"


# ---------------------------------------------------------------------- Claude Code


@pytest.fixture
def fake_claude(kgw_home, monkeypatch):
    common.save_settings(common.default_settings())
    state = {"version": "2.1.325", "verify_ok": True, "calls": []}
    monkeypatch.setattr(manage.validate, "find_claude", lambda: "/home/u/.local/bin/claude")
    monkeypatch.setattr(manage.validate, "claude_is_npm_placeholder", lambda _c: False)
    monkeypatch.setattr(manage.validate, "claude_version", lambda _c: state["version"])
    monkeypatch.setattr(manage, "verify_claude", lambda _c: state["verify_ok"])

    def call(cmd):
        state["calls"].append(cmd)
        if cmd[1:] == ["update"]:
            state["version"] = "2.1.330"
        elif cmd[1] == "install":
            state["version"] = cmd[2]
        return 0

    monkeypatch.setattr(manage.subprocess, "call", call)
    return state


def _update_args(**kw):
    base = dict(target="claude", yes=True, live=False, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_verify_claude_records_only_a_passing_version(fake_claude):
    fake_claude["verify_ok"] = False
    assert manage.cmd_verify(argparse.Namespace(target="claude")) == 1
    assert not updates.is_verified("claude_code", "2.1.325")
    fake_claude["verify_ok"] = True
    assert manage.cmd_verify(argparse.Namespace(target="claude")) == 0
    assert updates.is_verified("claude_code", "2.1.325")


def test_claude_update_is_verified_and_recorded(fake_claude):
    item = {"installed": "2.1.325", "verified": False, "latest": "2.1.330", "update_available": True}
    assert manage.update_claude(_update_args(), item) == 0
    assert fake_claude["calls"] == [["/home/u/.local/bin/claude", "update"]]
    assert updates.is_verified("claude_code", "2.1.330")


def test_failed_claude_update_offers_the_way_back(fake_claude, monkeypatch, capsys):
    fake_claude["verify_ok"] = False
    item = {"installed": "2.1.325", "verified": True, "latest": "2.1.330", "update_available": True}
    assert manage.update_claude(_update_args(), item) == 1
    assert "To go back: /home/u/.local/bin/claude install 2.1.325" in capsys.readouterr().out
    assert fake_claude["version"] == "2.1.330"  # non-interactive: only the hint, nothing is reinstalled
    monkeypatch.setattr(manage, "interactive", lambda _a: True)
    monkeypatch.setattr(common, "confirm", lambda *_a, **_k: True)
    fake_claude["version"] = "2.1.325"
    manage.update_claude(_update_args(), item)
    assert fake_claude["calls"][-1] == ["/home/u/.local/bin/claude", "install", "2.1.325"]
    assert not updates.is_verified("claude_code", "2.1.330")


def test_npm_installs_roll_back_with_npm(tmp_path):
    exe = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude"
    assert manage.claude_rollback_command(str(exe), "2.1.325") == ["npm", "install", "-g",
                                                                   "@anthropic-ai/claude-code@2.1.325"]
