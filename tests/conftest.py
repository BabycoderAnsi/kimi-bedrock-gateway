"""Shared fixtures. Tests never touch the real keychain, real settings or AWS."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "gateway"))

import common  # noqa: E402


class MemoryKeyring:
    """In-memory stand-in for the keyring module (records every stored value)."""

    def __init__(self, max_len: int | None = None):
        self.data: dict[tuple[str, str], str] = {}
        self.max_len = max_len

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.max_len is not None and len(value) > self.max_len:
            raise ValueError("blob too large")  # like Windows Credential Manager
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.data:
            raise KeyError(name)
        del self.data[(service, name)]


@pytest.fixture(autouse=True)
def isolated_login_items(tmp_path_factory, monkeypatch):
    """Never write real login items or call launchctl from tests (subprocesses inherit this)."""
    base = tmp_path_factory.mktemp("login-items")
    for var in ("KGW_LAUNCH_AGENTS_DIR", "KGW_XDG_AUTOSTART_DIR", "KGW_STARTUP_DIR"):
        monkeypatch.setenv(var, str(base))
    monkeypatch.setenv("KGW_AUTOSTART_NO_LOAD", "1")
    return base


@pytest.fixture
def kgw_home(tmp_path, monkeypatch):
    home = tmp_path / "kgw-home"
    monkeypatch.setenv("KGW_HOME", str(home))
    monkeypatch.setenv("KGW_NO_REEXEC", "1")
    monkeypatch.setenv("KGW_NON_INTERACTIVE", "1")
    common._KNOWN_SECRETS.clear()
    return home


@pytest.fixture
def memory_keyring(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.setattr(common.SecretStore, "_keyring", staticmethod(lambda: kr))
    return kr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port():
    return free_port()
