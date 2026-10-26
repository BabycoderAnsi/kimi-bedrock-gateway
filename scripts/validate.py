"""Connectivity and compatibility probes for the Kimi Bedrock Gateway.

Standard library only. Every message returned here is passed through
``common.redact`` and response bodies are truncated, so callers can print results
directly. Credentials are only ever sent to the configured Bedrock endpoint, the
configured Anthropic API endpoint, or the local proxy - never anywhere else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import common

PROBE_TIMEOUT = 90


@dataclass
class ProbeResult:
    ok: bool
    code: str
    message: str
    hint: str | None = None
    detail: dict[str, Any] | None = None

    def __post_init__(self):
        self.message = common.redact(self.message)
        self.hint = common.redact(self.hint) if self.hint else None


def _http(method: str, url: str, headers: dict[str, str], body: dict | None = None,
          timeout: float = PROBE_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Never follow redirects with credentials attached.
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_tls_context()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


def _tls_context() -> ssl.SSLContext:
    """Verify TLS with certifi's CA bundle when available (the same bundle LiteLLM uses).

    python.org builds on macOS have no system CA store until "Install Certificates.command"
    is run, which made direct Bedrock probes fail with CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi  # noqa: PLC0415 - installed with LiteLLM
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _message_of(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))[:500]
        return str(data.get("message") or data.get("Message") or err or "")[:500]
    return str(data)[:300]


def classify_bedrock_error(status: int, headers: dict[str, str], raw: bytes, model: str, region: str) -> ProbeResult:
    err_type = headers.get("x-amzn-errortype", "").split(":", 1)[0]
    msg = _message_of(raw)
    low = msg.lower()
    if status == 401 or (status == 403 and ("api key" in low or "authentication" in low or "security token" in low
                                            or "signature" in low or "unrecognizedclient" in err_type.lower())):
        return ProbeResult(False, "invalid_token", f"Bedrock rejected the bearer token: {msg}",
                           "Create a new Amazon Bedrock API key (console > Bedrock > API keys) and run "
                           "update-bedrock-key. Short-term keys expire after at most 12 hours.")
    if status == 403 or err_type == "AccessDeniedException":
        return ProbeResult(False, "model_access_denied",
                           f"The token is valid but cannot invoke {model} in {region}: {msg}",
                           "Check the IAM policy attached to the API key's user (bedrock:InvokeModel and "
                           "bedrock:InvokeModelWithResponseStream on the inference profile and its foundation "
                           "model in every destination Region) and any SCPs. For third-party models, make sure "
                           "the account accepted the model's Marketplace terms.")
    if status == 404 or err_type == "ResourceNotFoundException":
        return ProbeResult(False, "model_not_found", f"{model} was not found from {region}: {msg}",
                           "Use an inference profile that AWS lists for this source Region "
                           "(see README 'Changing regions').")
    if status == 400 and ("model identifier is invalid" in low or "invalid model" in low
                          or "on-demand throughput isn" in low or "inference profile" in low):
        return ProbeResult(False, "unsupported_model_or_region", f"{model} cannot be used from {region}: {msg}",
                           "Pick a supported Region/profile pair; Kimi K3 needs an inference profile "
                           "(in./us./global.).")
    if status == 400 and any(word in low for word in ("look-ahead", "look-around", "lookaround")):
        return ProbeResult(False, "structured_output_regex",
                           f"Bedrock rejected a schema regex: {msg}",
                           "Run the gateway with LiteLLM >= 1.106.0 and the bundled kimi_gateway_hooks adapter.")
    if status == 429 or err_type == "ThrottlingException":
        return ProbeResult(False, "throttled", f"Bedrock throttled the request: {msg}",
                           "Wait and retry, or request a quota increase in Service Quotas.")
    if status >= 500:
        return ProbeResult(False, "bedrock_server_error", f"Bedrock returned HTTP {status}: {msg}",
                           "Retry shortly; if it persists, check the AWS Health Dashboard for the Region.")
    return ProbeResult(False, "bedrock_error", f"Bedrock returned HTTP {status} {err_type}: {msg}")


