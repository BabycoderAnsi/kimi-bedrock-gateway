"""Offline end-to-end: manager -> real LiteLLM proxy -> mock Bedrock (-> real Claude Code if installed).

No AWS access and no real credentials: the mock accepts only the fake token below and
reproduces Kimi K3's documented Converse failures, so these tests prove the gateway
avoids them. Claude Code runs with an isolated CLAUDE_CONFIG_DIR, never your own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT, free_port

pytestmark = pytest.mark.e2e
FAKE_TOKEN = "mock-valid-token"  # kgw-scan: allow (only the local mock accepts it)
VENV_PY = Path(os.environ.get("KGW_VENV", ROOT / ".venv")) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CLAUDE = shutil.which("claude")


class Gateway:
    def __init__(self, tmp: Path, mock_args: list[str] | None = None, classifier: bool = False,
                 extra_settings: dict | None = None):
        self.tmp = tmp
        self.mock_port, self.proxy_port = free_port(), free_port()
        self.home = tmp / "home"
        self.mock_log = tmp / "mock"
        self.env = {**os.environ, "KGW_HOME": str(self.home), "KGW_NON_INTERACTIVE": "1"}
        self.env.pop("KGW_NO_REEXEC", None)
        self.mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_bedrock.py"), "--port",
                                      str(self.mock_port), "--log-dir", str(self.mock_log), *(mock_args or [])])
        time.sleep(0.5)
        self.manage("setup", "--credential-backend", "file", "--endpoint-url",
                    f"http://127.0.0.1:{self.mock_port}", "--token-stdin", "--skip-live", "--yes",
                    *(["--classifier", "bedrock"] if classifier else []), input_text=FAKE_TOKEN + "\n")
        settings_path = self.home / "settings.json"
        data = json.loads(settings_path.read_text())
        data["proxy_port"] = self.proxy_port
        data.update(extra_settings or {})
        settings_path.write_text(json.dumps(data))
        out = self.manage("start", "--background")
        assert "Gateway running" in out, out
        self.master = json.loads((self.home / "secrets.json").read_text())["litellm-master-key"]

    def manage(self, *args: str, input_text: str | None = None, check: bool = True) -> str:
        proc = subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "manage.py"), *args], env=self.env,
                              input=input_text, capture_output=True, text=True, timeout=300)
        if check:
            assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout + proc.stderr

    def post(self, body: dict, path: str = "/v1/messages") -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.proxy_port}{path}", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.master}",
                                              "content-type": "application/json", "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def mock_requests(self) -> list[dict]:
        f = self.mock_log / "requests.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def close(self):
        self.manage("stop", check=False)
        self.mock.terminate()
        self.mock.wait(timeout=10)


@pytest.fixture
def gateway(tmp_path):
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    gw = Gateway(tmp_path)
    yield gw
    gw.close()


@pytest.fixture
def gateway_with_classifier(tmp_path):
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    gw = Gateway(tmp_path, classifier=True)
    yield gw
    gw.close()


MULTI_TURN = {
    "model": "kimi-k3", "max_tokens": 256,
    "tools": [{"name": "mcp__demo__lookup", "description": "demo", "input_schema": {
        "type": "object", "properties": {"name": {"type": "string", "pattern": "^(?!rm\\b).+$"}}}}],
    "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "sig"},
                                          {"type": "text", "text": "hi"}]},
        {"role": "user", "content": "again"},
    ],
}


def test_status_health_and_no_secrets_on_disk(gateway):
    out = gateway.manage("status")
    assert "running" in out and "healthy" in out
    for path in gateway.home.rglob("*"):
        if path.is_file() and path.name != "secrets.json":
            text = path.read_text(errors="replace")
            assert FAKE_TOKEN not in text and gateway.master not in text, path


CLAUDE_CODE_EFFORT = {"thinking": {"type": "adaptive", "display": "omitted"}, "output_config": {"effort": "high"}}


def test_multi_turn_with_reasoning_and_lookaround_tool_succeeds(gateway):
    status, body = gateway.post({**MULTI_TURN, **CLAUDE_CODE_EFFORT})
    assert status == 200, body
    assert body["content"][-1]["text"] == "mock reply from in.moonshotai.kimi-k3"
    rec = gateway.mock_requests()[-1]
    sent = rec["body"]
    assert rec["op"] == "chat-completions" and rec["auth"] == "bearer"  # stayed on Chat Completions
    assert sent["reasoning_effort"] == "low"  # gateway default, not Claude Code's "high"
    assert not any(m.get("reasoning_content") for m in sent["messages"])  # prior reasoning removed
    assert "(?!" not in json.dumps(sent["tools"])  # look-around dropped (LiteLLM only does this on Converse)
    assert len(sent["tools"]) == 1


def test_converse_fallback_setting(tmp_path):
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    gw = Gateway(tmp_path, extra_settings={"kimi_api": "converse"})
    try:
        status, body = gw.post({**MULTI_TURN, **CLAUDE_CODE_EFFORT})
        assert status == 200, body
        rec = gw.mock_requests()[-1]
        assert rec["op"] == "converse"
        assert rec["body"]["messages"][1]["content"] == [{"text": "hi"}]
        assert "(?!" not in json.dumps(rec["body"]["toolConfig"]) and '"cachePoint"' not in json.dumps(rec["body"])
    finally:
        gw.close()


def test_application_inference_profile_uses_converse(tmp_path):
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    arn = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123xyz"
    gw = Gateway(tmp_path, extra_settings={"bedrock_model_id": arn, "kimi_api": "converse"})
    try:
        cached = {"system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
                  "messages": MULTI_TURN["messages"][:-1] + [{"role": "user", "content": [
                      {"type": "text", "text": "again", "cache_control": {"type": "ephemeral"}}]}]}
        status, body = gw.post({**MULTI_TURN, **CLAUDE_CODE_EFFORT, **cached})
        assert status == 200, body
        rec = gw.mock_requests()[-1]
        assert (rec["op"], rec["model"]) == ("converse", arn)
        assert '"cachePoint"' not in json.dumps(rec["body"])  # LiteLLM would add one behind an ARN
        assert "(?!" not in json.dumps(rec["body"]["toolConfig"])  # LiteLLM can't tell it's Kimi; the hook still strips
    finally:
        gw.close()


def test_access_key_auth_signs_every_request(tmp_path):
    """set-auth access-key: the manager's probe and LiteLLM's calls are both SigV4-signed and verified."""
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    sys.path.insert(0, str(ROOT / "tests"))
    from mock_bedrock import VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY  # noqa: PLC0415
    arn = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123xyz"
    gw = Gateway(tmp_path, extra_settings={"bedrock_model_id": arn, "kimi_api": "converse"})
    try:
        bad = gw.manage("set-auth", "access-key", input_text=f"{VALID_ACCESS_KEY_ID}\n{'x' * 40}\n", check=False)
        assert "not saved" in bad and json.loads((gw.home / "settings.json").read_text())["aws_auth"] == "bearer"
        out = gw.manage("set-auth", "access-key", "--restart",
                        input_text=f"{VALID_ACCESS_KEY_ID}\n{VALID_SECRET_ACCESS_KEY}\n")
        assert "answered" in out and VALID_SECRET_ACCESS_KEY not in out and VALID_ACCESS_KEY_ID not in out
        assert "aws_secret_access_key: os.environ/KGW_AWS_SECRET_ACCESS_KEY" in \
            (gw.home / "runtime" / "litellm-config.yaml").read_text()
        status, body = gw.post(MULTI_TURN)
        assert status == 200, body
        rec = gw.mock_requests()[-1]
        assert (rec["op"], rec["auth"], rec["token_valid"]) == ("converse", "sigv4", True)
        doctor = gw.manage("doctor", "--live")
        assert "aws-secret-access-key: stored" in doctor and "answered" in doctor
    finally:
        gw.close()


def test_background_start_and_logs_survive_a_legacy_codepage(tmp_path, monkeypatch):
    """Recreates Windows' cp1252 output on any OS: the proxy must start and 'logs' must not crash."""
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    gw = Gateway(tmp_path)  # starts the proxy in the background (log redirected to a file)
    try:
        status, body = gw.post(MULTI_TURN)
        assert status == 200, body
        out = gw.manage("logs", "-n", "200")  # banner lines include characters cp1252 can't encode
        assert "Traceback" not in out and "UnicodeEncodeError" not in out
        assert "UnicodeEncodeError" not in (gw.home / "logs" / "proxy.log").read_text(encoding="utf-8")
    finally:
        gw.close()


