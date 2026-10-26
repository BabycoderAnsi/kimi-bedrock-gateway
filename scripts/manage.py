#!/usr/bin/env python3
"""Kimi Bedrock Gateway manager - one CLI behind every platform wrapper.

Run ``python scripts/manage.py --help``. The ``bootstrap`` command runs on the system
Python (standard library only); everything else re-executes inside the project's
virtual environment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import validate  # noqa: E402
from common import GatewayError  # noqa: E402

PREVIOUS_SUFFIX = ".previous"
CONFLICTING_CLAUDE_ENV = (
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_SMALL_FAST_MODEL",
    # never hand the gateway's upstream secrets to Claude Code
    "AWS_BEARER_TOKEN_BEDROCK", "LITELLM_MASTER_KEY", "KGW_CLASSIFIER_BEDROCK_TOKEN", "KGW_CLASSIFIER_ANTHROPIC_KEY",
)
STARTUP_TIMEOUT = 120


# ----------------------------------------------------------------------- output


def info(msg: str = "") -> None:
    print(common.redact(msg), flush=True)


def ok(msg: str) -> None:
    info(f"[ OK ] {msg}")


def warn(msg: str) -> None:
    info(f"[WARN] {msg}")


def fail(msg: str) -> None:
    info(f"[FAIL] {msg}")


def step(msg: str) -> None:
    info(f"\n==> {msg}")


def interactive(args: argparse.Namespace) -> bool:
    return not getattr(args, "non_interactive", False) and sys.stdin.isatty()


# ------------------------------------------------------------------- bootstrap


def _marker(venv: Path) -> Path:
    return venv / ".kgw-requirements"


def venv_healthy(venv: Path, settings: dict[str, Any]) -> tuple[bool, str]:
    py = common.venv_python(venv)
    if not py.exists():
        return False, "missing"
    try:
        out = subprocess.run([str(py), "-c", "import sys, litellm, keyring, psutil, yaml; "
                                            "print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError):
        return False, "broken"
    if out.returncode != 0:
        return False, "incomplete"
    expected = (_marker(venv).read_text().strip() if _marker(venv).exists() else "")
    if expected != requirements_fingerprint_for(py, settings):
        return False, "outdated"
    return True, out.stdout.strip()


def requirements_fingerprint_for(py: Path, settings: dict[str, Any]) -> str:
    try:
        ver = subprocess.run([str(py), "-c", "import sys; print(sys.version.split()[0])"], capture_output=True,
                             text=True, timeout=60, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ver = "?"
    h = hashlib.sha256((common.REPO_ROOT / "requirements.txt").read_bytes())
    h.update((settings.get("litellm_version") or common.PINNED_LITELLM).encode())
    h.update(ver.encode())
    return h.hexdigest()


def install_requirements(venv: Path, settings: dict[str, Any], python_for_venv: str | None = None) -> None:
    py = common.venv_python(venv)
    if not py.exists():
        step(f"Creating virtual environment in {venv}")
        base = python_for_venv or sys.executable
        result = subprocess.run([base, "-m", "venv", str(venv)], check=False)
        if result.returncode != 0:
            hint = ("On Ubuntu/Debian install the venv module first, e.g.:\n"
                    "    sudo apt install python3-venv   (or python3.12-venv for your exact version)"
                    if sys.platform.startswith("linux") else "Reinstall Python with the venv/ensurepip component.")
            raise GatewayError("Could not create the virtual environment.", hint=hint, code="venv_failed")
    step("Installing pinned dependencies (this can take a few minutes the first time)")
    pip = [str(py), "-m", "pip", "--disable-pip-version-check"]
    subprocess.run(pip + ["install", "--upgrade", "pip"], check=False, stdout=subprocess.DEVNULL)
    result = subprocess.run(pip + ["install", "-r", str(common.REPO_ROOT / "requirements.txt")], check=False)
    if result.returncode != 0:
        raise GatewayError("Dependency installation failed (see pip output above).",
                           hint="Check network access to PyPI, then re-run setup; it resumes where it stopped.",
                           code="pip_failed")
    version = settings.get("litellm_version") or common.PINNED_LITELLM
    if version != common.PINNED_LITELLM:
        warn(f"Installing LiteLLM {version} (settings override; tested version is {common.PINNED_LITELLM}).")
        result = subprocess.run(pip + ["install", f"litellm[proxy]=={version}"], check=False)
        if result.returncode != 0:
            raise GatewayError(f"Could not install LiteLLM {version}.", code="pip_failed")
    _marker(venv).write_text(requirements_fingerprint_for(py, settings) + "\n")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Runs on the system Python: check platform, prepare the venv, hand over to setup."""
    plat = common.detect_platform()
    step("Checking platform")
    info(f"    OS / arch      : {plat.label}")
    info(f"    Python         : {sys.version.split()[0]} ({plat.python_machine}) at {sys.executable}")
    if not common.python_supported():
        supported = ", ".join(f"{a}.{b}" for a, b in common.SUPPORTED_PYTHON)
        raise GatewayError(f"Python {sys.version.split()[0]} is not supported (supported: {supported}).",
                           hint=_python_install_hint(plat.system), code="unsupported_python")
    if plat.is_translated and not (args.allow_rosetta or "--allow-rosetta" in args.setup_args):
        raise GatewayError("This Python runs under Rosetta 2 on Apple Silicon.",
                           hint=plat.notes[0] + " Or pass --allow-rosetta to continue anyway.",
                           code="rosetta_python")
    for note in plat.notes:
        warn(note)
    settings = common.load_settings()
    venv = common.venv_dir()
    healthy, why = venv_healthy(venv, settings)
    if healthy:
        ok(f"Reusing healthy virtual environment {venv} (Python {why})")
    else:
        if why in ("broken", "incomplete") and venv.exists():
            warn(f"Virtual environment at {venv} is {why}; repairing it.")
        install_requirements(venv, settings)
        healthy, why = venv_healthy(venv, settings)
        if not healthy:
            raise GatewayError(f"Virtual environment is still {why} after installation.", code="venv_failed")
        ok("Dependencies installed")
    py = str(common.venv_python(venv))
    forward = [py, str(Path(__file__).resolve()), "setup", *args.setup_args]
    if os.name == "nt":
        return subprocess.call(forward)
    os.execv(py, forward)
    return 0  # pragma: no cover


def _python_install_hint(system: str) -> str:
    if system == "Windows":
        return "Install Python 3.12 or 3.13 from https://www.python.org/downloads/windows/ or: winget install Python.Python.3.12"
    if system == "Darwin":
        return ("Install a native arm64 Python 3.12/3.13: https://www.python.org/downloads/macos/ (universal2) "
                "or: brew install python@3.12 (Homebrew in /opt/homebrew)")
    return "Ubuntu: sudo apt install python3 python3-venv  (Ubuntu 24.04 ships 3.12)"


# --------------------------------------------------------------------- helpers


def load() -> tuple[dict[str, Any], common.SecretStore]:
    settings = common.load_settings()
    store = common.store_for(settings)
    return settings, store


def get_secret(store: common.SecretStore, name: str) -> str | None:
    value = store.get(name)
    common.register_secret(value)
    return value


def read_access_keys(store: common.SecretStore) -> common.AwsKeys | None:
    key_id = get_secret(store, common.SECRET_AWS_ACCESS_KEY_ID)
    secret = get_secret(store, common.SECRET_AWS_SECRET_ACCESS_KEY)
    return common.AwsKeys(key_id, secret) if key_id and secret else None


def bedrock_credential(settings: dict[str, Any], store: common.SecretStore) -> str | common.AwsKeys | None:
    """The credential the gateway uses for Bedrock: bearer token or access key pair (per aws_auth)."""
    if settings.get("aws_auth") == "access_key":
        return read_access_keys(store)
    return get_secret(store, common.SECRET_BEDROCK_TOKEN)


def describe_access_keys(keys: common.AwsKeys) -> str:
    return f"access key ID ending {keys.access_key_id[-4:]}, secret fingerprint {common.fingerprint(keys.secret_access_key)}"


def backup_settings(reason: str) -> Path | None:
    p = common.paths()
    if not p.settings.exists():
        return None
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = common.ensure_private_dir(p.backups / f"{stamp}-{reason}")
    for src in (p.settings, p.runtime_config):
        if src.exists():
            shutil.copy2(src, dest / src.name)
            common.restrict_permissions(dest / src.name)
    return dest


def save_progress(stage: str) -> None:
    p = common.paths()
    common.write_private_text(p.setup_progress, json.dumps({"last_completed": stage,
                                                            "at": _dt.datetime.now().isoformat()}) + "\n")


def proxy_state(settings: dict[str, Any]) -> dict[str, Any]:
    """Who, if anyone, is serving the configured port?"""
    p = common.paths()
    rec = common.read_pid(p)
    ours = bool(rec and common.process_is_our_proxy(rec["pid"], p))
    if rec and not ours:
        common.clear_pid(p)  # stale PID file
        rec = None
    busy = common.port_in_use(settings["proxy_host"], settings["proxy_port"])
    return {"ours": ours, "pid": rec["pid"] if rec else None, "mode": rec.get("mode") if rec else None,
            "port_busy": busy, "foreign": busy and not ours}


