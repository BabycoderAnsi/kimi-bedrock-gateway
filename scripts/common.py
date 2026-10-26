"""Shared, platform-neutral logic for the Kimi Bedrock Gateway manager.

This module only uses the Python standard library so that the setup wrappers can
import it before the virtual environment exists. Optional third-party modules
(``keyring``, ``psutil``) are imported lazily inside the functions that need them.

Nothing in this module ever prints or logs a secret. Every message that could
contain upstream text passes through :func:`redact`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

APP_NAME = "kimi-bedrock-gateway"
KEYRING_SERVICE = APP_NAME
SECRET_BEDROCK_TOKEN = "bedrock-bearer-token"
SECRET_MASTER_KEY = "litellm-master-key"
SECRET_CLASSIFIER_BEDROCK_TOKEN = "classifier-bedrock-bearer-token"
SECRET_CLASSIFIER_ANTHROPIC_KEY = "classifier-anthropic-api-key"
ALL_SECRETS = (
    SECRET_BEDROCK_TOKEN,
    SECRET_MASTER_KEY,
    SECRET_CLASSIFIER_BEDROCK_TOKEN,
    SECRET_CLASSIFIER_ANTHROPIC_KEY,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_PYTHON = ((3, 11), (3, 12), (3, 13), (3, 14))
PINNED_LITELLM = "1.106.0"
MIN_CLAUDE_CODE = (2, 1, 288)  # CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS and classifier routing behaviour
TESTED_CLAUDE_CODE = "2.1.303"
SETTINGS_SCHEMA_VERSION = 1
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# Windows Credential Manager limits a generic credential blob to 2560 bytes and
# keyring stores UTF-16, so long (short-term) Bedrock tokens are split into parts.
_KEYRING_CHUNK = 1000

# Kimi K3 inference profiles and the source Regions AWS documents for each
# (https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html,
# checked 2026-10-19). Kimi K3 has no In-Region (bare model ID) availability.
_US_GEO_SOURCES = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "ca-west-1")
_IN_GEO_SOURCES = ("ap-south-1", "ap-south-2")
_GLOBAL_SOURCES = _US_GEO_SOURCES + _IN_GEO_SOURCES + (
    "eu-central-1", "eu-central-2", "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-2",
    "eu-west-3", "ap-east-2", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-southeast-1",
    "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5", "ap-southeast-6",
    "ap-southeast-7", "il-central-1", "me-central-1", "me-south-1", "af-south-1", "sa-east-1",
)
KNOWN_KIMI_PROFILES = {
    "in.moonshotai.kimi-k3": _IN_GEO_SOURCES,
    "us.moonshotai.kimi-k3": _US_GEO_SOURCES,
    "global.moonshotai.kimi-k3": _GLOBAL_SOURCES,
}

# Claude Sonnet 5 geo profiles used for the optional auto-mode classifier route
# (https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html).
_CLASSIFIER_GEO_BY_PREFIX = (
    (("ap-south-",), "in.anthropic.claude-sonnet-5"),
    (("us-", "ca-"), "us.anthropic.claude-sonnet-5"),
    (("eu-",), "eu.anthropic.claude-sonnet-5"),
    (("ap-southeast-2", "ap-southeast-4"), "au.anthropic.claude-sonnet-5"),
)
KIMI_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")  # AWS model card; "minimal" is not supported
CLASSIFIER_REQUEST_MODEL = "claude-sonnet-5"  # the model ID Claude Code 2.1.303 sends classifier calls to
REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": SETTINGS_SCHEMA_VERSION,
    "aws_region": "ap-south-1",
    "bedrock_model_id": "in.moonshotai.kimi-k3",
    "model_alias": "kimi-k3",
    # Bedrock API for Kimi K3. AWS recommends Chat Completions for this model, and only there is
    # reasoning_effort honoured (Converse accepts but ignores it, so Kimi always thinks at "max").
    "kimi_api": "chat_completions",  # chat_completions | converse
    # Reasoning effort sent to Kimi for every request: none|low|medium|high|xhigh|max, or
    # "client" to pass Claude Code's own effort through (Claude Code sends "high" by default).
    "kimi_reasoning_effort": "low",
    # Explicit Bedrock prompt-cache breakpoints for Kimi K3 on Chat Completions (cache reads bill
    # ~10x cheaper than input; writes 1.25x). Only effective with kimi_api=chat_completions.
    "kimi_prompt_cache": True,
    "proxy_host": "127.0.0.1",
    "proxy_port": 4000,
    "allow_non_loopback": False,
    "litellm_version": PINNED_LITELLM,
    "credential_backend": "keyring",  # keyring | file
    "bedrock_endpoint_url": None,  # optional override (VPC endpoint, or the offline test mock)
    "classifier": {
        "backend": "none",  # none | bedrock | anthropic
        "bedrock_model_id": None,  # default derived from the region
        "anthropic_model": "claude-sonnet-5",
        "request_model": CLASSIFIER_REQUEST_MODEL,
        "credential": "shared",  # shared (reuse the Bedrock token) | separate
    },
    "claude": {
        "disable_structured_outputs": False,
        "gateway_hint_headers": True,
        "extra_args": [],
    },
}


class GatewayError(Exception):
    """An actionable, already-sanitized error for the user."""

    def __init__(self, message: str, hint: str | None = None, code: str = "error"):
        super().__init__(redact(message))
        self.hint = redact(hint) if hint else None
        self.code = code


# --------------------------------------------------------------------------- paths


def state_dir() -> Path:
    """Private per-user directory for settings, runtime config, PID file and logs."""
    override = os.environ.get("KGW_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def runtime_config(self) -> Path:
        return self.runtime_dir / "litellm-config.yaml"

    @property
    def pid_file(self) -> Path:
        return self.root / "run" / "proxy.pid"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def proxy_log(self) -> Path:
        return self.log_dir / "proxy.log"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def file_secrets(self) -> Path:
        return self.root / "secrets.json"

    @property
    def setup_progress(self) -> Path:
        return self.root / "setup-progress.json"


def paths() -> Paths:
    return Paths(state_dir())


def venv_dir() -> Path:
    override = os.environ.get("KGW_VENV")
    return Path(override) if override else REPO_ROOT / ".venv"


def venv_python(venv: Path | None = None) -> Path:
    venv = venv or venv_dir()
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_litellm(venv: Path | None = None) -> Path:
    venv = venv or venv_dir()
    if os.name == "nt":
        return venv / "Scripts" / "litellm.exe"
    return venv / "bin" / "litellm"


# --------------------------------------------------------------------- permissions


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    restrict_permissions(path, directory=True)
    return path


def restrict_permissions(path: Path, directory: bool = False) -> None:
    """Make ``path`` accessible to the current user only.

    POSIX: 0700 for directories, 0600 for files. Windows: replace inherited ACLs
    with a single full-control grant for the current user via ``icacls``. This only
    touches files this tool owns; no machine-wide setting is changed.
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}\\{user}" if domain else user
        grant = f"{principal}:(OI)(CI)F" if directory else f"{principal}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return
    os.chmod(path, 0o700 if directory else 0o600)