def test_classifier_requests_never_reach_kimi(gateway):
    body = {"model": "kimi-k3", "max_tokens": 64, "stop_sequences": ["</block>"],
            "system": "You are a security monitor for autonomous AI coding agents.",
            "messages": [{"role": "user", "content": "<transcript>x</transcript>"}]}
    status, resp = gateway.post(body)
    assert status == 403 and "kgw_classifier_refused" in json.dumps(resp)
    assert not any("security monitor" in json.dumps(r["body"]) for r in gateway.mock_requests())


def test_unauthenticated_requests_rejected(gateway):
    req = urllib.request.Request(f"http://127.0.0.1:{gateway.proxy_port}/v1/messages",
                                 data=json.dumps(MULTI_TURN).encode(), headers={"content-type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=30)
    assert exc.value.code in (400, 401, 403)


def test_invalid_upstream_token_is_reported(gateway):
    gateway.manage("update-key", "--yes", "--no-validate", input_text="wrong-token-123456\n")
    gateway.manage("restart")
    out = gateway.manage("health", "--live", check=False)
    assert "invalid_token" in out or "API Key" in out or "failed" in out
    assert "wrong-token-123456" not in out
    gateway.manage("update-key", "--rollback", "--yes")


def test_classifier_route_serves_only_classifier(gateway_with_classifier):
    gw = gateway_with_classifier
    status, resp = gw.post({"model": "claude-sonnet-5", "max_tokens": 16, "stop_sequences": ["</block>"],
                            "thinking": {"type": "disabled"},
                            "system": "You are a security monitor for autonomous AI coding agents.",
                            "messages": [{"role": "user", "content": "x"}]})
    assert status == 200 and "<block>no</block>" in resp["content"][0]["text"]
    sent = gw.mock_requests()[-1]
    assert sent["model"] == "in.anthropic.claude-sonnet-5" and sent["body"]["thinking"] == {"type": "disabled"}
    status, resp = gw.post({**MULTI_TURN, "model": "claude-sonnet-5"})
    assert status == 403 and "kgw_classifier_only" in json.dumps(resp)


# ----------------------------------------------------------- real Claude Code

def _claude(gw: Gateway, workdir: Path, *args: str, launcher_flags: tuple[str, ...] = ()) -> dict:
    env = {**gw.env, "CLAUDE_CONFIG_DIR": str(gw.tmp / "claude-config")}
    proc = subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "manage.py"), "launch-claude", *launcher_flags,
                           "--", "-p", "run the probe", "--output-format", "json", *args],
                          env=env, cwd=workdir, capture_output=True, text=True, timeout=300)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    return json.loads(lines[-1])


