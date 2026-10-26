"""Platform detection, safe uninstall and the local secret scanner."""

import argparse
import os
from pathlib import Path

import pytest

import common
import manage


@pytest.mark.parametrize("system,machine,expected", [
    ("Darwin", "arm64", "macOS arm64"), ("Linux", "x86_64", "Linux x86_64"), ("Linux", "aarch64", "Linux arm64"),
    ("Windows", "AMD64", "Windows x86_64"), ("Windows", "ARM64", "Windows arm64"),
])
def test_platform_detection(monkeypatch, system, machine, expected):
    monkeypatch.setattr(common.platform, "system", lambda: system)
    monkeypatch.setattr(common.platform, "machine", lambda: machine)
    monkeypatch.setattr(common, "_macos_translated", lambda: False)
    monkeypatch.setattr(common, "_linux_distro", lambda: None)
    assert common.detect_platform().label == expected


def test_rosetta_python_is_flagged(monkeypatch):
    monkeypatch.setattr(common.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(common.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(common, "_macos_translated", lambda: True)
    info = common.detect_platform()
    assert info.is_translated and info.machine == "arm64" and info.python_machine == "x86_64"
    assert "Rosetta" in info.notes[0]


@pytest.mark.parametrize("version,ok", [((3, 10), False), ((3, 11), True), ((3, 13), True), ((3, 14), True),
                                        ((3, 15), False)])
def test_supported_python(version, ok):
    assert common.python_supported(version) == ok


def _un_args(**kw):
    base = dict(remove_venv=False, remove_config=False, remove_credentials=False, yes=True, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _install(kgw_home, monkeypatch, tmp_path):
    s = common.default_settings()
    s["credential_backend"] = "file"
    common.save_settings(s)
    store = common.store_for(s)
    store.set(common.SECRET_BEDROCK_TOKEN, "tok-123456")
    store.set(common.SECRET_MASTER_KEY, "sk-kgw-xyz")
    common.write_runtime_config(s)
    (kgw_home / "logs").mkdir()
    (kgw_home / "logs" / "proxy.log").write_text("x")
    venv = tmp_path / "repo-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n")
    monkeypatch.setenv("KGW_VENV", str(venv))
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)  # venv must live under the "repo"
    return store, venv


def test_uninstall_default_keeps_credentials_settings_and_venv(kgw_home, monkeypatch, tmp_path):
    store, venv = _install(kgw_home, monkeypatch, tmp_path)
    assert manage.cmd_uninstall(_un_args()) == 0
    assert not (kgw_home / "runtime").exists() and not (kgw_home / "logs").exists()
    assert (kgw_home / "settings.json").exists() and venv.exists()
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "tok-123456"


def test_uninstall_everything_when_requested(kgw_home, monkeypatch, tmp_path):
    store, venv = _install(kgw_home, monkeypatch, tmp_path)
    manage.cmd_uninstall(_un_args(remove_venv=True, remove_config=True, remove_credentials=True))
    assert not venv.exists()
    assert not kgw_home.exists()
    assert tmp_path.exists()  # the "repository" itself is never removed


def test_uninstall_requires_confirmation(kgw_home, monkeypatch, tmp_path):
    _install(kgw_home, monkeypatch, tmp_path)
    assert manage.cmd_uninstall(_un_args(yes=False)) == 1
    assert (kgw_home / "runtime").exists()


def test_safe_remove_refuses_outside_paths(tmp_path, kgw_home):
    outside = tmp_path / "user-file.txt"
    outside.write_text("precious")
    with pytest.raises(common.GatewayError):
        manage._safe_remove(outside, [kgw_home])
    with pytest.raises(common.GatewayError):
        manage._safe_remove(common.REPO_ROOT, [common.REPO_ROOT])
    with pytest.raises(common.GatewayError):
        manage._safe_remove(Path.home(), [Path.home().parent])
    assert outside.read_text() == "precious"


def test_secret_scan_finds_patterns_and_stored_values(tmp_path):
    (tmp_path / "ok.py").write_text("print('hello')\n")
    (tmp_path / "leak.env").write_text("AWS_BEARER_TOKEN_BEDROCK=ABSK" + "Q" * 40 + "\n")
    (tmp_path / "exact.txt").write_text("value: my-own-stored-token-xyz\n")
    (tmp_path / "fixture.py").write_text("FAKE = 'sk-kgw-" + "a" * 43 + "'  # kgw-scan: allow\n")
    findings = manage.scan_for_secrets(tmp_path, ["my-own-stored-token-xyz"])
    files = sorted(f[0] for f in findings)
    assert files == ["exact.txt", "leak.env"]


def test_repository_itself_is_clean():
    assert manage.scan_for_secrets(common.REPO_ROOT, []) == []