def is_private(path: Path) -> bool:
    if os.name == "nt":
        return True  # verified by icacls at write time; no portable stat check
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0


def write_private_text(path: Path, text: str) -> None:
    """Atomically write a user-only file (created 0600 before any content lands)."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    restrict_permissions(tmp)
    os.replace(tmp, path)


# ------------------------------------------------------------------------ settings


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_settings() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_SETTINGS)


def load_settings(p: Paths | None = None) -> dict[str, Any]:
    """User settings layered over application defaults. Missing file -> defaults."""
    p = p or paths()
    if not p.settings.exists():
        return default_settings()
    try:
        raw = json.loads(p.settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GatewayError(
            f"Settings file {p.settings} is not valid JSON ({exc.__class__.__name__}).",
            hint="Fix the file, or run setup and choose 'reset' (a backup is kept).",
            code="invalid_settings",
        ) from None
    if not isinstance(raw, dict):
        raise GatewayError(f"Settings file {p.settings} must contain a JSON object.", code="invalid_settings")
    return _deep_merge(default_settings(), raw)


def save_settings(settings: dict[str, Any], p: Paths | None = None) -> None:
    p = p or paths()
    errors = [i for i in validate_settings(settings) if i.level == "error"]
    if errors:
        raise GatewayError("Refusing to save invalid settings: " + "; ".join(i.message for i in errors),
                           code="invalid_settings")
    write_private_text(p.settings, json.dumps(settings, indent=2, sort_keys=True) + "\n")


def settings_exist(p: Paths | None = None) -> bool:
    return (p or paths()).settings.exists()


@dataclass
class Issue:
    level: str  # error | warning
    message: str
    hint: str | None = None


def default_classifier_model_for_region(region: str) -> str:
    for prefixes, model in _CLASSIFIER_GEO_BY_PREFIX:
        if region.startswith(prefixes):
            return model
    return "global.anthropic.claude-sonnet-5"


def suggest_kimi_profile(region: str) -> str | None:
    for profile, regions in KNOWN_KIMI_PROFILES.items():
        if profile.startswith(("in.", "us.")) and region in regions:
            return profile
    return "global.moonshotai.kimi-k3" if region in _GLOBAL_SOURCES else None


def validate_settings(s: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    region = s.get("aws_region")
    model = s.get("bedrock_model_id")
    alias = s.get("model_alias")
    host = s.get("proxy_host")
    port = s.get("proxy_port")

    if not isinstance(region, str) or not REGION_RE.match(region):
        issues.append(Issue("error", f"aws_region {region!r} is not a valid AWS Region name.",
                            "Use a Region code such as ap-south-1."))
    if not isinstance(model, str) or not MODEL_ID_RE.match(model):
        issues.append(Issue("error", f"bedrock_model_id {model!r} is not a valid Bedrock model or profile ID."))
    elif isinstance(region, str):
        allowed = KNOWN_KIMI_PROFILES.get(model)
        if model == "moonshotai.kimi-k3":
            issues.append(Issue("error", "Kimi K3 has no In-Region availability; the bare model ID cannot be invoked.",
                                f"Use an inference profile such as {suggest_kimi_profile(region) or 'global.moonshotai.kimi-k3'}."))
        elif allowed is not None and region not in allowed:
            suggestion = suggest_kimi_profile(region)
            issues.append(Issue(
                "error",
                f"Inference profile {model} cannot be called from {region} (AWS lists: {', '.join(allowed)}).",
                f"Use {suggestion} for {region}, or pick a Region from that list." if suggestion
                else "Pick a Region from that list.",
            ))
        elif allowed is None:
            issues.append(Issue("warning", f"{model} is not in this tool's table of known Kimi K3 profiles; "
                                           "it will be used as-is and verified only by a live request."))
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        issues.append(Issue("error", f"model_alias {alias!r} must be 1-64 characters of letters, digits, . _ : -"))
    elif alias.lower().startswith(("claude", "anthropic")):
        issues.append(Issue("error", "model_alias must not look like a Claude model ID; Claude Code would treat "
                                     "the Kimi route as a Claude model and could send classifier traffic to it."))
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        issues.append(Issue("error", f"proxy_port {port!r} must be an integer between 1024 and 65535."))
    if not isinstance(host, str) or not host:
        issues.append(Issue("error", "proxy_host must be set."))
    elif host not in LOOPBACK_HOSTS:
        if not s.get("allow_non_loopback"):
            issues.append(Issue("error", f"proxy_host {host} is not a loopback address.",
                                "Binding beyond 127.0.0.1 exposes the gateway to the network. Set "
                                "allow_non_loopback=true only if you understand and accept that."))
        else:
            issues.append(Issue("warning", f"The proxy will listen on {host}. Anyone who can reach that address "
                                           "and knows the master key can spend your Bedrock quota."))
    if s.get("kimi_api") not in ("chat_completions", "converse"):
        issues.append(Issue("error", "kimi_api must be 'chat_completions' or 'converse'."))
    if s.get("kimi_reasoning_effort") not in KIMI_EFFORTS + ("client",):
        issues.append(Issue("error", f"kimi_reasoning_effort must be one of {', '.join(KIMI_EFFORTS)} or client."))
    elif s.get("kimi_api") == "converse" and s.get("kimi_reasoning_effort") != "client":
        issues.append(Issue("warning", "Bedrock Converse ignores reasoning_effort for Kimi K3; kimi_reasoning_effort "
                                       "only takes effect with kimi_api=chat_completions."))
    if not isinstance(s.get("kimi_prompt_cache"), bool):
        issues.append(Issue("error", "kimi_prompt_cache must be true or false."))
    if s.get("credential_backend") not in ("keyring", "file"):
        issues.append(Issue("error", "credential_backend must be 'keyring' or 'file'."))
    endpoint = s.get("bedrock_endpoint_url")
    if endpoint is not None and (not isinstance(endpoint, str) or not re.match(r"^https?://", endpoint)):
        issues.append(Issue("error", "bedrock_endpoint_url must be null or an http(s) URL."))
    elif isinstance(endpoint, str) and endpoint.startswith("http://") and not re.match(
            r"^http://(127\.0\.0\.1|localhost|\[::1\])[:/]", endpoint):
        issues.append(Issue("error", "A plain-http bedrock_endpoint_url is only allowed for loopback test mocks."))

    c = s.get("classifier") or {}
    backend = c.get("backend")
    if backend not in ("none", "bedrock", "anthropic"):
        issues.append(Issue("error", "classifier.backend must be none, bedrock or anthropic."))
    if c.get("credential") not in ("shared", "separate"):
        issues.append(Issue("error", "classifier.credential must be 'shared' or 'separate'."))
    req = c.get("request_model")
    if not isinstance(req, str) or not req.startswith("claude-"):
        issues.append(Issue("error", "classifier.request_model must be the Claude model ID Claude Code requests "
                                     f"for the classifier (default {CLASSIFIER_REQUEST_MODEL})."))
    elif isinstance(alias, str) and req == alias:
        issues.append(Issue("error", "classifier.request_model must differ from model_alias."))
    if backend == "bedrock":
        cmodel = c.get("bedrock_model_id") or default_classifier_model_for_region(region or "")
        if "anthropic.claude" not in cmodel:
            issues.append(Issue("error", f"classifier.bedrock_model_id {cmodel} is not a Claude model. "
                                         "Auto mode requires a supported Claude classifier; Kimi K3 is not one."))
    if backend == "anthropic" and "claude" not in str(c.get("anthropic_model", "")):
        issues.append(Issue("error", "classifier.anthropic_model must be a Claude model."))
    if not isinstance((s.get("claude") or {}).get("extra_args", []), list):
        issues.append(Issue("error", "claude.extra_args must be a list of strings."))
    return issues


def require_valid(s: dict[str, Any]) -> list[Issue]:
    issues = validate_settings(s)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        first = errors[0]
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        raise GatewayError(first.message + more, hint=first.hint, code="invalid_settings")
    return [i for i in issues if i.level == "warning"]


def bedrock_runtime_url(s: dict[str, Any]) -> str:
    return (s.get("bedrock_endpoint_url") or f"https://bedrock-runtime.{s['aws_region']}.amazonaws.com").rstrip("/")


# ---------------------------------------------------------------- runtime config


def render_runtime_config(s: dict[str, Any], template_path: Path | None = None) -> str:
    """Render the LiteLLM config from the repo template plus user settings.

    The output never contains a secret: credentials are referenced as
    ``os.environ/NAME`` and injected into the proxy process environment at start.
    """
    template_path = template_path or REPO_ROOT / "config.yaml"
    text = template_path.read_text(encoding="utf-8")
    c = s["classifier"]
    endpoint = s.get("bedrock_endpoint_url")
    endpoint_line = f"\n      aws_bedrock_runtime_endpoint: {json.dumps(endpoint)}" if endpoint else ""
    classifier_block = ""
    if c["backend"] == "bedrock":
        model = c.get("bedrock_model_id") or default_classifier_model_for_region(s["aws_region"])
        env = "KGW_CLASSIFIER_BEDROCK_TOKEN" if c["credential"] == "separate" else "AWS_BEARER_TOKEN_BEDROCK"
        classifier_block = (
            f"  # Auto-mode classifier: a real Claude model, reachable only by classifier-shaped requests.\n"
            f"  - model_name: {json.dumps(c['request_model'])}\n"
            f"    litellm_params:\n"
            f"      model: {json.dumps('bedrock/' + model)}\n"
            f"      aws_region_name: {json.dumps(s['aws_region'])}\n"
            f"      api_key: os.environ/{env}{endpoint_line}\n"
        )
    elif c["backend"] == "anthropic":
        classifier_block = (
            f"  # Auto-mode classifier: a real Claude model, reachable only by classifier-shaped requests.\n"
            f"  - model_name: {json.dumps(c['request_model'])}\n"
            f"    litellm_params:\n"
            f"      model: {json.dumps('anthropic/' + c['anthropic_model'])}\n"
            f"      api_key: os.environ/KGW_CLASSIFIER_ANTHROPIC_KEY\n"
        )
    replacements = {
        "__MODEL_ALIAS__": json.dumps(s["model_alias"]),
        "__BEDROCK_MODEL__": json.dumps(f"bedrock/{s.get('kimi_api', 'chat_completions')}/{s['bedrock_model_id']}"),
        "__AWS_REGION__": json.dumps(s["aws_region"]),
        "__ENDPOINT_LINE__": endpoint_line,
        "__CLASSIFIER_BLOCK__": classifier_block,
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    leftover = re.findall(r"__[A-Z_]+__", text)
    if leftover:
        raise GatewayError(f"config.yaml template has unknown placeholders: {sorted(set(leftover))}",
                           code="invalid_template")
    return text


def write_runtime_config(s: dict[str, Any], p: Paths | None = None) -> Path:
    p = p or paths()
    write_private_text(p.runtime_config, render_runtime_config(s))
    return p.runtime_config


# --------------------------------------------------------------------- secrets


class SecretStore:
    """Credential storage. ``keyring`` maps to Windows Credential Manager, the macOS
    Keychain or the Secret Service (libsecret) on Linux. ``file`` is an explicit,
    documented fallback: a 0600 JSON file in the private state directory."""

    def __init__(self, backend: str, p: Paths | None = None):
        if backend not in ("keyring", "file"):
            raise GatewayError(f"Unknown credential backend {backend!r}.")
        self.backend = backend
        self.p = p or paths()

    # keyring helpers ---------------------------------------------------------
    @staticmethod
    def _keyring():
        try:
            import keyring  # noqa: PLC0415
            import keyring.errors  # noqa: PLC0415
        except ImportError:
            raise GatewayError("The 'keyring' package is not installed in the virtual environment.",
                               hint="Re-run setup to install dependencies.", code="missing_dependency") from None
        return keyring

    @classmethod
    def keyring_usable(cls) -> tuple[bool, str]:
        """Whether a real, secure keyring backend is available (not the fail/plaintext ones)."""
        try:
            kr = cls._keyring()
            backend = kr.get_keyring()
        except GatewayError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - backend discovery failures are environmental
            return False, exc.__class__.__name__
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        if any(x in name for x in ("fail.Keyring", "null.Keyring", "Plaintext")):
            return False, name
        if "chainer" in name and not getattr(backend, "backends", None):
            return False, name
        return True, name

    def _kr_get(self, name: str) -> str | None:
        kr = self._keyring()
        try:
            head = kr.get_password(KEYRING_SERVICE, name)
            if head is None:
                return None
            if head.startswith("kgw-chunked:"):
                count = int(head.split(":", 1)[1])
                parts = [kr.get_password(KEYRING_SERVICE, f"{name}.part{i}") for i in range(count)]
                if any(part is None for part in parts):
                    raise GatewayError(f"Stored credential '{name}' is incomplete.",
                                       hint="Update it again with the update-bedrock-key command.")
                return "".join(parts)  # type: ignore[arg-type]
            return head
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GatewayError(f"Could not read '{name}' from the system credential store "
                               f"({exc.__class__.__name__}).",
                               hint="Unlock the keychain / Secret Service, or see README 'Credential storage'.",
                               code="keyring_error") from None

    def _kr_set(self, name: str, value: str) -> None:
        kr = self._keyring()
        try:
            self._kr_delete(name)
            if len(value) > _KEYRING_CHUNK:
                parts = [value[i:i + _KEYRING_CHUNK] for i in range(0, len(value), _KEYRING_CHUNK)]
                for i, part in enumerate(parts):
                    kr.set_password(KEYRING_SERVICE, f"{name}.part{i}", part)
                kr.set_password(KEYRING_SERVICE, name, f"kgw-chunked:{len(parts)}")
            else:
                kr.set_password(KEYRING_SERVICE, name, value)
        except Exception as exc:  # noqa: BLE001
            raise GatewayError(f"Could not save '{name}' to the system credential store "
                               f"({exc.__class__.__name__}).", code="keyring_error") from None

    def _kr_delete(self, name: str) -> bool:
        kr = self._keyring()
        removed = False
        try:
            head = kr.get_password(KEYRING_SERVICE, name)
        except Exception:  # noqa: BLE001
            head = None
        if head is not None and head.startswith("kgw-chunked:"):
            for i in range(int(head.split(":", 1)[1])):
                try:
                    kr.delete_password(KEYRING_SERVICE, f"{name}.part{i}")
                except Exception:  # noqa: BLE001
                    pass
        try:
            kr.delete_password(KEYRING_SERVICE, name)
            removed = True
        except Exception:  # noqa: BLE001 - PasswordDeleteError when absent
            pass
        return removed

    # file helpers --------------------------------------------------------------
    def _file_load(self) -> dict[str, str]:
        f = self.p.file_secrets
        if not f.exists():
            return {}
        if not is_private(f):
            raise GatewayError(f"{f} is readable by other users; refusing to use it.",
                               hint=f"Run: chmod 600 '{f}'", code="insecure_permissions")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            raise GatewayError(f"{f} is corrupt.", code="invalid_secrets") from None
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def _file_save(self, data: dict[str, str]) -> None:
        write_private_text(self.p.file_secrets, json.dumps(data, indent=2) + "\n")

    # public API ----------------------------------------------------------------
    def get(self, name: str) -> str | None:
        if self.backend == "keyring":
            return self._kr_get(name)
        return self._file_load().get(name)

    def set(self, name: str, value: str) -> None:
        if not value or value != value.strip() or "\n" in value:
            raise GatewayError("Refusing to store an empty or whitespace-padded credential.")
        if self.backend == "keyring":
            self._kr_set(name, value)
        else:
            data = self._file_load()
            data[name] = value
            self._file_save(data)
        register_secret(value)

    def delete(self, name: str) -> bool:
        if self.backend == "keyring":
            return self._kr_delete(name)
        data = self._file_load()
        if name not in data:
            return False
        del data[name]
        self._file_save(data)
        return True

    def has(self, name: str) -> bool:
        return self.get(name) is not None


def store_for(s: dict[str, Any], p: Paths | None = None) -> SecretStore:
    return SecretStore(s.get("credential_backend", "keyring"), p)


def generate_master_key() -> str:
    """A LiteLLM proxy master key: must start with ``sk-``; 256 bits of CSPRNG entropy."""
    return "sk-kgw-" + secrets.token_urlsafe(32)


def fingerprint(value: str) -> str:
    """A short non-reversible identifier so users can tell credentials apart."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------- redaction