def probe_bedrock(settings: dict[str, Any], token: str, model_id: str | None = None,
                  kind: str = "kimi") -> ProbeResult:
    """Send one minimal inference request straight to Bedrock with ``token``.

    ``kind='kimi'`` uses Converse; ``kind='claude'`` uses InvokeModel with the
    Anthropic Messages body (the path LiteLLM uses for the classifier route).
    """
    common.register_secret(token)
    region = settings["aws_region"]
    model = model_id or settings["bedrock_model_id"]
    base = common.bedrock_runtime_url(settings)
    quoted = urllib.parse.quote(model, safe="")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json"}
    if kind == "claude":
        url = f"{base}/model/{quoted}/invoke"
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 16,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    elif settings.get("kimi_api", "chat_completions") == "chat_completions":
        # The same API the gateway uses for Kimi K3 (reasoning_effort is honoured here).
        url = f"{base}/openai/v1/chat/completions"
        body = {"model": model, "max_tokens": 64, "reasoning_effort": "none",
                "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    else:
        url = f"{base}/model/{quoted}/converse"
        body = {"messages": [{"role": "user", "content": [{"text": "Reply with the single word OK."}]}],
                "inferenceConfig": {"maxTokens": 64},
                "additionalModelRequestFields": {"reasoning_effort": "none"}}
    try:
        status, resp_headers, raw = _http("POST", url, headers, body)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult(False, "network_error", f"Could not reach {base}: {reason}",
                           "Check internet access, proxies (HTTPS_PROXY) and firewall rules for "
                           f"bedrock-runtime.{region}.amazonaws.com.")
    except (TimeoutError, OSError) as exc:
        return ProbeResult(False, "network_error", f"Request to {base} failed: {exc.__class__.__name__}",
                           "Check connectivity and retry.")
    if status == 200:
        return ProbeResult(True, "ok", f"{model} answered from {region}.")
    return classify_bedrock_error(status, resp_headers, raw, model, region)


def probe_anthropic(api_key: str, model: str) -> ProbeResult:
    """Minimal request to the Anthropic API for the optional classifier backend."""
    common.register_secret(api_key)
    try:
        status, _, raw = _http("POST", "https://api.anthropic.com/v1/messages",
                               {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                               {"model": model, "max_tokens": 16,
                                "messages": [{"role": "user", "content": "Reply with OK."}]})
    except (urllib.error.URLError, OSError) as exc:
        return ProbeResult(False, "network_error", f"Could not reach api.anthropic.com: {exc.__class__.__name__}")
    if status == 200:
        return ProbeResult(True, "ok", f"{model} answered on the Anthropic API.")
    code = "invalid_token" if status in (401, 403) else "anthropic_error"
    return ProbeResult(False, code, f"Anthropic API returned HTTP {status}: {_message_of(raw)}")


# ------------------------------------------------------------------ local proxy


def proxy_base_url(settings: dict[str, Any]) -> str:
    host = settings["proxy_host"]
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings['proxy_port']}"


def proxy_health(settings: dict[str, Any], master_key: str | None, timeout: float = 5) -> ProbeResult:
    """Is *our* gateway answering? Liveness alone is not enough: an authenticated
    model listing must succeed with our master key and include our alias."""
    base = proxy_base_url(settings)
    try:
        status, _, _ = _http("GET", f"{base}/health/liveliness", {}, timeout=timeout)
    except (urllib.error.URLError, OSError):
        return ProbeResult(False, "not_running", f"Nothing is answering on {base}.")
    if status != 200:
        return ProbeResult(False, "foreign_listener", f"{base} answered HTTP {status} to a LiteLLM liveness probe; "
                                                      "it does not look like this gateway.")
    if not master_key:
        return ProbeResult(False, "no_master_key", "A LiteLLM proxy is answering, but no master key is stored.")
    common.register_secret(master_key)
    try:
        status, _, raw = _http("GET", f"{base}/v1/models", {"Authorization": f"Bearer {master_key}"},
                               timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        return ProbeResult(False, "proxy_error", f"Model listing failed: {exc.__class__.__name__}")
    if status in (401, 403):
        return ProbeResult(False, "foreign_listener", f"A LiteLLM proxy on {base} rejected this gateway's master "
                                                      "key; it is another proxy instance or an old process.")
    if status != 200:
        return ProbeResult(False, "proxy_error", f"Model listing returned HTTP {status}: {_message_of(raw)}")
    try:
        ids = [m.get("id") for m in json.loads(raw).get("data", [])]
    except (ValueError, AttributeError):
        ids = []
    if settings["model_alias"] not in ids:
        return ProbeResult(False, "alias_missing", f"The proxy is up but does not serve '{settings['model_alias']}' "
                                                   f"(serves: {', '.join(map(str, ids)) or 'nothing'}).",
                           "Restart the proxy so it reloads the current configuration.")
    return ProbeResult(True, "ok", f"Gateway healthy on {base}; serving {', '.join(map(str, ids))}.",
                       detail={"models": ids})


def proxy_messages_test(settings: dict[str, Any], master_key: str, model: str | None = None,
                        classifier: bool = False) -> ProbeResult:
    """One real request through the proxy (costs a few tokens).

    With ``classifier=True`` the request mimics Claude Code's classifier call shape so
    the gateway's classifier-only route accepts it.
    """
    model = model or settings["model_alias"]
    body: dict[str, Any] = {"model": model, "max_tokens": 64,
                            "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    if classifier:
        # Same marker the gateway hook uses to recognise classifier traffic.
        marker = "You are a security monitor for autonomous AI coding agents."
        body.update({"system": marker + " This is a gateway connectivity check; answer <block>no</block>.",
                     "stop_sequences": ["</block>"], "thinking": {"type": "disabled"}, "max_tokens": 16})
    common.register_secret(master_key)
    try:
        status, _, raw = _http("POST", f"{proxy_base_url(settings)}/v1/messages",
                               {"Authorization": f"Bearer {master_key}", "content-type": "application/json",
                                "anthropic-version": "2023-06-01"}, body)
    except (urllib.error.URLError, OSError) as exc:
        return ProbeResult(False, "proxy_unreachable", f"Proxy request failed: {exc.__class__.__name__}")
    if status == 200:
        try:
            data = json.loads(raw)
            served = data.get("model")
            kinds = [b.get("type") for b in data.get("content", [])]
        except (ValueError, AttributeError):
            served, kinds = None, []
        return ProbeResult(True, "ok", f"'{model}' answered through the gateway (content blocks: {kinds or 'none'}).",
                           detail={"served_model": served})
    msg = _message_of(raw)
    code = "proxy_request_failed"
    low = msg.lower()
    hint = "Inspect recent logs with: gateway logs"
    if "authentication failed" in low or "api key is valid" in low:
        code, hint = "invalid_token", "Run update-bedrock-key with a fresh Bedrock API key, then restart the proxy."
    elif "don't have access" in low or "accessdenied" in low:
        code, hint = "model_access_denied", "Grant the API key's IAM user access to the model/profile."
    elif "look-ahead" in low or "look-around" in low:
        code = "structured_output_regex"
        hint = "LiteLLM < 1.106.0 or the adapter is not loaded; see README 'Structured-output regex error'."
    return ProbeResult(False, code, f"'{model}' request through the gateway failed (HTTP {status}): {msg}", hint)


# --------------------------------------------------------------- claude code


def find_claude() -> str | None:
    override = os.environ.get("KGW_CLAUDE_BIN")
    if override:
        return override if shutil.which(override) or os.path.exists(override) else None
    for name in (("claude.exe", "claude.cmd", "claude") if os.name == "nt" else ("claude",)):
        found = shutil.which(name)
        if found:
            return found
    home_local = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe" if os.name == "nt" else "claude")
    return home_local if os.path.exists(home_local) else None


def claude_version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", out.stdout or "")
    return m.group(0) if m else None


def node_version() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15, check=False)
        return (out.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def claude_install_hint(system: str) -> str:
    if system == "Windows":
        return ("Install Claude Code (official native installer, PowerShell):  irm https://claude.ai/install.ps1 | iex\n"
                "  or with Node.js 18+:  npm install -g @anthropic-ai/claude-code\n"
                "  Docs: https://code.claude.com/docs/en/setup")
    return ("Install Claude Code (official native installer):  curl -fsSL https://claude.ai/install.sh | bash\n"
            "  or with Node.js 18+:  npm install -g @anthropic-ai/claude-code\n"
            "  Docs: https://code.claude.com/docs/en/setup")