def build_proxy_env(settings: dict[str, Any], store: common.SecretStore) -> dict[str, str]:
    cred = bedrock_credential(settings, store)
    master = get_secret(store, common.SECRET_MASTER_KEY)
    if not cred:
        if settings.get("aws_auth") == "access_key":
            raise GatewayError("No AWS access key pair is stored.", hint="Run: gateway set-auth access-key",
                               code="missing_token")
        raise GatewayError("No Bedrock bearer token is stored.", hint="Run update-bedrock-key (or setup).",
                           code="missing_token")
    if not master:
        raise GatewayError("No local proxy master key is stored.", hint="Run setup.", code="missing_master_key")
    env = dict(os.environ)
    for k in ("DATABASE_URL", "LITELLM_MASTER_KEY", "AWS_BEARER_TOKEN_BEDROCK", "KGW_CLASSIFIER_ALIAS",
              "STORE_MODEL_IN_DB", "KGW_AWS_ACCESS_KEY_ID", "KGW_AWS_SECRET_ACCESS_KEY"):
        env.pop(k, None)
    if isinstance(cred, common.AwsKeys):
        env["KGW_AWS_ACCESS_KEY_ID"] = cred.access_key_id
        env["KGW_AWS_SECRET_ACCESS_KEY"] = cred.secret_access_key
    else:
        env["AWS_BEARER_TOKEN_BEDROCK"] = cred
    env.update({
        "LITELLM_MASTER_KEY": master,
        "KGW_PRIMARY_ALIAS": settings["model_alias"],
        "KGW_KIMI_MODEL_ID": settings["bedrock_model_id"],
        "KGW_KIMI_API": settings["kimi_api"],
        "KGW_KIMI_EFFORT": settings["kimi_reasoning_effort"],
        "KGW_KIMI_PROMPT_CACHE": "1" if settings.get("kimi_prompt_cache", True) else "0",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(common.REPO_ROOT / "gateway"), env.get("PYTHONPATH")])),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # pinned model capabilities, no runtime download
        "DISABLE_ADMIN_UI": "True",
        "LITELLM_LOG": env.get("KGW_LITELLM_LOG", "INFO"),
        "PYTHONUNBUFFERED": "1",
    })
    c = settings["classifier"]
    if c["backend"] != "none":
        env["KGW_CLASSIFIER_ALIAS"] = c["request_model"]
        if c["backend"] == "bedrock" and c["credential"] == "separate":
            sep = get_secret(store, common.SECRET_CLASSIFIER_BEDROCK_TOKEN)
            if not sep:
                raise GatewayError("The classifier is set to use a separate Bedrock token, but none is stored.",
                                   hint="Run: classifier enable --separate-credential", code="missing_token")
            env["KGW_CLASSIFIER_BEDROCK_TOKEN"] = sep
        if c["backend"] == "anthropic":
            key = get_secret(store, common.SECRET_CLASSIFIER_ANTHROPIC_KEY)
            if not key:
                raise GatewayError("The Anthropic classifier backend needs an API key.",
                                   hint="Run: classifier enable --backend anthropic", code="missing_token")
            env["KGW_CLASSIFIER_ANTHROPIC_KEY"] = key
    return env


def rotate_log(log: Path, limit: int = 5 * 1024 * 1024) -> None:
    if log.exists() and log.stat().st_size > limit:
        older = log.with_suffix(".log.1")
        older.unlink(missing_ok=True)
        log.rename(older)


def tail_log(n: int = 40) -> list[str]:
    log = common.paths().proxy_log
    if not log.exists():
        return []
    with open(log, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 256 * 1024))
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    return [common.redact(line) for line in lines[-n:]]


def _litellm_cmd(settings: dict[str, Any]) -> list[str]:
    exe = common.venv_litellm()
    if not exe.exists():
        raise GatewayError(f"LiteLLM is not installed in {common.venv_dir()}.", hint="Run setup.",
                           code="missing_dependency")
    return [str(exe), "--config", str(common.paths().runtime_config), "--host", settings["proxy_host"],
            "--port", str(settings["proxy_port"])]


