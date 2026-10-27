import argparse
import os
import sys
import types

import pytest

import common
import manage
import userbin


class FakeWinreg(types.ModuleType):
    """Just enough of winreg for HKCU/HKLM Environment\\Path, recording writes."""

    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    REG_SZ, REG_EXPAND_SZ = 1, 2
    KEY_READ, KEY_SET_VALUE = 1, 2

    def __init__(self, user_path=None, user_kind=2, machine_path=""):
        super().__init__("winreg")
        self.values = {("HKCU", "Path"): (user_path, user_kind)} if user_path is not None else {}
        self.values[("HKLM", "Path")] = (machine_path, 2)

    class _Key:
        def __init__(self, root):
            self.root = root

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, key, reserved=0, access=0):  # noqa: N802
        return self._Key(root)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):  # noqa: N802
        if (key.root, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[(key.root, name)]

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[(key.root, name)] = (value, kind)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(userbin, "_is_windows", lambda: True)

    def install(**kw):
        reg = FakeWinreg(**kw)
        monkeypatch.setitem(sys.modules, "winreg", reg)
        return reg
    return install


def test_posix_command_install_refresh_and_remove(kgw_home, isolated_login_items):
    st = userbin.command_status()
    assert not st["installed"] and not st["foreign"]
    assert userbin.install_command() is True
    path = userbin.command_path()
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n") and userbin.MARKER in text and "claude-kimi.sh" in text
    assert f"export KGW_HOME='{kgw_home}'" in text  # a non-default install remembers its home
    assert os.access(path, os.X_OK)
    assert userbin.install_command() is False  # already current
    assert userbin.command_status()["current"]
    assert userbin.remove_command() and not path.exists()


def test_foreign_command_is_never_overwritten_or_removed(kgw_home):
    path = userbin.command_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho somebody else's tool\n")
    assert userbin.command_status()["foreign"]
    with pytest.raises(common.GatewayError) as exc:
        userbin.install_command()
    assert exc.value.code == "command_conflict"
    assert userbin.remove_command() is False and path.read_text().endswith("tool\n")


def test_windows_wrapper_calls_venv_python_and_passes_arguments(kgw_home, windows):
    windows(user_path="")
    text = userbin.wrapper_content()
    assert userbin.command_path().name == "claude-kimi.cmd"
    assert text.startswith("@echo off\r\n") and userbin.MARKER in text
    assert "launch-claude -- %*" in text and str(common.venv_python()) in text
    assert "setlocal" in text and f'set "KGW_HOME={kgw_home}"' in text
    assert "exit /b %ERRORLEVEL%" in text


def test_windows_user_path_append_keeps_entries_and_type(windows, monkeypatch, tmp_path):
    reg = windows(user_path=r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps;C:\Tools", user_kind=2)
    monkeypatch.setenv("PATH", "/usr/bin")
    target = tmp_path / "bin"
    assert userbin.path_status(target) == {"now": False, "persistent": False}
    assert userbin.add_to_user_path(target) is True
    value, kind = reg.values[("HKCU", "Path")]
    assert value == rf"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps;C:\Tools;{target}"
    assert kind == reg.REG_EXPAND_SZ  # unexpanded %VARS% stay valid
    assert userbin.path_status(target)["persistent"] and str(target) in os.environ["PATH"]
    assert userbin.add_to_user_path(target) is False  # idempotent
    assert reg.values[("HKCU", "Path")][0].count(str(target)) == 1


def test_windows_user_path_created_when_missing_and_machine_path_counts(windows, tmp_path):
    reg = windows(user_path=None, machine_path=str(tmp_path / "already"))
    assert userbin.path_status(tmp_path / "already")["persistent"]
    assert userbin.add_to_user_path(tmp_path / "bin") is True
    assert reg.values[("HKCU", "Path")] == (str(tmp_path / "bin"), reg.REG_EXPAND_SZ)


def test_path_change_refused_off_windows(tmp_path):
    with pytest.raises(common.GatewayError):
        userbin.add_to_user_path(tmp_path)
    assert "export PATH=" in userbin.posix_path_hint(tmp_path)


def test_installer_is_official_and_blocked_in_tests():
    assert "https://claude.ai/install.sh" in " ".join(userbin.claude_installer_command())
    with pytest.raises(common.GatewayError) as exc:
        userbin.install_claude_code()
    assert exc.value.code == "install_disabled"


def _args(**kw):
    base = {"non_interactive": True, "install_claude": False, "install_command": False, "no_command": False,
            "add_to_path": False}
    return argparse.Namespace(**{**base, **kw})


def test_setup_offers_claude_install_only_with_consent(kgw_home, monkeypatch, capsys):
    plat = common.detect_platform()
    state = {"usable": False, "ran": 0}
    monkeypatch.setattr(manage, "_claude_usable", lambda: state["usable"])
    monkeypatch.setattr(manage.validate, "find_claude", lambda: None)

    def fake_install():
        state["ran"] += 1
        state["usable"] = True
        return 0
    monkeypatch.setattr(userbin, "install_claude_code", fake_install)
    manage.ensure_claude_code(_args(), plat)  # non-interactive, no flag: only a hint
    assert state["ran"] == 0 and "install.sh" in capsys.readouterr().out
    manage.ensure_claude_code(_args(install_claude=True), plat)
    assert state["ran"] == 1 and "Claude Code installed" in capsys.readouterr().out


def test_setup_command_step(kgw_home, monkeypatch, capsys):
    assert manage.ensure_claude_kimi_command(_args()) == (False, False)  # no consent: nothing written
    assert not userbin.command_path().exists()
    monkeypatch.setattr(userbin, "path_status", lambda d=None: {"now": False, "persistent": False})
    ready, changed = manage.ensure_claude_kimi_command(_args(install_command=True))
    assert userbin.command_status()["installed"] and (ready, changed) == (False, False)
    assert "export PATH=" in capsys.readouterr().out  # POSIX: hint only, no shell-file edits
    monkeypatch.setattr(userbin, "path_status", lambda d=None: {"now": True, "persistent": True})
    assert manage.ensure_claude_kimi_command(_args()) == (True, False)  # already installed: no prompt


def test_setup_command_step_adds_windows_path_with_consent(kgw_home, monkeypatch, windows, capsys):
    reg = windows(user_path=r"C:\Tools")
    monkeypatch.setattr(manage.os, "name", "nt")
    monkeypatch.setattr(userbin, "install_command", lambda: True)
    monkeypatch.setattr(userbin, "command_status", lambda: {"path": "x", "installed": True, "foreign": False,
                                                             "current": True, "dir_on_path": {}})
    assert manage.ensure_command_dir_on_path(_args()) is False  # no consent
    assert reg.values[("HKCU", "Path")][0] == r"C:\Tools"
    assert manage.ensure_command_dir_on_path(_args(add_to_path=True)) is True
    assert reg.values[("HKCU", "Path")][0].endswith(str(userbin.command_dir()))
