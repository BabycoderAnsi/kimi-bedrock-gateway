"""Check that the installed LiteLLM still provides what kimi_gateway_hooks relies on.

Run with a venv's Python (``gateway upgrade`` runs it in the staging venv before switching):

    python gateway/litellm_contract.py --model in.moonshotai.kimi-k3 --api chat_completions

Prints one JSON object: ``{"litellm": "<version>", "checks": [{"name", "level", "ok", "detail"}, ...]}``.
``level`` is ``error`` when a failure breaks or silently degrades the gateway, ``warning`` when the hooks
have a safe local fallback. Several hooks degrade silently on purpose (so a LiteLLM change can't take
the proxy down); this script is where those changes become visible. It makes no network calls.

Behaviour that only shows up on a real request (the Anthropic adapter mapping ``output_config.effort``
to ``reasoning_effort``, prompt-cache markers reaching Bedrock, the hooks actually being called) is
covered by the offline end-to-end tests, which ``gateway upgrade`` also runs.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # as the proxy runs: no price-map download

TOOLS_FLAG = "supports_bedrock_runtime_chat_completions_tools_with_reasoning"


def _check(checks: list[dict], name: str, level: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - every failure is a finding, not a crash
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    checks.append({"name": name, "level": level, "ok": bool(ok), "detail": detail})


def run(model: str, api: str) -> dict:
    import importlib.metadata  # noqa: PLC0415

    checks: list[dict] = []
    version = importlib.metadata.version("litellm")

    def proxy_imports():
        import litellm.proxy.proxy_server  # noqa: F401, PLC0415
        import prisma.errors  # noqa: F401, PLC0415 - LiteLLM's auth-failure path imports it (else 500, not 401)
        return True, "litellm.proxy and prisma.errors import"

    def hook_signatures():
        from litellm.integrations.custom_logger import CustomLogger  # noqa: PLC0415

        want = {"async_pre_call_hook": ["self", "user_api_key_dict", "cache", "data", "call_type"],
                "async_pre_call_deployment_hook": ["self", "kwargs", "call_type"],
                "async_log_success_event": ["self", "kwargs", "response_obj", "start_time", "end_time"],
                "async_log_failure_event": ["self", "kwargs", "response_obj", "start_time", "end_time"]}
        wrong = []
        for method, params in want.items():
            fn = getattr(CustomLogger, method, None)
            got = list(inspect.signature(fn).parameters) if fn else None
            if got != params:
                wrong.append(f"{method}{tuple(got) if got else ' missing'}")
        return not wrong, "CustomLogger hook methods match" if not wrong else "changed: " + ", ".join(wrong)

    def hooks_import():
        import kimi_gateway_hooks as hooks  # noqa: PLC0415

        return isinstance(hooks.proxy_handler_instance, hooks.CustomLogger), "kimi_gateway_hooks loads"

    def upstream_lookaround():
        import kimi_gateway_hooks as hooks  # noqa: PLC0415

        ok = hooks._upstream_drop_lookaround is not None
        return ok, ("LiteLLM's drop_lookaround_regex_patterns is used" if ok else
                    "LiteLLM's drop_lookaround_regex_patterns moved; the hooks use their local copy")

    def lookaround_dropped():
        from litellm.llms.bedrock.common_utils import bedrock_model_supports_regex_lookaround  # noqa: PLC0415

        # LiteLLM can't see the model behind an application-profile ARN; check the Kimi ID it serves.
        target = "moonshotai.kimi-k3" if model.startswith("arn:") else model
        ok = not bedrock_model_supports_regex_lookaround(target)
        return ok, f"look-around regexes are {'dropped' if ok else 'NOT dropped'} for {target} on Converse"

    def tools_stay_on_chat_completions():
        if api != "chat_completions":
            return True, f"not used (kimi_api={api})"
        from litellm.llms.bedrock import common_utils  # noqa: PLC0415

        import kimi_gateway_hooks as hooks  # noqa: PLC0415

        serves = getattr(common_utils, "bedrock_runtime_chat_completions_serves_tools_with_reasoning", None)
        if serves is None:
            return False, ("LiteLLM no longer has bedrock_runtime_chat_completions_serves_tools_with_reasoning; "
                           "tool calls would fall back to Converse, which ignores the reasoning effort")
        os.environ["KGW_KIMI_API"] = "chat_completions"
        hooks.enable_chat_completions_tools(model)
        ok = serves(model)
        return ok, (f"tool calls stay on Chat Completions ({TOOLS_FLAG})" if ok else
                    f"setting {TOOLS_FLAG} no longer keeps tool calls on Chat Completions; they'd fall back "
                    "to Converse, which ignores the reasoning effort (slow responses)")

    _check(checks, "proxy_imports", "error", proxy_imports)
    _check(checks, "hook_signatures", "error", hook_signatures)
    _check(checks, "hooks_import", "error", hooks_import)
    _check(checks, "upstream_lookaround_helper", "warning", upstream_lookaround)
    _check(checks, "lookaround_dropped", "error", lookaround_dropped)
    _check(checks, "tools_on_chat_completions", "error", tools_stay_on_chat_completions)
    return {"litellm": version, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="the configured bedrock_model_id")
    ap.add_argument("--api", default="chat_completions", choices=["chat_completions", "converse"])
    args = ap.parse_args()
    report = run(args.model, args.api)
    print(json.dumps(report))
    return 0 if all(c["ok"] for c in report["checks"] if c["level"] == "error") else 1


if __name__ == "__main__":
    sys.exit(main())