def start_proxy(settings: dict[str, Any], store: common.SecretStore, background: bool) -> int:
    for issue in common.require_valid(settings):
        warn(issue.message)
    state = proxy_state(settings)
    if state["ours"]:
        ok(f"The gateway is already running (PID {state['pid']}, {state['mode']}).")
        return 0
    if state["foreign"]:
        raise GatewayError(
            f"Port {settings['proxy_port']} on {settings['proxy_host']} is already in use by another process.",
            hint="Stop that process, or choose another port: set proxy_port in settings.json "
                 f"({common.paths().settings}) and re-run.", code="port_in_use")
    common.write_runtime_config(settings)
    env = build_proxy_env(settings, store)
    cmd = _litellm_cmd(settings)
    p = common.paths()
    if not background:
        info(f"Starting LiteLLM proxy in the foreground on {validate.proxy_base_url(settings)} "
             f"(model alias '{settings['model_alias']}' -> {settings['bedrock_model_id']} in {settings['aws_region']}).")
        info("Press Ctrl+C to stop.\n")
        proc = subprocess.Popen(cmd, env=env)
        common.write_pid(proc.pid, settings, "foreground", p)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT) if os.name != "nt" else proc.terminate()
            try:
                return proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                return 130
        finally:
            common.clear_pid(p)
    common.ensure_private_dir(p.log_dir)
    rotate_log(p.proxy_log)
    logf = open(p.proxy_log, "ab")  # noqa: SIM115 - handed to the child
    common.restrict_permissions(p.proxy_log)
    kwargs: dict[str, Any] = {"stdout": logf, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    logf.close()
    common.write_pid(proc.pid, settings, "background", p)
    master = env["LITELLM_MASTER_KEY"]
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            common.clear_pid(p)
            fail("The proxy exited during startup. Last log lines:")
            for line in tail_log(25):
                info("    " + line)
            raise GatewayError("Proxy failed to start.", hint="Fix the error above, then start again.",
                               code="start_failed")
        if validate.proxy_health(settings, master, timeout=2).ok:
            ok(f"Gateway running in the background (PID {proc.pid}) on {validate.proxy_base_url(settings)}. "
               f"Logs: {p.proxy_log}")
            return 0
        time.sleep(1)
    warn("The proxy did not become healthy within the timeout; it is still starting or stuck. Check: logs")
    return 1


def stop_proxy(settings: dict[str, Any], quiet: bool = False) -> bool:
    p = common.paths()
    state = proxy_state(settings)
    if not state["ours"]:
        if not quiet:
            if state["foreign"]:
                warn(f"Port {settings['proxy_port']} is used by a process that is not this gateway; leaving it alone.")
            else:
                info("The gateway is not running.")
        return False
    import psutil  # noqa: PLC0415

    proc = psutil.Process(state["pid"])
    children = proc.children(recursive=True)
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    except psutil.TimeoutExpired:
        warn("Proxy did not exit after SIGTERM; killing it.")
        proc.kill()
    except psutil.NoSuchProcess:
        pass
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    common.clear_pid(p)
    if not quiet:
        ok(f"Gateway stopped (PID {state['pid']}).")
    return True


def offer_restart(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore,
                  why: str) -> None:
    state = proxy_state(settings)
    if not state["ours"]:
        info(f"The gateway is not running; the change ({why}) applies at the next start.")
        return
    want = getattr(args, "restart", False) or (interactive(args) and common.confirm(
        f"The running gateway still uses the old {why}. Restart it now?", default=True))
    if not want:
        warn(f"The running gateway keeps the old {why} until you restart it (gateway restart).")
        return
    mode = state["mode"]
    stop_proxy(settings, quiet=True)
    if mode == "foreground":
        info("The gateway was running in the foreground; start it again with ./start.sh (or .\\start.ps1).")
        return
    start_proxy(settings, store, background=True)


# ------------------------------------------------------------------------ setup


def configure_settings(args: argparse.Namespace, settings: dict[str, Any], existed: bool) -> dict[str, Any]:
    if existed:
        info("Existing configuration found:")
        print_settings(settings)
        action = "keep"
        if args.reset:
            action = "reset"
        elif args.region or args.model_id or args.endpoint_url or args.credential_backend:
            action = "update"
        elif interactive(args):
            action = common.choose("Keep, update, or reset the configuration?", ["keep", "update", "reset"], "keep")
        if action == "keep":
            return settings
        if action == "reset":
            dest = backup_settings("reset")
            info(f"Backed up the previous settings to {dest}")
            kept_backend = settings.get("credential_backend", "keyring")
            settings = common.default_settings()
            settings["credential_backend"] = kept_backend
    if args.credential_backend:
        settings["credential_backend"] = args.credential_backend
    if not args.region and common.is_application_profile(args.model_id):
        args.region = common.APP_PROFILE_RE.match(args.model_id).group(1)
    region = args.region or (common.ask("AWS Region for Bedrock", settings["aws_region"])
                             if interactive(args) else settings["aws_region"])
    settings["aws_region"] = region.strip()
    model = args.model_id or settings["bedrock_model_id"]
    allowed = common.KNOWN_KIMI_PROFILES.get(model)
    if allowed is not None and region not in allowed:
        suggestion = common.suggest_kimi_profile(region)
        msg = f"{model} cannot be called from {region}."
        if suggestion and interactive(args) and common.confirm(f"{msg} Use {suggestion} instead?", default=True):
            model = suggestion
        elif not args.model_id:
            raise GatewayError(msg, hint=f"Re-run with --model-id {suggestion}" if suggestion else None,
                               code="unsupported_model_or_region")
    elif interactive(args) and not args.model_id:
        model = common.ask("Bedrock model / inference profile ID", model)
    common.set_kimi_model(settings, model)
    if args.endpoint_url is not None:
        settings["bedrock_endpoint_url"] = args.endpoint_url or None
    if getattr(args, "auth", None):
        settings["aws_auth"] = AUTH_CHOICES[args.auth]
    return settings


def ensure_backend_usable(settings: dict[str, Any], args: argparse.Namespace) -> None:
    if settings["credential_backend"] != "keyring":
        warn(f"Credentials are stored in a user-only (0600) file: {common.paths().file_secrets}. "
             "This is less protected than the OS credential store; see README 'Credential storage'.")
        return
    usable, name = common.SecretStore.keyring_usable()
    if usable:
        ok(f"Credential store: {name}")
        return
    hint = ("No usable OS credential store was found. On Ubuntu desktop install/unlock GNOME Keyring "
            "(sudo apt install gnome-keyring libsecret-1-0) and make sure a D-Bus session is running; on a headless "
            "server re-run setup with --credential-backend file to use a 0600 file instead.")
    raise GatewayError(f"The system keyring backend '{name}' is not usable.", hint=hint, code="no_keyring")


AUTH_CHOICES = {"bearer": "bearer", "access-key": "access_key"}


def obtain_access_keys_interactively(args: argparse.Namespace, settings: dict[str, Any],
                                     store: common.SecretStore) -> None:
    existing = read_access_keys(store)
    if existing and not args.replace_token:
        ok(f"An AWS access key pair is already stored ({describe_access_keys(existing)}); keeping it.")
        if interactive(args) and common.confirm("Replace the stored access key pair?", default=False):
            update_access_keys(args, settings, store, confirm_replace=False)
        return
    if not existing and not interactive(args) and not args.token_stdin:
        raise GatewayError("No AWS access key pair stored and no way to ask for one.",
                           hint="Re-run interactively, or pass --token-stdin and pipe the key ID and secret "
                                "in on two lines.", code="missing_token")
    update_access_keys(args, settings, store, confirm_replace=False)


def obtain_token_interactively(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore) -> None:
    existing = get_secret(store, common.SECRET_BEDROCK_TOKEN)
    if existing and not args.replace_token:
        ok(f"A Bedrock bearer token is already stored (fingerprint {common.fingerprint(existing)}); keeping it.")
        if interactive(args) and common.confirm("Replace the stored Bedrock token?", default=False):
            update_token(args, settings, store, confirm_replace=False)
        return
    if not existing and not interactive(args) and not args.token_stdin:
        raise GatewayError("No Bedrock token stored and no way to ask for one.",
                           hint="Re-run interactively, or pass --token-stdin and pipe the token in.",
                           code="missing_token")
    update_token(args, settings, store, confirm_replace=False)


def cmd_setup(args: argparse.Namespace) -> int:
    p = common.paths()
    if p.setup_progress.exists():
        try:
            last = json.loads(p.setup_progress.read_text()).get("last_completed")
            info(f"Resuming setup (the previous run completed: {last}). Every step is safe to repeat.")
        except ValueError:
            pass
    common.ensure_private_dir(p.root)
    plat = common.detect_platform()
    existed = common.settings_exist(p)
    settings = common.load_settings(p)

    step("Configuration")
    settings = configure_settings(args, settings, existed)
    for issue in common.require_valid(settings):
        warn(issue.message)
    common.save_settings(settings, p)
    ok(f"Settings saved to {p.settings}")
    save_progress("settings")

    step("Credentials")
    ensure_backend_usable(settings, args)
    store = common.store_for(settings, p)
    master = get_secret(store, common.SECRET_MASTER_KEY)
    if master:
        ok(f"Local proxy master key already exists (fingerprint {common.fingerprint(master)}); keeping it.")
    else:
        master = common.generate_master_key()
        store.set(common.SECRET_MASTER_KEY, master)
        ok(f"Generated a new local proxy master key (fingerprint {common.fingerprint(master)}).")
    if settings["aws_auth"] == "access_key":
        obtain_access_keys_interactively(args, settings, store)
    else:
        obtain_token_interactively(args, settings, store)
    token = get_secret(store, common.SECRET_BEDROCK_TOKEN)
    if token and token == master:
        raise GatewayError("The Bedrock token and the proxy master key must differ.", code="key_reuse")
    save_progress("credentials")

    if args.classifier:
        step("Auto-mode classifier")
        if args.classifier == "none":
            settings["classifier"]["backend"] = "none"
            common.save_settings(settings, p)
        else:
            try:
                enable_classifier(argparse.Namespace(
                    backend=args.classifier, model_id=None, separate_credential=args.separate_credential,
                    no_validate=args.skip_live, replace_token=False), settings, store)
            except GatewayError as exc:
                # Not fatal: the gateway works without it; auto mode just stays disabled.
                fail(str(exc))
                if exc.hint:
                    info("    " + exc.hint)
                settings = common.load_settings(p)
                settings["classifier"]["backend"] = "none"
                common.save_settings(settings, p)
                warn("Continuing without an auto-mode classifier (retry later: gateway classifier enable).")

    step("LiteLLM configuration")
    path = common.write_runtime_config(settings, p)
    ok(f"Rendered {path} (contains no secrets)")
    save_progress("runtime_config")

    step("Gateway connectivity test")
    if args.skip_live:
        warn("Skipped live test (--skip-live). Bedrock access has NOT been verified end to end.")
    else:
        live_ok = run_gateway_selftest(args, settings, store)
        if not live_ok:
            fail("The gateway connectivity test failed. Fix the problem above and re-run setup.")
            return 1
    save_progress("selftest")

    step("Claude Code")
    report_claude(plat)
    p.setup_progress.unlink(missing_ok=True)
    info("\nSetup complete. Next steps:")
    wrapper = ".\\" if os.name == "nt" else "./"
    ext = ".ps1" if os.name == "nt" else ".sh"
    info(f"  1. Start the gateway:        {wrapper}start{ext}            (foreground; Ctrl+C stops it)")
    info(f"  2. In another terminal:      {wrapper}claude-kimi{ext}       (Claude Code through the gateway)")
    info(f"  Status / logs / doctor:      {wrapper}gateway{ext} status | logs | doctor")
    if interactive(args) and validate.find_claude() and proxy_state(settings)["ours"] and common.confirm(
            "Launch Claude Code through the gateway now?", default=False):
        return cmd_launch_claude(argparse.Namespace(claude_args=[], non_interactive=False,
                                                    skip_classifier_check=False))
    return 0


def run_gateway_selftest(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore) -> bool:
    """Start the proxy if needed, check health, send one real request, stop if we started it."""
    state = proxy_state(settings)
    if state["foreign"]:
        fail(f"Port {settings['proxy_port']} is used by another process; cannot run the gateway test.")
        info("    Choose another port (proxy_port in settings.json) or stop that process, then re-run setup.")
        return False
    if interactive(args) and not args.yes and not common.confirm(
            "Run a live test now? It sends two tiny requests to Bedrock (a few tokens, billed to your account).",
            default=True):
        warn("Live test skipped by user. Bedrock access has NOT been verified.")
        return True
    started = False
    if not state["ours"]:
        if start_proxy(settings, store, background=True) != 0:
            return False
        started = True
    try:
        master = get_secret(store, common.SECRET_MASTER_KEY)
        health = validate.proxy_health(settings, master)
        (ok if health.ok else fail)(health.message)
        if not health.ok:
            return False
        result = validate.proxy_messages_test(settings, master)
        (ok if result.ok else fail)(result.message)
        if result.hint and not result.ok:
            info("    " + result.hint)
        if result.ok and settings["classifier"]["backend"] != "none":
            cres = validate.proxy_messages_test(settings, master, settings["classifier"]["request_model"],
                                                classifier=True)
            (ok if cres.ok else fail)("Classifier route: " + cres.message)
        return result.ok
    finally:
        if started:
            stop_proxy(settings, quiet=True)


def report_claude(plat: common.PlatformInfo) -> None:
    claude = validate.find_claude()
    node = validate.node_version()
    info(f"    Node.js        : {node or 'not found (not needed for the native Claude Code installer)'}")
    if not claude:
        warn("Claude Code is not installed (or not on PATH).")
        info("    " + validate.claude_install_hint(plat.system).replace("\n", "\n    "))
        return
    version = validate.claude_version(claude)
    parsed = common.parse_version(version or "")
    if parsed and parsed < common.MIN_CLAUDE_CODE:
        warn(f"Claude Code {version} at {claude} is older than the minimum supported "
             f"{'.'.join(map(str, common.MIN_CLAUDE_CODE))}; run: claude update")
    else:
        ok(f"Claude Code {version or '(unknown version)'} at {claude} (tested: {common.TESTED_CLAUDE_CODE})")


# ---------------------------------------------------------------- credentials


def update_token(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore,
                 confirm_replace: bool = True) -> bool:
    existing = get_secret(store, common.SECRET_BEDROCK_TOKEN)
    new = common.read_secret("New Amazon Bedrock API key (bearer token)")
    if existing and new == existing:
        ok("That token is already the stored one; nothing changed.")
        return False
    if new == get_secret(store, common.SECRET_MASTER_KEY):
        raise GatewayError("That value is the local proxy master key, not a Bedrock token.", code="key_reuse")
    if existing and confirm_replace and not getattr(args, "yes", False):
        if not interactive(args):
            raise GatewayError("A Bedrock token is already stored; pass --yes to replace it non-interactively.",
                               code="confirmation_required")
        if not common.confirm(f"Replace the stored token (fingerprint {common.fingerprint(existing)}) "
                              f"with the new one (fingerprint {common.fingerprint(new)})?", default=False):
            info("Kept the existing token.")
            return False
    if getattr(args, "no_validate", False):
        warn("Saving without validation (--no-validate).")
    else:
        info(f"Validating the new token against {settings['bedrock_model_id']} in {settings['aws_region']} ...")
        result = validate.probe_bedrock(settings, new)
        if result.ok:
            ok(result.message)
        else:
            fail(result.message)
            if result.hint:
                info("    " + result.hint)
            save_anyway = getattr(args, "save_unvalidated", False) or (
                interactive(args) and result.code in ("network_error", "throttled", "bedrock_server_error")
                and common.confirm("Validation could not complete for a transient reason. Save the token anyway?",
                                   default=False))
            if not save_anyway:
                info("The previously stored token (if any) was left unchanged.")
                raise GatewayError("The new token was not saved because validation failed.", code=result.code)
            warn("Saving the token although validation failed (explicitly requested).")
    if existing:
        store.set(common.SECRET_BEDROCK_TOKEN + PREVIOUS_SUFFIX, existing)
    store.set(common.SECRET_BEDROCK_TOKEN, new)
    ok(f"Bedrock token saved to the {settings['credential_backend']} store (fingerprint {common.fingerprint(new)}).")
    if existing:
        info("    The previous token is kept for rollback: update-bedrock-key --rollback "
             "(remove it with --forget-previous).")
    return True


def update_access_keys(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore,
                       confirm_replace: bool = True) -> bool:
    """Prompt for an IAM access key pair, validate it with a SigV4-signed request, then store it."""
    key_id = common.read_secret("AWS access key ID")
    if not common.ACCESS_KEY_ID_RE.match(key_id):
        raise GatewayError("That is not an IAM user access key ID (AKIA followed by 16 letters/digits).",
                           hint="Temporary credentials (ASIA..., which need a session token) are not supported.",
                           code="invalid_input")
    secret = common.read_secret("AWS secret access key")
    if not common.SECRET_ACCESS_KEY_RE.match(secret):
        raise GatewayError("That is not an AWS secret access key (40 characters of letters, digits, / + =).",
                           code="invalid_input")
    new = common.AwsKeys(key_id, secret)
    existing = read_access_keys(store)
    if existing == new:
        ok("That access key pair is already the stored one; nothing changed.")
        return False
    if existing and confirm_replace and not getattr(args, "yes", False):
        if not interactive(args):
            raise GatewayError("An access key pair is already stored; pass --yes to replace it non-interactively.",
                               code="confirmation_required")
        if not common.confirm(f"Replace the stored access key pair ({describe_access_keys(existing)}) "
                              f"with the new one ({describe_access_keys(new)})?", default=False):
            info("Kept the existing access key pair.")
            return False
    if getattr(args, "no_validate", False):
        warn("Saving without validation (--no-validate).")
    else:
        info(f"Validating the access key pair against {settings['bedrock_model_id']} in {settings['aws_region']} ...")
        result = validate.probe_bedrock(settings, new)
        if result.ok:
            ok(result.message)
        else:
            fail(result.message)
            if result.hint:
                info("    " + result.hint)
            if not getattr(args, "save_unvalidated", False):
                info("The previously stored access key pair (if any) was left unchanged.")
                raise GatewayError("The access key pair was not saved because validation failed.", code=result.code)
            warn("Saving the access key pair although validation failed (explicitly requested).")
    store.set(common.SECRET_AWS_ACCESS_KEY_ID, key_id)
    store.set(common.SECRET_AWS_SECRET_ACCESS_KEY, secret)
    ok(f"AWS access key pair saved to the {settings['credential_backend']} store ({describe_access_keys(new)}).")
    return True


def cmd_set_auth(args: argparse.Namespace) -> int:
    settings, store = load()
    common.require_valid(settings)
    mode = AUTH_CHOICES[args.mode]
    trial = dict(settings, aws_auth=mode)
    changed_credential = False
    if mode == "access_key":
        if args.replace or not read_access_keys(store):
            changed_credential = update_access_keys(args, trial, store, confirm_replace=True)
        else:
            ok(f"Using the stored AWS access key pair ({describe_access_keys(read_access_keys(store))}); "
               "pass --replace to enter a new one.")
        if not read_access_keys(store):
            return 1
    else:
        if not get_secret(store, common.SECRET_BEDROCK_TOKEN):
            changed_credential = update_token(args, trial, store, confirm_replace=False)
            if not get_secret(store, common.SECRET_BEDROCK_TOKEN):
                return 1
        if args.forget_access_key:
            removed = [store.delete(n) for n in (common.SECRET_AWS_ACCESS_KEY_ID, common.SECRET_AWS_SECRET_ACCESS_KEY)]
            (ok if any(removed) else info)("Removed the stored AWS access key pair." if any(removed)
                                           else "No AWS access key pair was stored.")
    if settings["aws_auth"] == mode:
        ok(f"Bedrock authentication is already '{args.mode}'.")
        if changed_credential:
            offer_restart(args, settings, store, "Bedrock credentials")
        return 0
    backup_settings("set-auth")
    common.save_settings(trial, common.paths())
    common.write_runtime_config(trial)
    ok(f"Bedrock authentication set to '{args.mode}'.")
    offer_restart(args, trial, store, "Bedrock authentication")
    return 0


def cmd_update_key(args: argparse.Namespace) -> int:
    settings, store = load()
    common.require_valid(settings)
    name, prev = common.SECRET_BEDROCK_TOKEN, common.SECRET_BEDROCK_TOKEN + PREVIOUS_SUFFIX
    if args.forget_previous:
        removed = store.delete(prev)
        (ok if removed else info)("Removed the previous token." if removed else "No previous token was stored.")
        return 0
    if args.rollback:
        old = get_secret(store, prev)
        if not old:
            raise GatewayError("No previous token is stored; nothing to roll back to.", code="nothing_to_rollback")
        current = get_secret(store, name)
        if not args.yes and not (interactive(args) and common.confirm(
                f"Restore the previous token (fingerprint {common.fingerprint(old)})?", default=False)):
            info("Rollback cancelled.")
            return 1
        store.set(name, old)
        if current:
            store.set(prev, current)
        ok("Restored the previous Bedrock token.")
        offer_restart(args, settings, store, "Bedrock token")
        return 0
    if settings.get("aws_auth") == "access_key":
        warn("The gateway currently authenticates with an AWS access key pair, so it does not use this token. "
             "Switch with: gateway set-auth bearer")
    if update_token(args, settings, store, confirm_replace=True):
        offer_restart(args, settings, store, "Bedrock token")
    return 0


def cmd_rotate_master_key(args: argparse.Namespace) -> int:
    settings, store = load()
    if not args.yes and not (interactive(args) and common.confirm(
            "Generate a new local proxy master key? Running Claude Code sessions must be relaunched.", default=False)):
        return 1
    key = common.generate_master_key()
    store.set(common.SECRET_MASTER_KEY, key)
    ok(f"New master key stored (fingerprint {common.fingerprint(key)}).")
    offer_restart(args, settings, store, "master key")
    return 0


def cmd_set_region(args: argparse.Namespace) -> int:
    settings, store = load()
    new = dict(settings)
    new["aws_region"] = args.region
    model = args.model_id or settings["bedrock_model_id"]
    allowed = common.KNOWN_KIMI_PROFILES.get(model)
    if allowed is not None and args.region not in allowed and not args.model_id:
        suggestion = common.suggest_kimi_profile(args.region)
        if not suggestion:
            raise GatewayError(f"No known Kimi K3 inference profile can be called from {args.region}.",
                               code="unsupported_model_or_region")
        if not (args.yes or (interactive(args) and common.confirm(
                f"{model} cannot be called from {args.region}. Switch the model to {suggestion}?", default=True))):
            raise GatewayError("Region not changed.", hint=f"Re-run with --model-id {suggestion}.",
                               code="unsupported_model_or_region")
        model = suggestion
    common.set_kimi_model(new, model)
    common.require_valid(new)
    if not args.no_validate:
        token = bedrock_credential(new, store)
        if token:
            result = validate.probe_bedrock(new, token)
            (ok if result.ok else fail)(result.message)
            if not result.ok:
                if result.hint:
                    info("    " + result.hint)
                if not (interactive(args) and common.confirm("Save the new Region anyway?", default=False)):
                    info("Settings unchanged.")
                    return 1
    backup_settings("set-region")
    common.save_settings(new, common.paths())
    common.write_runtime_config(new)
    ok(f"Region set to {args.region} with model {model}.")
    offer_restart(args, new, store, "Region/model configuration")
    return 0


# ------------------------------------------------------------------ classifier


def enable_classifier(args: argparse.Namespace, settings: dict[str, Any], store: common.SecretStore) -> None:
    c = settings["classifier"]
    backend = getattr(args, "backend", None) or "bedrock"
    c["backend"] = backend
    if backend == "bedrock":
        c["bedrock_model_id"] = getattr(args, "model_id", None) or c.get("bedrock_model_id") or \
            common.default_classifier_model_for_region(settings["aws_region"])
        if getattr(args, "base_model", None):
            c["base_model"] = args.base_model
        c["credential"] = "separate" if getattr(args, "separate_credential", False) else "shared"
        for issue in common.validate_settings(settings):
            if issue.level == "error" and "classifier" in issue.message.lower():
                raise GatewayError(issue.message, hint=issue.hint, code="invalid_settings")
        if c["credential"] == "separate":
            value = common.read_secret("Bedrock API key for the classifier model")
            store.set(common.SECRET_CLASSIFIER_BEDROCK_TOKEN, value)
        token = (get_secret(store, common.SECRET_CLASSIFIER_BEDROCK_TOKEN) if c["credential"] == "separate"
                 else bedrock_credential(settings, store))
        if token and not getattr(args, "no_validate", False):
            result = validate.probe_bedrock(settings, token, c["bedrock_model_id"], kind="claude")
            (ok if result.ok else fail)("Classifier model: " + result.message)
            if not result.ok:
                if result.hint:
                    info("    " + result.hint)
                raise GatewayError("The classifier model is not reachable; auto mode stays disabled.",
                                   code=result.code)
    else:
        c["anthropic_model"] = getattr(args, "model_id", None) or c.get("anthropic_model") or "claude-sonnet-5"
        key = get_secret(store, common.SECRET_CLASSIFIER_ANTHROPIC_KEY)
        if not key or getattr(args, "replace_token", False):
            key = common.read_secret("Anthropic API key for the classifier (separate from the gateway key)")
        if not getattr(args, "no_validate", False):
            result = validate.probe_anthropic(key, c["anthropic_model"])
            (ok if result.ok else fail)("Classifier model: " + result.message)
            if not result.ok:
                raise GatewayError("The classifier model is not reachable; auto mode stays disabled.",
                                   code=result.code)
        store.set(common.SECRET_CLASSIFIER_ANTHROPIC_KEY, key)
    common.require_valid(settings)
    common.save_settings(settings, common.paths())
    ok(f"Auto-mode classifier route enabled: '{c['request_model']}' -> "
       f"{c['bedrock_model_id'] if backend == 'bedrock' else 'anthropic/' + c['anthropic_model']}")


def cmd_classifier(args: argparse.Namespace) -> int:
    settings, store = load()
    c = settings["classifier"]
    if args.action == "status":
        if c["backend"] == "none":
            info("Auto-mode classifier: not configured. claude-kimi launches Claude Code with auto mode disabled.")
        else:
            target = c.get("bedrock_model_id") or common.default_classifier_model_for_region(settings["aws_region"])
            info(f"Auto-mode classifier: {c['backend']} ({target if c['backend'] == 'bedrock' else c['anthropic_model']})"
                 f", requested by Claude Code as '{c['request_model']}', credential: {c['credential']}")
        return 0
    if args.action == "disable":
        c["backend"] = "none"
        common.save_settings(settings, common.paths())
        common.write_runtime_config(settings)
        ok("Classifier route disabled; Claude Code will be launched with auto mode disabled.")
        offer_restart(args, settings, store, "classifier configuration")
        return 0
    enable_classifier(args, settings, store)
    common.write_runtime_config(settings)
    offer_restart(args, settings, store, "classifier configuration")
    return 0


# ---------------------------------------------------------------- claude code


def claude_environment(settings: dict[str, Any], master: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    inherited_sonnet = env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
    if inherited_sonnet == settings["model_alias"]:
        raise GatewayError(
            "ANTHROPIC_DEFAULT_SONNET_MODEL is set to the Kimi alias in your environment. Claude Code uses that "
            "variable for the auto-mode classifier model, which would make Kimi K3 the classifier.",
            hint="Unset ANTHROPIC_DEFAULT_SONNET_MODEL; the launcher sets it correctly.", code="classifier_conflict")
    for key in CONFLICTING_CLAUDE_ENV:
        env.pop(key, None)
    alias = settings["model_alias"]
    env.update({
        "ANTHROPIC_BASE_URL": validate.proxy_base_url(settings),
        "ANTHROPIC_AUTH_TOKEN": master,
        "ANTHROPIC_MODEL": alias,
        # Background work, Haiku/Opus aliases and subagents stay on Kimi K3 ...
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": alias,
        "CLAUDE_CODE_SUBAGENT_MODEL": alias,
        # ... but the Sonnet slot is what Claude Code uses for the auto-mode classifier. Point it at the
        # Claude classifier route, never at Kimi (verified against Claude Code 2.1.303).
        "ANTHROPIC_DEFAULT_SONNET_MODEL": settings["classifier"]["request_model"],
        # Server-side classifier review needs the Anthropic API; through this gateway it can't work.
        "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
        # The attribution block opens the system prompt with a per-conversation fingerprint, so every
        # new session would miss Bedrock's prompt cache. Claude Code's gateway guide says to omit it
        # (at the client) when forwarding to a third-party provider or reshaping system content.
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "KGW_GATEWAY": "1",
    })
    if settings["claude"].get("gateway_hint_headers", True):
        env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "1"
    if settings["claude"].get("disable_structured_outputs"):
        env["CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS"] = "1"
    return env


def claude_arguments(settings: dict[str, Any], classifier_ready: bool, user_args: list[str]) -> list[str]:
    args: list[str] = []
    if not classifier_ready:
        args += ["--settings", json.dumps({"disableAutoMode": "disable"})]
    return args + list(settings["claude"].get("extra_args") or []) + list(user_args)


def cmd_launch_claude(args: argparse.Namespace) -> int:
    settings, store = load()
    common.require_valid(settings)
    plat = common.detect_platform()
    claude = validate.find_claude()
    if not claude:
        raise GatewayError("Claude Code is not installed or not on PATH.",
                           hint=validate.claude_install_hint(plat.system), code="claude_missing")
    version = validate.claude_version(claude)
    parsed = common.parse_version(version or "")
    if parsed and parsed < common.MIN_CLAUDE_CODE:
        warn(f"Claude Code {version} is older than {'.'.join(map(str, common.MIN_CLAUDE_CODE))}; run 'claude update'.")
    master = get_secret(store, common.SECRET_MASTER_KEY)
    health = validate.proxy_health(settings, master)
    if not health.ok:
        fail(health.message)
        if health.code == "not_running" and interactive(args) and common.confirm(
                "Start the gateway in the background now?", default=True):
            if start_proxy(settings, store, background=True) != 0:
                return 1
            health = validate.proxy_health(settings, master)
        if not health.ok:
            raise GatewayError("The gateway is not healthy; not launching Claude Code.",
                               hint="Start it with ./start.sh (or .\\start.ps1) and check: gateway doctor",
                               code=health.code)
    ok(health.message)
    classifier_ready = False
    c = settings["classifier"]
    if c["backend"] == "none":
        warn("Auto mode is DISABLED for this session: no supported Claude classifier is configured, and Kimi K3 "
             "is not a supported auto-mode classifier. Use Manual or Accept-edits mode, or the sandbox "
             "(/sandbox, auto-allow). To enable auto mode: gateway classifier enable")
    elif c["request_model"] not in (health.detail or {}).get("models", []):
        warn("The classifier route is configured but the running gateway does not serve it; restart the gateway. "
             "Auto mode is disabled for this session.")
    elif args.skip_classifier_check:
        classifier_ready = True
        warn("Skipping the classifier connectivity check (--skip-classifier-check).")
    else:
        result = validate.proxy_messages_test(settings, master, c["request_model"], classifier=True)
        classifier_ready = result.ok
        if result.ok:
            ok(f"Auto-mode classifier route answered ({c['request_model']}). Auto mode is available.")
        else:
            fail("Classifier route check failed: " + result.message)
            warn("Auto mode is disabled for this session.")
    env = claude_environment(settings, master, None)
    argv = [claude] + claude_arguments(settings, classifier_ready, args.claude_args)
    if any(a == "--settings" for a in args.claude_args) and not classifier_ready:
        warn("You passed --settings; Claude Code may let it override the launcher's disableAutoMode setting.")
    info(f"Launching Claude Code {version or ''} with model '{settings['model_alias']}' via "
         f"{env['ANTHROPIC_BASE_URL']} (credentials are passed only to this process).")
    info("Note: Claude Code may print an 'unrecognized model' notice for the custom alias; requests are still served "
         "by the gateway's Kimi K3 route.\n")
    if os.name == "nt":
        return subprocess.call(argv, env=env)
    os.execvpe(claude, argv, env)
    return 0  # pragma: no cover


def cmd_internal_claude_env(args: argparse.Namespace) -> int:
    """For scripts/claude-code-env.ps1 only: the launcher environment as JSON.

    Refuses to write to an interactive terminal so the master key is never displayed.
    """
    if sys.stdout.isatty():
        raise GatewayError("Refusing to print credentials to a terminal.", code="refused")
    settings, store = load()
    common.require_valid(settings)
    master = store.get(common.SECRET_MASTER_KEY)
    if not master:
        raise GatewayError("No master key stored; run setup.", code="missing_master_key")
    env = claude_environment(settings, master, {})
    ready = settings["classifier"]["backend"] != "none"
    sys.stdout.write(json.dumps({"set": env, "unset": list(CONFLICTING_CLAUDE_ENV),
                                 "claude_args": claude_arguments(settings, ready, [])}))
    return 0


# ------------------------------------------------------------- status & tools


def print_settings(settings: dict[str, Any]) -> None:
    c = settings["classifier"]
    rows = [
        ("AWS Region", settings["aws_region"]),
        ("Bedrock model", settings["bedrock_model_id"]),
        ("Model alias", settings["model_alias"]),
        ("Kimi API / effort", f"{settings['kimi_api']} / {settings['kimi_reasoning_effort']}"
                              f"{' / prompt cache' if settings.get('kimi_prompt_cache', True) else ''}"),
        ("Proxy", f"{settings['proxy_host']}:{settings['proxy_port']}"),
        ("Bedrock auth", "access key (SigV4)" if settings.get("aws_auth") == "access_key" else "bearer token"),
        ("Credential store", settings["credential_backend"]),
        ("LiteLLM (configured)", settings["litellm_version"]),
        ("Auto-mode classifier", c["backend"] if c["backend"] == "none" else
         f"{c['backend']} -> {c.get('bedrock_model_id') or common.default_classifier_model_for_region(settings['aws_region']) if c['backend'] == 'bedrock' else c['anthropic_model']}"),
    ]
    if settings.get("bedrock_endpoint_url"):
        rows.append(("Bedrock endpoint", settings["bedrock_endpoint_url"]))
    for k, v in rows:
        info(f"    {k:<22}: {v}")


def installed_litellm_version() -> str | None:
    py = common.venv_python()
    if not py.exists():
        return None
    out = subprocess.run([str(py), "-c", "from importlib.metadata import version; print(version('litellm'))"],
                         capture_output=True, text=True, timeout=60, check=False)
    return out.stdout.strip() or None


def cmd_status(args: argparse.Namespace) -> int:
    settings, store = load()
    state = proxy_state(settings)
    master = get_secret(store, common.SECRET_MASTER_KEY)
    health = validate.proxy_health(settings, master) if state["port_busy"] else None
    data = {
        "running": state["ours"], "pid": state["pid"], "mode": state["mode"],
        "port_in_use_by_other": state["foreign"],
        "healthy": bool(health and health.ok), "health": health.message if health else "not running",
        "litellm": installed_litellm_version(), "settings": {
            "aws_region": settings["aws_region"], "bedrock_model_id": settings["bedrock_model_id"],
            "model_alias": settings["model_alias"], "proxy": f"{settings['proxy_host']}:{settings['proxy_port']}",
            "classifier": settings["classifier"]["backend"]},
    }
    if args.json:
        print(json.dumps(data, indent=2))
        return 0 if data["running"] and data["healthy"] else 1
    info("Kimi Bedrock Gateway status")
    print_settings(settings)
    info(f"    {'LiteLLM (installed)':<22}: {data['litellm'] or 'not installed'}")
    if state["ours"]:
        info(f"    {'Process':<22}: running, PID {state['pid']} ({state['mode']})")
    elif state["foreign"]:
        info(f"    {'Process':<22}: NOT running; port {settings['proxy_port']} is used by another process")
    else:
        info(f"    {'Process':<22}: not running")
    if health:
        info(f"    {'Health':<22}: {'healthy' if health.ok else 'unhealthy'} - {health.message}")
    return 0 if state["ours"] and health and health.ok else 1


def cmd_health(args: argparse.Namespace) -> int:
    settings, store = load()
    master = get_secret(store, common.SECRET_MASTER_KEY)
    health = validate.proxy_health(settings, master)
    (ok if health.ok else fail)(health.message)
    if not health.ok:
        return 1
    if args.live:
        result = validate.proxy_messages_test(settings, master)
        (ok if result.ok else fail)(result.message)
        if result.hint and not result.ok:
            info("    " + result.hint)
        if settings["classifier"]["backend"] != "none":
            cres = validate.proxy_messages_test(settings, master, settings["classifier"]["request_model"],
                                                classifier=True)
            (ok if cres.ok else fail)("Classifier route: " + cres.message)
            return 0 if result.ok and cres.ok else 1
        return 0 if result.ok else 1
    info("    (liveness + authenticated model listing only; add --live to send a real model request)")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    lines = tail_log(args.lines)
    if not lines:
        info(f"No background log yet ({common.paths().proxy_log}). Foreground runs log to the terminal.")
        return 0
    for line in lines:
        info(line)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    settings = common.load_settings()
    claude = validate.find_claude()
    info(f"LiteLLM installed : {installed_litellm_version() or 'not installed'} (pinned/tested {common.PINNED_LITELLM}, "
         f"configured {settings['litellm_version']})")
    info(f"Claude Code       : {validate.claude_version(claude) if claude else 'not installed'} "
         f"(tested {common.TESTED_CLAUDE_CODE})")
    info(f"Python (venv)     : {_venv_python_version() or 'no venv'}")
    info(f"Model             : {settings['model_alias']} -> {settings['bedrock_model_id']} ({settings['aws_region']})")
    return 0


def _venv_python_version() -> str | None:
    py = common.venv_python()
    if not py.exists():
        return None
    return subprocess.run([str(py), "-c", "import sys, platform; print(sys.version.split()[0], platform.machine())"],
                          capture_output=True, text=True, timeout=30, check=False).stdout.strip()


def cmd_config(args: argparse.Namespace) -> int:
    settings = common.load_settings()
    if args.action == "show":
        print_settings(settings)
        info(f"    {'Settings file':<22}: {common.paths().settings}")
        info(f"    {'Runtime config':<22}: {common.paths().runtime_config}")
        return 0
    issues = common.validate_settings(settings)
    for issue in issues:
        (fail if issue.level == "error" else warn)(issue.message + (f" -> {issue.hint}" if issue.hint else ""))
    if any(i.level == "error" for i in issues):
        return 1
    text = common.render_runtime_config(settings)
    try:
        import yaml  # noqa: PLC0415

        parsed = yaml.safe_load(text)
        names = [m["model_name"] for m in parsed["model_list"]]
        assert parsed["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    except Exception as exc:  # noqa: BLE001
        fail(f"Rendered LiteLLM config is invalid: {exc.__class__.__name__}: {exc}")
        return 1
    ok(f"Settings valid; LiteLLM config renders with models {names} and no inline secrets.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0
    plat = common.detect_platform()
    step("Platform")
    info(f"    {plat.label}; manager Python {sys.version.split()[0]} ({plat.python_machine})")
    for note in plat.notes:
        warn(note)
    step("Virtual environment & dependencies")
    venv_ver = _venv_python_version()
    if venv_ver:
        ok(f"venv Python {venv_ver}")
        lite = installed_litellm_version()
        (ok if lite == common.load_settings()["litellm_version"] else warn)(
            f"LiteLLM {lite} installed (configured {common.load_settings()['litellm_version']})")
        if plat.system == "Darwin" and "arm64" not in venv_ver and plat.machine == "arm64":
            warn("The venv Python is not arm64 on Apple Silicon (running under Rosetta).")
    else:
        fail("No virtual environment. Run setup.")
        problems += 1
    step("Settings")
    try:
        settings, store = load()
        for issue in common.validate_settings(settings):
            (fail if issue.level == "error" else warn)(issue.message)
            problems += issue.level == "error"
        print_settings(settings)
    except GatewayError as exc:
        fail(str(exc))
        return 1
    step("Credentials (presence only; values are never shown)")
    try:
        if settings["credential_backend"] == "keyring":
            usable, name = common.SecretStore.keyring_usable()
            (ok if usable else fail)(f"Keyring backend: {name}")
            problems += not usable
        primary = ((common.SECRET_AWS_ACCESS_KEY_ID, common.SECRET_AWS_SECRET_ACCESS_KEY)
                   if settings.get("aws_auth") == "access_key" else (common.SECRET_BEDROCK_TOKEN,))
        for secret in (*primary, common.SECRET_MASTER_KEY):
            value = get_secret(store, secret)
            (ok if value else fail)(f"{secret}: {'stored (fingerprint ' + common.fingerprint(value) + ')' if value else 'missing'}")
            problems += not value
    except GatewayError as exc:
        fail(str(exc))
        problems += 1
    step("Proxy")
    state = proxy_state(settings)
    if state["ours"]:
        health = validate.proxy_health(settings, get_secret(store, common.SECRET_MASTER_KEY))
        (ok if health.ok else fail)(health.message)
        problems += not health.ok
    elif state["foreign"]:
        fail(f"Port {settings['proxy_port']} is used by another process.")
        problems += 1
    else:
        info("    Not running (start it with ./start.sh).")
    step("Claude Code & auto mode")
    report_claude(plat)
    c = settings["classifier"]
    if c["backend"] == "none":
        info("    Auto mode: disabled by the launcher (no supported Claude classifier configured).")
    else:
        info(f"    Auto mode: classifier route '{c['request_model']}' via {c['backend']}.")
    for var in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        if os.environ.get(var):
            warn(f"{var} is set in this shell; the launcher overrides/unsets it for Claude Code.")
    if args.live:
        step("Live Bedrock check")
        token = bedrock_credential(settings, store)
        if token:
            result = validate.probe_bedrock(settings, token)
            (ok if result.ok else fail)(result.message)
            if result.hint and not result.ok:
                info("    " + result.hint)
            problems += not result.ok
    info(f"\n{'No problems found.' if not problems else f'{problems} problem(s) found.'}")
    return 1 if problems else 0


def cmd_start(args: argparse.Namespace) -> int:
    settings, store = load()
    return start_proxy(settings, store, background=args.background)


def cmd_stop(args: argparse.Namespace) -> int:
    settings, _ = load()
    stop_proxy(settings)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    settings, store = load()
    state = proxy_state(settings)
    if state["ours"] and state["mode"] == "foreground":
        stop_proxy(settings, quiet=True)
        info("Stopped the foreground gateway; start it again with ./start.sh (or pass --background here next time).")
        return 0
    stop_proxy(settings, quiet=True)
    return start_proxy(settings, store, background=True)


# --------------------------------------------------------------- secret scan

SCAN_EXCLUDE_DIRS = {".git", ".venv", ".venv-upgrade", ".venv.previous", "node_modules", "__pycache__",
                     ".pytest_cache", ".mypy_cache", ".ruff_cache"}
ALLOW_MARKER = "kgw-scan: allow"


def _scan_candidates(root: Path) -> list[Path]:
    if (root / ".git").exists() and shutil.which("git"):
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=False)
        if out.returncode == 0:
            return [root / p.decode() for p in out.stdout.split(b"\0") if p]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_EXCLUDE_DIRS]
        files.extend(Path(dirpath) / f for f in filenames)
    return files


def scan_for_secrets(root: Path, known: list[str]) -> list[tuple[str, int, str]]:
    findings = []
    patterns = common._SECRET_PATTERNS  # noqa: SLF001 - shared definition
    for path in _scan_candidates(root):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                continue
            hit = None
            if any(k and k in line for k in known):
                hit = "stored credential value"
            else:
                for pattern in patterns:
                    if pattern.search(line):
                        hit = f"credential-like pattern /{pattern.pattern[:40]}/"
                        break
            if hit:
                findings.append((str(path.relative_to(root)), lineno, hit))
    return findings


def cmd_scan_secrets(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else common.REPO_ROOT
    known: list[str] = []
    if not args.no_store:
        try:
            settings, store = load()
            for name in common.ALL_SECRETS + (common.SECRET_BEDROCK_TOKEN + PREVIOUS_SUFFIX,):
                value = store.get(name)
                if value:
                    known.append(value)
                    common.register_secret(value)
        except GatewayError as exc:
            warn(f"Could not read stored credentials for exact matching ({exc}); pattern scan only.")
    info(f"Scanning {root} locally (nothing is uploaded) ...")
    findings = scan_for_secrets(root, known)
    for path, lineno, kind in findings:
        fail(f"{path}:{lineno}: {kind}")
    if findings:
        info(f"\n{len(findings)} possible secret(s) found. Remove them, rotate the affected credentials, "
             f"and purge them from git history if they were committed. Mark deliberate test fixtures with "
             f"'{ALLOW_MARKER}'.")
        return 1
    ok("No credentials found.")
    return 0


# ------------------------------------------------------------------ upgrade


def cmd_upgrade(args: argparse.Namespace) -> int:
    settings, store = load()
    venv = common.venv_dir()
    previous = venv.with_name(venv.name + PREVIOUS_SUFFIX)
    if args.rollback:
        if not previous.exists():
            raise GatewayError("No previous virtual environment to roll back to.", code="nothing_to_rollback")
        stop_proxy(settings, quiet=True)
        failed = venv.with_name(venv.name + ".failed")
        shutil.rmtree(failed, ignore_errors=True)
        venv.rename(failed)
        previous.rename(venv)
        shutil.rmtree(failed, ignore_errors=True)
        ver = installed_litellm_version()
        settings["litellm_version"] = ver or common.PINNED_LITELLM
        common.save_settings(settings, common.paths())
        ok(f"Rolled back to LiteLLM {ver}. Start the gateway again.")
        return 0
    target = args.litellm
    current = installed_litellm_version()
    info(f"Upgrade LiteLLM {current} -> {target} (tested version: {common.PINNED_LITELLM}).")
    if re.search(r"[a-z]", target) and not args.allow_prerelease:
        raise GatewayError(f"{target} looks like a pre-release.", hint="Pass --allow-prerelease to try it anyway "
                           "(experimental; not recommended for daily use).", code="prerelease")
    if not args.yes and not (interactive(args) and common.confirm("Continue?", default=False)):
        return 1
    dest = backup_settings("upgrade")
    info(f"Backed up configuration to {dest}")
    staging = venv.with_name(".venv-upgrade")
    shutil.rmtree(staging, ignore_errors=True)
    trial = dict(settings, litellm_version=target)
    install_requirements(staging, trial)
    py = str(common.venv_python(staging))
    step("Compatibility checks in the staging environment")
    # LiteLLM cannot see the model behind an application-profile ARN; check the Kimi ID it would serve.
    lookaround_model = "moonshotai.kimi-k3" if common.is_application_profile(settings["bedrock_model_id"]) \
        else settings["bedrock_model_id"]
    check = subprocess.run([py, "-c", (
        "import json, litellm, importlib.metadata as m\n"
        "from litellm.llms.bedrock.common_utils import bedrock_model_supports_regex_lookaround as f\n"
        f"print(json.dumps({{'version': m.version('litellm'), 'drops_lookaround': not f({lookaround_model!r})}}))")],
        capture_output=True, text=True, timeout=300, check=False, env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    if check.returncode != 0:
        fail("The staged LiteLLM lacks APIs the gateway relies on:")
        info("    " + common.redact((check.stderr or "").strip().splitlines()[-1] if check.stderr else "unknown error"))
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    result = json.loads(check.stdout.strip().splitlines()[-1])
    (ok if result["drops_lookaround"] else fail)(
        f"LiteLLM {result['version']} {'drops' if result['drops_lookaround'] else 'does NOT drop'} look-around "
        f"regexes from tool schemas for {lookaround_model}")
    subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(common.REPO_ROOT / "requirements-dev.txt")],
                   check=False)
    tests = subprocess.run([py, "-m", "pytest", "-q", "-m", "not e2e and not live", str(common.REPO_ROOT / "tests")],
                           check=False, env={**os.environ, "KGW_VENV": str(staging), "KGW_NO_REEXEC": "1"})
    if tests.returncode != 0 or not result["drops_lookaround"]:
        fail("Compatibility checks failed; the current installation was left untouched.")
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    stop_proxy(settings, quiet=True)
    shutil.rmtree(previous, ignore_errors=True)
    venv.rename(previous)
    staging.rename(venv)
    settings["litellm_version"] = target
    common.save_settings(settings, common.paths())
    ok(f"Upgraded to LiteLLM {target}. Previous environment kept at {previous} (gateway upgrade --rollback).")
    info("Start the gateway and run 'gateway health --live' to verify against Bedrock.")
    return 0


# ---------------------------------------------------------------- uninstall


def _safe_remove(path: Path, allowed_parents: list[Path]) -> bool:
    """Delete ``path`` only when it is inside one of ``allowed_parents`` (never the repo or home itself)."""
    resolved = path.resolve()
    forbidden = {common.REPO_ROOT.resolve(), Path.home().resolve(), Path(resolved.anchor)}
    if resolved in forbidden:
        raise GatewayError(f"Refusing to delete {resolved}.", code="unsafe_delete")
    if not any(resolved == a.resolve() or a.resolve() in resolved.parents for a in allowed_parents):
        raise GatewayError(f"Refusing to delete {resolved}: outside the gateway's own directories.",
                           code="unsafe_delete")
    if not resolved.exists():
        return False
    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()
    return True


def cmd_uninstall(args: argparse.Namespace) -> int:
    p = common.paths()
    settings = common.load_settings() if p.settings.exists() else common.default_settings()
    venv = common.venv_dir()
    remove_venv = args.remove_venv
    remove_config = args.remove_config
    remove_creds = args.remove_credentials
    info("Uninstall plan for the Kimi Bedrock Gateway (the repository itself is never deleted):")
    info(f"  - stop the gateway if it is running")
    info(f"  - delete generated runtime files: {p.runtime_dir}, {p.pid_file.parent}, {p.log_dir}")
    if interactive(args) and not args.yes:
        remove_venv = remove_venv or common.confirm(f"Also delete the virtual environment {venv}?", default=False)
        remove_config = remove_config or common.confirm(f"Also delete settings and backups in {p.root}?", default=False)
        remove_creds = remove_creds or common.confirm(
            "Also delete the saved credentials (Bedrock token, master key, classifier keys) from the "
            f"{settings['credential_backend']} store? This cannot be undone.", default=False)
    info(f"  - virtual environment: {'DELETE' if remove_venv else 'keep'}")
    info(f"  - settings & backups : {'DELETE' if remove_config else 'keep'}")
    info(f"  - saved credentials  : {'DELETE' if remove_creds else 'keep'}")
    if not args.yes and not (interactive(args) and common.confirm("Proceed?", default=False)):
        info("Nothing was changed.")
        return 1
    try:
        stop_proxy(settings, quiet=True)
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not stop the proxy cleanly: {exc.__class__.__name__}")
    for target in (p.runtime_dir, p.pid_file.parent, p.log_dir, p.setup_progress):
        if _safe_remove(target, [p.root]):
            ok(f"Removed {target}")
    if remove_venv:
        running_from = Path(sys.prefix).resolve()
        for candidate in (venv, venv.with_name(venv.name + PREVIOUS_SUFFIX), venv.with_name(".venv-upgrade")):
            if not (candidate.exists() and (candidate / "pyvenv.cfg").exists()):
                continue
            if os.name == "nt" and candidate.resolve() == running_from:
                # Windows can't delete the interpreter that is running; uninstall.ps1 finishes the job.
                (candidate / ".kgw-remove-pending").write_text("1")
                info(f"{candidate} will be removed by uninstall.ps1 after this process exits.")
                continue
            _safe_remove(candidate, [common.REPO_ROOT])
            ok(f"Removed {candidate}")
    if remove_creds:
        try:
            store = common.store_for(settings)
            for name in common.ALL_SECRETS + (common.SECRET_BEDROCK_TOKEN + PREVIOUS_SUFFIX,):
                if store.delete(name):
                    ok(f"Deleted credential '{name}'")
        except GatewayError as exc:
            fail(f"Could not delete credentials: {exc}")
        if p.file_secrets.exists():
            _safe_remove(p.file_secrets, [p.root])
    if remove_config:
        for target in (p.settings, p.backups):
            if _safe_remove(target, [p.root]):
                ok(f"Removed {target}")
    if p.root.exists() and not any(p.root.iterdir()):
        p.root.rmdir()
    ok("Uninstall finished.")
    if not remove_creds:
        info("Saved credentials were kept; re-running setup will reuse them.")
    return 0


# ---------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage.py", description="Kimi Bedrock Gateway manager")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt; fail instead")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="(used by setup wrappers) prepare venv, then run setup")
    b.add_argument("--allow-rosetta", action="store_true")
    b.add_argument("setup_args", nargs=argparse.REMAINDER)

    s = sub.add_parser("setup", help="configure credentials, settings and verify the gateway")
    s.add_argument("--region")
    s.add_argument("--model-id")
    s.add_argument("--endpoint-url", help="override the Bedrock runtime endpoint (VPC endpoint or test mock)")
    s.add_argument("--credential-backend", choices=["keyring", "file"])
    s.add_argument("--auth", choices=list(AUTH_CHOICES),
                   help="authenticate with a Bedrock API key (bearer, default) or an IAM access key pair")
    s.add_argument("--token-stdin", action="store_true",
                   help="read the Bedrock token (or access key ID and secret, one per line) from stdin")
    s.add_argument("--replace-token", action="store_true")
    s.add_argument("--reset", action="store_true", help="reset settings to defaults (backup kept)")
    s.add_argument("--skip-live", action="store_true", help="skip the live Bedrock test")
    s.add_argument("--no-validate", action="store_true", help="store the token without a live check")
    s.add_argument("--save-unvalidated", action="store_true")
    s.add_argument("--classifier", choices=["none", "bedrock", "anthropic"])
    s.add_argument("--separate-credential", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--allow-rosetta", action="store_true")

    st = sub.add_parser("start", help="start the proxy (foreground by default)")
    st.add_argument("--background", action="store_true")
    sub.add_parser("stop", help="stop the proxy")
    sub.add_parser("restart", help="restart the proxy in the background")
    s2 = sub.add_parser("status", help="process, health and configuration summary")
    s2.add_argument("--json", action="store_true")
    h = sub.add_parser("health", help="health check (add --live for a real model request)")
    h.add_argument("--live", action="store_true")
    lg = sub.add_parser("logs", help="recent sanitized background logs")
    lg.add_argument("-n", "--lines", type=int, default=60)
    sub.add_parser("version", help="installed versions")
    cf = sub.add_parser("config", help="show or validate configuration")
    cf.add_argument("action", choices=["show", "validate"])

    uk = sub.add_parser("update-key", help="replace the Bedrock bearer token")
    uk.add_argument("--yes", action="store_true", help="replace without confirmation")
    uk.add_argument("--no-validate", action="store_true")
    uk.add_argument("--save-unvalidated", action="store_true", help="save even if validation fails")
    uk.add_argument("--restart", action="store_true", help="restart a running background proxy afterwards")
    uk.add_argument("--rollback", action="store_true", help="restore the previously stored token")
    uk.add_argument("--forget-previous", action="store_true")

    sa = sub.add_parser("set-auth", help="choose Bedrock authentication: bearer token or IAM access key pair")
    sa.add_argument("mode", choices=list(AUTH_CHOICES))
    sa.add_argument("--replace", action="store_true", help="enter a new access key pair even if one is stored")
    sa.add_argument("--forget-access-key", action="store_true", help="(bearer) delete the stored access key pair")
    sa.add_argument("--yes", action="store_true", help="replace without confirmation")
    sa.add_argument("--no-validate", action="store_true")
    sa.add_argument("--save-unvalidated", action="store_true", help="save even if validation fails")
    sa.add_argument("--restart", action="store_true", help="restart a running background proxy afterwards")

    rm = sub.add_parser("rotate-master-key", help="generate a new local proxy master key")
    rm.add_argument("--yes", action="store_true")
    rm.add_argument("--restart", action="store_true")

    sr = sub.add_parser("set-region", help="change the AWS Region (and optionally model ID)")
    sr.add_argument("region")
    sr.add_argument("--model-id")
    sr.add_argument("--yes", action="store_true")
    sr.add_argument("--no-validate", action="store_true")
    sr.add_argument("--restart", action="store_true")

    cl = sub.add_parser("classifier", help="auto-mode classifier route: status | enable | disable")
    cl.add_argument("action", choices=["status", "enable", "disable"])
    cl.add_argument("--backend", choices=["bedrock", "anthropic"], default="bedrock")
    cl.add_argument("--model-id", help="Bedrock profile or application-profile ARN (bedrock), or model name (anthropic)")
    cl.add_argument("--base-model", help="for an application-profile ARN: the Claude model behind it "
                                         "(default anthropic.claude-sonnet-5)")
    cl.add_argument("--separate-credential", action="store_true", help="use a separate Bedrock token")
    cl.add_argument("--replace-token", action="store_true")
    cl.add_argument("--no-validate", action="store_true")
    cl.add_argument("--restart", action="store_true")

    lc = sub.add_parser("launch-claude", help="run Claude Code through the gateway")
    lc.add_argument("--skip-classifier-check", action="store_true")
    lc.add_argument("claude_args", nargs=argparse.REMAINDER, help="arguments passed to claude (after --)")

    sub.add_parser("_claude-env", help=argparse.SUPPRESS)

    dr = sub.add_parser("doctor", help="diagnose the installation")
    dr.add_argument("--live", action="store_true", help="also send one request directly to Bedrock")

    sc = sub.add_parser("scan-secrets", help="scan the repository for committed credentials (local only)")
    sc.add_argument("--path")
    sc.add_argument("--no-store", action="store_true", help="don't compare against stored credential values")

    up = sub.add_parser("upgrade", help="upgrade LiteLLM in a staging venv with compatibility checks")
    up.add_argument("--litellm", default=common.PINNED_LITELLM)
    up.add_argument("--allow-prerelease", action="store_true")
    up.add_argument("--rollback", action="store_true")
    up.add_argument("--yes", action="store_true")

    un = sub.add_parser("uninstall", help="remove generated files (credentials kept unless requested)")
    un.add_argument("--remove-venv", action="store_true")
    un.add_argument("--remove-config", action="store_true")
    un.add_argument("--remove-credentials", action="store_true")
    un.add_argument("--yes", action="store_true")
    return parser


COMMANDS = {
    "bootstrap": cmd_bootstrap, "setup": cmd_setup, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "status": cmd_status, "health": cmd_health, "logs": cmd_logs, "version": cmd_version, "config": cmd_config,
    "update-key": cmd_update_key, "set-auth": cmd_set_auth, "rotate-master-key": cmd_rotate_master_key, "set-region": cmd_set_region,
    "classifier": cmd_classifier, "launch-claude": cmd_launch_claude, "_claude-env": cmd_internal_claude_env,
    "doctor": cmd_doctor, "scan-secrets": cmd_scan_secrets, "upgrade": cmd_upgrade, "uninstall": cmd_uninstall,
}


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    non_interactive = "--non-interactive" in raw
    parsed_argv = [a for a in raw if a != "--non-interactive"]
    parser = build_parser()
    args = parser.parse_args(parsed_argv)
    args.non_interactive = non_interactive or bool(os.environ.get("KGW_NON_INTERACTIVE"))
    if getattr(args, "claude_args", None) and args.claude_args[:1] == ["--"]:
        args.claude_args = args.claude_args[1:]
    if getattr(args, "setup_args", None) and args.setup_args[:1] == ["--"]:
        args.setup_args = args.setup_args[1:]
    if args.command not in ("bootstrap",) and Path(sys.prefix).resolve() != common.venv_dir().resolve() \
            and common.venv_python().exists() and not os.environ.get("KGW_NO_REEXEC"):
        # Always run management commands inside the project venv (keyring, psutil, litellm).
        py = str(common.venv_python())
        forward = [py, str(Path(__file__).resolve()), *raw]
        if os.name == "nt":
            return subprocess.call(forward)
        os.execv(py, forward)
    try:
        return COMMANDS[args.command](args) or 0
    except GatewayError as exc:
        fail(str(exc))
        if exc.hint:
            info("    " + exc.hint.replace("\n", "\n    "))
        return 2
    except KeyboardInterrupt:
        info("\nInterrupted. Re-run the same command to resume; setup steps are safe to repeat.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
