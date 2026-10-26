"""Static checks for the platform wrappers (shellcheck, PowerShell parser) when available."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

SHELL_SCRIPTS = sorted(ROOT.glob("*.sh")) + [ROOT / "scripts" / "lib.sh"]
PS_SCRIPTS = sorted(ROOT.glob("*.ps1")) + sorted((ROOT / "scripts").glob("*.ps1"))


def _shellcheck():
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = Path(sys.executable).parent / "shellcheck"
    return str(candidate) if candidate.exists() else None


@pytest.mark.skipif(not _shellcheck(), reason="shellcheck not installed (pip install shellcheck-py)")
def test_shellcheck_clean():
    proc = subprocess.run([_shellcheck(), "-x", *map(str, SHELL_SCRIPTS)], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout


def test_shell_scripts_are_executable_and_strict():
    for script in ROOT.glob("*.sh"):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
        assert "set -euo pipefail" in script.read_text()
        code = [line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")]
        # sudo may appear inside printed advice ("sudo apt install ..."), never as a command
        assert not any(re.match(r"\s*(exec\s+)?sudo\b", line) for line in code), script.name


def _pwsh():
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(not _pwsh(), reason="PowerShell not installed")
def test_powershell_syntax():
    for script in PS_SCRIPTS:
        cmd = ("$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
               f"'{script}', [ref]$null, [ref]$e) | Out-Null; if ($e) {{ $e | ForEach-Object {{ $_.ToString() }}; exit 1 }}")
        proc = subprocess.run([_pwsh(), "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                              text=True)
        assert proc.returncode == 0, f"{script.name}: {proc.stdout}{proc.stderr}"


def test_powershell_scripts_never_change_machine_policy():
    for script in PS_SCRIPTS:
        text = script.read_text()
        assert "Set-ExecutionPolicy" not in text
        assert "[Environment]::SetEnvironmentVariable" not in text
        assert "-Scope Machine" not in text