needs_claude = pytest.mark.skipif(not CLAUDE, reason="Claude Code not installed")


@needs_claude
def test_claude_code_round_trip(gateway, tmp_path):
    result = _claude(gateway, tmp_path)
    assert result["result"] == "mock reply from in.moonshotai.kimi-k3"
    assert not result["is_error"]


def _auto_gateway(tmp_path, mode: str, cmd: str) -> Gateway:
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    return Gateway(tmp_path, ["--kimi-tool-cmd", cmd, "--classifier-mode", mode], classifier=True)


@needs_claude
def test_auto_mode_classifier_allows_harmless_command(tmp_path):
    gw = _auto_gateway(tmp_path, "allow", 'python3 -c "print(6*7)"')
    try:
        result = _claude(gw, tmp_path, "--permission-mode", "auto")
        assert result["permission_denials"] == []
        classifier_calls = [r for r in gw.mock_requests() if "security monitor" in json.dumps(r["body"])]
        assert classifier_calls and all(r["model"] == "in.anthropic.claude-sonnet-5" for r in classifier_calls)
    finally:
        gw.close()


@needs_claude
def test_auto_mode_with_application_profiles_and_access_keys(tmp_path):
    """Kimi and the classifier each behind an application-profile ARN, SigV4-signed (the ARN-only IAM setup)."""
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    sys.path.insert(0, str(ROOT / "tests"))
    from mock_bedrock import VALID_ACCESS_KEY_ID, VALID_SECRET_ACCESS_KEY  # noqa: PLC0415
    kimi_arn = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/kimi0001"
    # The mock treats an ARN containing "claude" as a Claude model; real profile IDs are opaque.
    claude_arn = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/claude0001"
    gw = Gateway(tmp_path, ["--kimi-tool-cmd", 'python3 -c "print(6*7)"', "--classifier-mode", "allow"],
                 extra_settings={"bedrock_model_id": kimi_arn, "kimi_api": "converse"})
    try:
        gw.manage("set-auth", "access-key", input_text=f"{VALID_ACCESS_KEY_ID}\n{VALID_SECRET_ACCESS_KEY}\n")
        out = gw.manage("classifier", "enable", "--model-id", claude_arn, "--restart")
        assert "answered" in out, out
        config = (gw.home / "runtime" / "litellm-config.yaml").read_text()
        assert f'model_id: "{claude_arn}"' in config and '"bedrock/anthropic.claude-sonnet-5"' in config
        result = _claude(gw, tmp_path, "--permission-mode", "auto")
        assert result["permission_denials"] == [], result
        reqs = gw.mock_requests()
        classifier_calls = [r for r in reqs if "security monitor" in json.dumps(r["body"])]
        assert classifier_calls and all(r["model"] == claude_arn for r in classifier_calls)
        assert all(r["auth"] == "sigv4" and r["token_valid"] for r in reqs if r["model"] in (kimi_arn, claude_arn))
        assert any(r["model"] == kimi_arn for r in reqs)
    finally:
        gw.close()