_KNOWN_SECRETS: set[str] = set()
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bABSK[A-Za-z0-9+/=]{20,}"),  # Bedrock long-term API key
    re.compile(r"\bbedrock-api-key-[A-Za-z0-9+/=._-]{20,}"),  # Bedrock short-term API key
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),  # LiteLLM / Anthropic style keys
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key IDs
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)[A-Za-z0-9/+=]{30,}"),
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(X-Amz-Security-Token=)[^&\s]+"),  # kgw-scan: allow (pattern definition)
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _KNOWN_SECRETS.add(value)


def redact(text: Any) -> str:
    """Remove every known secret value and anything shaped like a credential."""
    if text is None:
        return ""
    out = str(text)
    for value in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        out = out.replace(value, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", out)
    return out


# --------------------------------------------------------------- platform checks


@dataclass
class PlatformInfo:
    system: str  # Windows | Linux | Darwin
    machine: str  # normalized: x86_64 | arm64
    python_version: tuple[int, int, int]
    python_machine: str
    is_translated: bool = False  # Rosetta 2 on macOS
    distro: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        names = {"Darwin": "macOS", "Linux": self.distro or "Linux", "Windows": "Windows"}
        return f"{names.get(self.system, self.system)} {self.machine}"


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64", "armv8l"):
        return "arm64"
    return m


def _macos_translated() -> bool:
    try:
        out = subprocess.run(["sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True,
                             timeout=5, check=False).stdout.strip()
        return out == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def _linux_distro() -> str | None:
    try:
        data = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', data, re.M)
    return m.group(1) if m else None


def detect_platform() -> PlatformInfo:
    system = platform.system()
    py_machine = _normalize_arch(platform.machine())
    info = PlatformInfo(system=system, machine=py_machine, python_version=tuple(sys.version_info[:3]),
                        python_machine=py_machine)
    if system == "Darwin":
        translated = _macos_translated()
        info.is_translated = translated
        if translated:
            info.machine = "arm64"
            info.notes.append("This Python runs under Rosetta 2 (x86_64) on Apple Silicon. Use a native arm64 "
                              "Python (python.org universal2 installer or /opt/homebrew/bin/python3).")
    elif system == "Linux":
        info.distro = _linux_distro()
    return info


def python_supported(version: tuple[int, ...] | None = None) -> bool:
    version = version or tuple(sys.version_info[:2])
    return tuple(version[:2]) in SUPPORTED_PYTHON


# ------------------------------------------------------------- network/processes


def port_in_use(host: str, port: int) -> bool:
    """True if something already listens on host:port (or binding is otherwise refused)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            # Without exclusive use, Windows lets a second socket bind over a live listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Ignore TIME_WAIT leftovers; a live listener still makes bind() fail.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def read_pid(p: Paths | None = None) -> dict[str, Any] | None:
    p = p or paths()
    try:
        data = json.loads(p.pid_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None
    except (OSError, ValueError):
        return None


def write_pid(pid: int, s: dict[str, Any], mode: str, p: Paths | None = None) -> None:
    p = p or paths()
    write_private_text(p.pid_file, json.dumps({
        "pid": pid, "host": s["proxy_host"], "port": s["proxy_port"], "mode": mode,
        "config": str(p.runtime_config),
    }) + "\n")


def clear_pid(p: Paths | None = None) -> None:
    (p or paths()).pid_file.unlink(missing_ok=True)


def process_is_our_proxy(pid: int, p: Paths | None = None) -> bool:
    """Confirm that ``pid`` is a LiteLLM process started with *our* runtime config.

    A process merely listening on the port is never assumed to be ours.
    """
    p = p or paths()
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return str(p.runtime_config) in cmdline and "litellm" in cmdline.lower()


def listening_pids(port: int) -> list[int]:
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return []
    pids: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                pids.add(conn.pid)
    except (psutil.AccessDenied, PermissionError, OSError):
        pass  # macOS requires root for other users' sockets; caller falls back to HTTP identity check
    return sorted(pids)


# ------------------------------------------------------------------------ prompts


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def choose(prompt: str, options: Iterable[str], default: str) -> str:
    opts = list(options)
    while True:
        answer = ask(f"{prompt} ({'/'.join(opts)})", default).lower()
        if answer in opts:
            return answer
        print(f"  Please answer one of: {', '.join(opts)}")


def read_secret(prompt: str) -> str:
    """Read a credential without echoing it. Accepts piped stdin for automation."""
    import getpass  # noqa: PLC0415

    if not sys.stdin.isatty():
        value = sys.stdin.readline().strip()
    else:
        value = getpass.getpass(f"{prompt} (input hidden): ").strip()
    if not value:
        raise GatewayError("No value entered.", code="empty_input")
    if any(ch.isspace() for ch in value):
        raise GatewayError("The value contains whitespace; paste the token exactly as issued.",
                           code="invalid_input")
    register_secret(value)
    return value


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None
