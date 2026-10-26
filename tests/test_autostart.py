import os
import plistlib
import time

import pytest

import autostart
import common
import manage


@pytest.fixture
def on(monkeypatch):
    def set_system(name):
        monkeypatch.setattr(autostart, "_system", lambda: name)
    return set_system


def test_macos_login_items(kgw_home, isolated_login_items, on, tmp_path):
    on("Darwin")
    written = autostart.enable(open_claude=True, claude_workdir=tmp_path, load_gateway_now=True)
    gateway = plistlib.loads(autostart.entry_path("gateway").read_bytes())
    assert gateway["Label"] == autostart.label("gateway") != autostart.LABEL_BASE  # KGW_HOME gets its own label
    assert gateway["ProgramArguments"][-3:] == ["--non-interactive", "start", "--background"]
    assert gateway["EnvironmentVariables"] == {"KGW_HOME": str(kgw_home)}
    assert gateway["RunAtLoad"] and gateway["AbandonProcessGroup"] and gateway["LimitLoadToSessionType"] == "Aqua"
    claude = plistlib.loads(autostart.entry_path("claude").read_bytes())
    script = autostart.claude_command_file()
    assert claude["ProgramArguments"] == ["/usr/bin/open", "-a", "Terminal", str(script)]
    text = script.read_text()
    assert f"cd '{tmp_path}'" in text and "claude-kimi.sh" in text and f"export KGW_HOME='{kgw_home}'" in text
    assert os.stat(script).st_mode & 0o777 == 0o700
    assert all(p.parent in (isolated_login_items, script.parent) for p in written)
    # Re-enabling without --open-claude removes the Claude login item.
    autostart.enable(open_claude=False, claude_workdir=tmp_path, load_gateway_now=False)
    st = autostart.status()
    assert st["gateway"]["installed"] and not st["claude"]["installed"] and not script.exists()
    assert {p.name for p in autostart.disable()} == {autostart.entry_path("gateway").name}
    assert not autostart.status()["gateway"]["installed"]


def test_linux_desktop_entries_quote_paths(kgw_home, on, tmp_path):
    on("Linux")
    workdir = tmp_path / "my projects"
    workdir.mkdir()
    autostart.enable(open_claude=True, claude_workdir=workdir, load_gateway_now=False)
    gateway = autostart.entry_path("gateway").read_text()
    assert gateway.startswith("[Desktop Entry]") and "Terminal=false" in gateway
    assert '"--non-interactive" "start" "--background"' in gateway and f'"KGW_HOME={kgw_home}"' in gateway
    claude = autostart.entry_path("claude").read_text()
    assert "Terminal=true" in claude and "my projects" in claude and "claude-kimi.sh" in claude


def test_windows_startup_cmd(kgw_home, on, tmp_path):
    on("Windows")
    autostart.enable(open_claude=True, claude_workdir=tmp_path, load_gateway_now=False)
    gateway = autostart.entry_path("gateway").read_text()
    assert autostart.entry_path("gateway").suffix == ".cmd"
    assert f'set "KGW_HOME={kgw_home}"' in gateway and "pythonw.exe" in gateway and "start --background" in gateway
    claude = autostart.entry_path("claude").read_text()
    assert "claude-kimi.ps1" in claude and "-ExecutionPolicy Bypass -File" in claude  # per process, not machine-wide


def test_default_install_keeps_unsuffixed_names(monkeypatch, tmp_path):
    monkeypatch.delenv("KGW_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert common.instance_suffix() == ""
    assert common.keyring_service() == common.KEYRING_SERVICE
    assert autostart.label("gateway") == autostart.LABEL_BASE


def test_second_install_never_shares_keyring_entries(kgw_home, memory_keyring, monkeypatch, tmp_path):
    store = common.SecretStore("keyring")
    store.set(common.SECRET_MASTER_KEY, "sk-kgw-test-instance-value")  # kgw-scan: allow (fake test key)
    assert store.service.startswith(common.KEYRING_SERVICE + "@")
    assert (common.KEYRING_SERVICE, common.SECRET_MASTER_KEY) not in memory_keyring.data
    monkeypatch.setenv("KGW_HOME", str(tmp_path / "other"))
    assert common.SecretStore("keyring").get(common.SECRET_MASTER_KEY) is None


def test_start_lock_serialises_and_recovers_from_stale(kgw_home, monkeypatch):
    p = common.paths()
    manage.acquire_start_lock(p)
    assert p.start_lock.read_text() == str(os.getpid())
    with pytest.raises(common.GatewayError) as exc:
        manage.acquire_start_lock(p, wait=0.6)  # held by a live process (us)
    assert exc.value.code == "start_in_progress"
    manage.release_start_lock(p)
    assert not p.start_lock.exists()
    p.start_lock.write_text("999999999")  # holder is gone
    monkeypatch.setattr(manage, "_pid_alive", lambda pid: False)
    t0 = time.time()
    manage.acquire_start_lock(p, wait=5)
    assert time.time() - t0 < 2 and p.start_lock.read_text() == str(os.getpid())
    manage.release_start_lock(p)


def test_start_gateway_on_launch_setting_validated():
    s = dict(common.default_settings(), start_gateway_on_launch="yes")
    assert any("start_gateway_on_launch" in i.message for i in common.validate_settings(s) if i.level == "error")
    assert common.default_settings()["start_gateway_on_launch"] is True
