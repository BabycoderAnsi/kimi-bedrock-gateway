"""Available updates for LiteLLM and Claude Code, and which versions were verified on this machine.

* Latest versions come from public, unauthenticated feeds: PyPI's JSON API for LiteLLM and npm's
  dist-tags for Claude Code. Only a GET is sent; no credentials, settings or machine details.
* "Verified" means this gateway's checks passed against that version: LiteLLM's contract check plus
  the offline end-to-end tests (``gateway upgrade`` / ``gateway verify litellm``), or the Claude Code
  end-to-end tests (``gateway verify claude``). The versions this release was tested with count as
  verified. Records live in ``verified.json`` in the private state directory.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

import common
import validate

PYPI_URL = "https://pypi.org/pypi/litellm/json"
NPM_DIST_TAGS_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
STABLE_RE = re.compile(r"^\d+\.\d+\.\d+$")
KINDS = ("litellm", "claude_code")


def _get_json(url: str, timeout: float = 10) -> Any:
    try:
        status, _, raw = validate._http("GET", url, {"Accept": "application/json"}, timeout=timeout)
    except OSError as exc:
        raise common.GatewayError(f"Could not reach {url.split('/')[2]} ({exc.__class__.__name__}).",
                                  code="network_error") from None
    if status != 200:
        raise common.GatewayError(f"{url.split('/')[2]} answered HTTP {status}.", code="network_error")
    try:
        return json.loads(raw)
    except ValueError:
        raise common.GatewayError(f"{url.split('/')[2]} returned malformed JSON.", code="network_error") from None


def latest_litellm(data: dict | None = None) -> dict[str, str]:
    """Newest stable (X.Y.Z, not yanked) LiteLLM release. PyPI's info.version can be a pre-release, and
    patch releases of an older line are published after newer lines, so compare versions, not dates."""
    data = data if data is not None else _get_json(PYPI_URL)
    best: tuple[tuple[int, ...], str, str] | None = None
    for version, files in (data.get("releases") or {}).items():
        if not STABLE_RE.match(version) or not files or all(f.get("yanked") for f in files):
            continue
        key = common.parse_version(version) or ()
        if best is None or key > best[0]:
            best = (key, version, min(str(f.get("upload_time", "")) for f in files))
    if best is None:
        raise common.GatewayError("PyPI lists no stable LiteLLM release.", code="network_error")
    return {"version": best[1], "released": best[2][:10]}


def claude_code_tags(data: dict | None = None) -> dict[str, str]:
    """npm dist-tags: ``latest`` (what `claude update` installs by default) and ``stable``."""
    data = data if data is not None else _get_json(NPM_DIST_TAGS_URL)
    return {k: v for k, v in data.items() if k in ("latest", "stable") and isinstance(v, str)}


# ------------------------------------------------------------------ verified versions

BUILT_IN = {"litellm": {common.PINNED_LITELLM}, "claude_code": {common.TESTED_CLAUDE_CODE}}


def load_verified(p: common.Paths | None = None) -> dict[str, dict[str, Any]]:
    path = (p or common.paths()).root / "verified.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {k: dict(data.get(k) or {}) for k in KINDS}


def is_verified(kind: str, version: str | None, p: common.Paths | None = None) -> bool:
    return bool(version) and (version in BUILT_IN[kind] or version in load_verified(p)[kind])


def record_verified(kind: str, version: str, how: str, p: common.Paths | None = None) -> None:
    p = p or common.paths()
    data = load_verified(p)
    data[kind][version] = {"at": _dt.datetime.now().isoformat(timespec="seconds"), "checks": how}
    common.ensure_private_dir(p.root)
    common.write_private_text(p.root / "verified.json", json.dumps(data, indent=2, sort_keys=True) + "\n")


def forget_verified(kind: str, version: str, p: common.Paths | None = None) -> None:
    p = p or common.paths()
    data = load_verified(p)
    if data[kind].pop(version, None) is not None:
        common.write_private_text(p.root / "verified.json", json.dumps(data, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- report


def newer(candidate: str | None, installed: str | None) -> bool:
    a, b = common.parse_version(candidate or ""), common.parse_version(installed or "")
    return bool(a and (not b or a > b))


def check(litellm_installed: str | None, claude_installed: str | None, offline: bool = False) -> dict[str, Any]:
    """What is installed, what is available, and whether each is verified. Network errors are reported
    per source instead of raised, so one unreachable feed doesn't hide the other."""
    report: dict[str, Any] = {}
    lite: dict[str, Any] = {"installed": litellm_installed,
                            "verified": is_verified("litellm", litellm_installed)}
    claude: dict[str, Any] = {"installed": claude_installed,
                              "verified": is_verified("claude_code", claude_installed)}
    if not offline:
        try:
            latest = latest_litellm()
            lite.update(latest=latest["version"], released=latest["released"],
                        update_available=newer(latest["version"], litellm_installed))
        except common.GatewayError as exc:
            lite["error"] = str(exc)
        try:
            tags = claude_code_tags()
            claude.update(latest=tags.get("latest"), stable=tags.get("stable"),
                          update_available=newer(tags.get("latest"), claude_installed))
        except common.GatewayError as exc:
            claude["error"] = str(exc)
    report["litellm"], report["claude_code"] = lite, claude
    return report