@needs_claude
def test_launcher_starts_a_stopped_gateway(gateway, tmp_path):
    """No separate start step: claude-kimi brings the gateway up in the background on demand."""
    gateway.manage("stop")
    assert "not running" in gateway.manage("status", check=False)
    result = _claude(gateway, tmp_path)
    assert result["result"] == "mock reply from in.moonshotai.kimi-k3" and not result["is_error"]
    assert "running, PID" in gateway.manage("status", check=False)


def test_kimi_self_review_requires_consent(gateway):
    out = gateway.manage("classifier", "enable", "--backend", "kimi", check=False)
    assert "without explicit consent" in out
    assert json.loads((gateway.home / "settings.json").read_text())["classifier"]["backend"] == "none"


@needs_claude
def test_auto_mode_with_kimi_self_review_opt_in(tmp_path):
    """Opt-in: Claude Code's classifier slot is served by Kimi (one call per check, no failing Claude route)."""
    if not VENV_PY.exists():
        pytest.skip("project venv not installed")
    arn = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/kimi0001"
    gw = Gateway(tmp_path, ["--kimi-tool-cmd", 'python3 -c "print(6*7)"', "--classifier-mode", "allow"],
                 extra_settings={"bedrock_model_id": arn, "kimi_api": "converse"})
    try:
        out = gw.manage("classifier", "enable", "--backend", "kimi", "--accept-risk", "--restart")
        assert "UNSAFE" in out
        proc = subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "manage.py"), "launch-claude", "--", "-p",
                               "run the probe", "--output-format", "json", "--permission-mode", "auto"],
                              env={**gw.env, "CLAUDE_CONFIG_DIR": str(gw.tmp / "claude-config")}, cwd=tmp_path,
                              capture_output=True, text=True, timeout=300)
        assert "reviewer is Kimi K3 itself" in proc.stdout + proc.stderr
        result = json.loads([line for line in proc.stdout.splitlines() if line.startswith("{")][-1])
        assert result["permission_denials"] == [], result
        checks = [r for r in gw.mock_requests() if "security monitor" in json.dumps(r["body"])]
        assert checks and all(r["model"] == arn for r in checks)
        assert all("stopSequences" not in json.dumps(r["body"].get("inferenceConfig", {})) for r in checks)
        assert "Invalid model name" not in (gw.home / "logs" / "proxy.log").read_text()
    finally:
        gw.close()


@needs_claude
def test_auto_mode_classifier_block_is_enforced(tmp_path):
    gw = _auto_gateway(tmp_path, "block-rm", "python3 -c \"print('rm -rf simulated')\"")
    try:
        result = _claude(gw, tmp_path, "--permission-mode", "auto")
        assert [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]
    finally:
        gw.close()


@needs_claude
def test_auto_mode_fallback_to_kimi_is_refused(tmp_path):
    gw = _auto_gateway(tmp_path, "404", 'python3 -c "print(6*7)"')
    try:
        result = _claude(gw, tmp_path, "--permission-mode", "auto", launcher_flags=("--skip-classifier-check",))
        assert [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]
        kimi_classifier = [r for r in gw.mock_requests()
                           if "kimi" in r["model"] and "security monitor" in json.dumps(r["body"])]
        assert kimi_classifier == []
        assert "refused an auto-mode classifier request" in (gw.home / "logs" / "proxy.log").read_text()
    finally:
        gw.close()


@needs_claude
def test_auto_mode_disabled_without_classifier(gateway, tmp_path):
    gateway.mock.terminate()
    gateway.mock.wait(timeout=10)
    gateway.mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_bedrock.py"), "--port",
                                     str(gateway.mock_port), "--log-dir", str(gateway.mock_log),
                                     "--kimi-tool-cmd", 'python3 -c "print(6*7)"'])
    time.sleep(0.5)
    result = _claude(gateway, tmp_path, "--permission-mode", "auto")
    assert [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]  # no classifier -> no auto-approval
    assert not any("security monitor" in json.dumps(r["body"]) for r in gateway.mock_requests())
