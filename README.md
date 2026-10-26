# Kimi Bedrock Gateway

Use **Kimi K3 on Amazon Bedrock** as the model behind **Claude Code**, through a local
**LiteLLM** proxy, on Windows 10/11, Ubuntu and Apple Silicon macOS.

```
Claude Code ──(Anthropic Messages, ANTHROPIC_BASE_URL)──► LiteLLM proxy on 127.0.0.1:4000 ──(Chat Completions, bearer token)──► Bedrock: in.moonshotai.kimi-k3
   process-scoped env from the launcher                    + kimi_gateway_hooks adapter                                   (ap-south-1 → Mumbai/Hyderabad)
                                                            └─ optional classifier route ─(Invoke)─► Bedrock: in.anthropic.claude-sonnet-5
```

* `kimi-k3` is the coding model for every Claude Code request: main loop, subagents, and the
  background/Haiku/Opus slots.
* Claude Code authenticates to the proxy with a randomly generated local **master key**. The proxy
  authenticates to Bedrock with your **Bedrock API key** (`AWS_BEARER_TOKEN_BEDROCK`). Both are kept
  in the OS credential store, and neither is ever written to a file in this repository.
* Auto Mode needs a real Claude classifier. Kimi K3 is never used as one (see [Auto Mode](#7-auto-mode)).

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Install (one command)](#2-install-one-command)
3. [Start and stop the proxy](#3-start-and-stop-the-proxy)
4. [Launch Claude Code through the gateway](#4-launch-claude-code-through-the-gateway)
5. [Update the Bedrock bearer token](#5-update-the-bedrock-bearer-token)
6. [Change Region or model](#6-change-region-or-model)
7. [Auto Mode](#7-auto-mode)
8. [Troubleshooting: authentication and model access](#8-troubleshooting-authentication-and-model-access)
9. [Troubleshooting: structured-output regex error](#9-troubleshooting-the-structured-output-regex-error)
10. [Updating dependencies safely](#10-updating-dependencies-safely)
11. [Uninstall](#11-uninstall)
12. [Security considerations](#12-security-considerations)
13. [Supported versions, test status and known limitations](#13-supported-versions-test-status-and-known-limitations)
14. [Repository layout and development](#14-repository-layout-and-development)

---

## 1. Prerequisites

| | Windows 10/11 | Ubuntu (22.04/24.04) | macOS on Apple Silicon |
|---|---|---|---|
| Shell | PowerShell 5.1 or 7+ (no Git Bash needed) | bash | zsh/bash |
| Python | 3.11 – 3.14 (`winget install Python.Python.3.12`) | 3.11 – 3.14 plus `python3-venv` | **native arm64** 3.11 – 3.14 (python.org universal2, or Homebrew in `/opt/homebrew`) |
| Claude Code | ≥ 2.1.288 (tested 2.1.303) | same | same |
| Node.js | only if you install Claude Code with npm (18+) | same | same |
| Admin / sudo | **not needed** | not needed (setup never runs `sudo`; it prints the apt command if a package is missing) | not needed |

**Install Claude Code** using the official method (docs: <https://code.claude.com/docs/en/setup>):

* macOS / Linux: `curl -fsSL https://claude.ai/install.sh | bash`
* Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`
* or, with Node.js 18+: `npm install -g @anthropic-ai/claude-code`

**AWS:**

* A Bedrock API key, created in the console under *Amazon Bedrock → API keys*. Long-term keys
  start with `ABSK…`; short-term keys start with `bedrock-api-key-…` and expire within 12 hours.
* The key's IAM identity must be allowed to call `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream` on the inference profile, and on the foundation model in
  each destination Region.

Default Region and model: **`ap-south-1`** with **`in.moonshotai.kimi-k3`**. This is the India
geographic inference profile, which routes only between Mumbai and Hyderabad. Kimi K3 has no
In-Region (bare model ID) availability, so you always use a profile:
`in.` (from ap-south-1/2), `us.` (US and Canada Regions) or `global.`
([AWS model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html)).

## 2. Install (one command)

Clone the repository, then run the setup command for your platform.

**Windows (PowerShell):**

```powershell
.\setup.ps1
```

If script execution is blocked, allow it for this one process only. This doesn't change the
machine policy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

**Ubuntu or Apple Silicon macOS:**

```bash
./setup.sh
```

Setup does the following:

1. Detects the OS and architecture. On Apple Silicon it uses `uname -m` and refuses x86_64
   (Rosetta) Python builds; it never installs Rosetta or uses Intel Homebrew paths.
2. Finds a supported Python and creates `.venv`. It then installs the **pinned** dependencies from
   `requirements.txt`. Nothing is installed globally.
3. Asks for the AWS Region (default `ap-south-1`) and checks that the inference profile can be
   called from that Region. If it can't, setup suggests a profile; it never substitutes one silently.
4. Generates the local proxy master key with a CSPRNG (`secrets.token_urlsafe(32)`, prefixed `sk-kgw-`).
5. Asks for the Bedrock API key **without echoing it**, checks it with one tiny direct request to the
   model, and only then stores it.
6. Renders the LiteLLM config. It contains no secrets, only `os.environ/…` references.
7. Starts the proxy and runs a health check and one real request through it, then stops the proxy.
   Setup asks before this step because it costs a few tokens.
8. Reports the Claude Code version and offers to launch it.

**Re-running setup is safe.** A healthy venv is reused, and dependencies are reinstalled only when
`requirements.txt`, the configured LiteLLM version or the Python version changes. The master key is
never regenerated, and a stored token is kept unless you choose to replace it. When a configuration
exists you choose **keep / update / reset**; reset saves a backup first. If setup is interrupted,
run it again: it reports the last completed step and continues.

Non-interactive example, for automation. The token is read from stdin:

```bash
printf '%s\n' "$TOKEN" | ./setup.sh --non-interactive --token-stdin --region ap-south-1 --yes
```

`./setup.sh --help` lists every option: `--region`, `--model-id`, `--classifier`,
`--credential-backend`, `--skip-live`, `--reset` and more.

### Where things are stored

| What | Where |
|---|---|
| Bedrock token, master key, classifier keys | **Windows Credential Manager** / **macOS Keychain** / **Secret Service (libsecret)**, service name `kimi-bedrock-gateway`. Optional fallback: a user-only `secrets.json` file (see below) |
| Settings (non-secret) | `~/.config/kimi-bedrock-gateway/settings.json` (Linux/macOS), `%APPDATA%\kimi-bedrock-gateway\settings.json` (Windows); created with mode 0600, or a current-user-only ACL on Windows |
| Rendered LiteLLM config, PID file, background log, backups | Same private directory: `runtime/`, `run/`, `logs/`, `backups/` |
| Application defaults | `config.yaml` (template) and `scripts/common.py` (`DEFAULT_SETTINGS`), both in the repo |

**Credential storage on headless Ubuntu.** With no desktop session there is usually no Secret
Service, and setup stops with an explanation. Either install and unlock GNOME Keyring
(`sudo apt install gnome-keyring libsecret-1-0`, inside a D-Bus session), or explicitly choose the
file fallback with `./setup.sh --credential-backend file`. That stores secrets in `secrets.json`
with mode 0600 in the private directory. The gateway refuses to read the file if anyone else can.
It's still plaintext on disk, so prefer full-disk encryption.

## 3. Start and stop the proxy

```bash
./start.sh                       # foreground (default) – errors are visible; Ctrl+C stops it
./start.sh --background          # detached; PID file + log in the private directory
./gateway.sh status              # running?, healthy?, Region, alias, LiteLLM version (no secrets)
./gateway.sh health [--live]     # liveness + authenticated model listing; --live sends one real request
./gateway.sh logs [-n 100]       # recent background log lines, secrets redacted
./gateway.sh version             # LiteLLM / Claude Code / Python versions
./gateway.sh restart             # stop + start in the background (after credential/config changes)
./gateway.sh stop
./gateway.sh doctor [--live]     # full diagnosis
```

On Windows, use `.\start.ps1` and `.\gateway.ps1 <command>` with the same arguments.

The proxy binds to **127.0.0.1** only. Binding anywhere else requires setting
`"allow_non_loopback": true` in `settings.json`, and every start then prints a warning.

**Port conflicts and process identity.** The manager never assumes that whatever listens on port 4000
is the gateway:

* `start` refuses to run if another process holds the port.
* `stop` only signals a PID that it recorded **and** whose command line contains this gateway's
  runtime config path.
* `status` and `health` additionally require an authenticated `/v1/models` call, using the gateway's
  master key, that lists the configured alias.

Background mode keeps the master key and Bedrock token only in the proxy process's environment.
They aren't on its command line and aren't in the log.

## 4. Launch Claude Code through the gateway

```bash
./claude-kimi.sh                 # macOS / Ubuntu
.\claude-kimi.ps1                # Windows
./claude-kimi.sh -p "explain this repo"    # any claude arguments are passed through
```

The launcher first checks that Claude Code is installed and reports its version. It then checks that
the gateway is healthy and actually *ours*, and offers to start it in the background if it isn't
running. Finally it starts Claude Code with these **process-scoped** variables; your shell profile
and user/machine environment are never modified:

| Variable | Value | Why |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:4000` | use the gateway |
| `ANTHROPIC_AUTH_TOKEN` | local master key (never printed) | authenticate to the gateway |
| `ANTHROPIC_MODEL` | `kimi-k3` | primary model |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | `kimi-k3` | background work and subagents stay on Kimi |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `claude-sonnet-5` | Claude Code uses this slot for the **Auto Mode classifier** (verified), so it must never point at Kimi |
| `CLAUDE_CODE_AUTO_MODE_SERVER` | `0` | server-side classifier review only works against Anthropic's API |
| `CLAUDE_CODE_GATEWAY_HINT_HEADERS` | `1` | lets the gateway tell main-loop requests apart from classifier requests |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `0` | keeps the prompt prefix identical across sessions so Bedrock's prompt cache hits (Claude Code's gateway guide recommends this for third-party providers) |
| unset | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY/MANTLE`, provider base URLs, `AWS_BEARER_TOKEN_BEDROCK`, `LITELLM_MASTER_KEY` | avoid conflicting auth, and don't hand upstream secrets to Claude Code |

If you'd rather run `claude` yourself in a PowerShell window, dot-source
`. .\scripts\claude-code-env.ps1`. It sets the same variables for that session only and adds the
Auto Mode guard to `claude`.

**"Unrecognized model" notice.** Claude Code prints
`[claude-code:unrecognized_model] {"model":"kimi-k3",…}` because `kimi-k3` isn't a Claude model ID.
This is expected. In tests (real Claude Code 2.1.303 → LiteLLM 1.106.0 → mocked Bedrock) the request
still completed. It does mean Claude Code assumes a 200K context window and current-Claude request
features for the alias. The gateway adapts those requests (see [§9](#9-troubleshooting-the-structured-output-regex-error)).
We deliberately don't use `modelOverrides` to pretend `kimi-k3` is a Claude model: that would feed
Claude-specific capability assumptions to a non-Claude model, and could make Claude Code treat the
Kimi route as eligible for Claude-only duties.

## 5. Update the Bedrock bearer token

```bash
./update-bedrock-key.sh            # macOS / Ubuntu
.\update-bedrock-key.ps1           # Windows
```

1. Reads the new token without echoing it and rejects empty input or input containing whitespace.
2. Asks for confirmation before replacing an existing token, showing only short SHA-256 fingerprints.
3. Validates the token with one minimal request to the configured model, sent directly to Bedrock.
   It's never sent to any other service.
4. Saves the token **only if validation succeeds**; otherwise the existing token is left unchanged.
   `--save-unvalidated` (or an explicit prompt answer, offered only for network or throttling
   failures) overrides this.
5. Keeps the previous token for rollback: `--rollback` restores it, `--forget-previous` deletes it.
6. A running proxy keeps the old token until a **controlled restart**. The script offers one, or you
   can pass `--restart`; a foreground proxy is stopped so you can start it again.

The proxy master key is separate. Rotate it with `./gateway.sh rotate-master-key`, then relaunch
Claude Code.

## 6. Change Region or model

```bash
./gateway.sh set-region us-east-1                     # offers us.moonshotai.kimi-k3 (asks first)
./gateway.sh set-region eu-west-1 --model-id global.moonshotai.kimi-k3
```

The new Region and profile pair is validated against AWS's published availability, then checked
with a live request before it's saved. A backup is kept. Other settings (`model_alias`, `proxy_port`,
`bedrock_endpoint_url` for VPC endpoints, `claude.extra_args`, `claude.disable_structured_outputs`)
live in `settings.json`. Validate them with `./gateway.sh config validate`, then restart.

| Source Region | Kimi K3 profile | Classifier profile (optional) |
|---|---|---|
| ap-south-1, ap-south-2 | `in.moonshotai.kimi-k3` | `in.anthropic.claude-sonnet-5` |
| us-east-1/2, us-west-1/2, ca-central-1, ca-west-1 | `us.moonshotai.kimi-k3` | `us.anthropic.claude-sonnet-5` |
| any other commercial Region in the model card | `global.moonshotai.kimi-k3` | `eu.`/`au.`/`global.anthropic.claude-sonnet-5` |

### Latency and reasoning effort

Kimi K3 reasons at **`max`** effort unless told otherwise, and Claude Code asks for `high`.
Measured on 2026-10-19 (`in.moonshotai.kimi-k3` from ap-south-1, prompt "hello"):

| Bedrock API | Effort | Time | Notes |
|---|---|---|---|
| Converse (old route) | default | 29.5 s | Converse **ignores** `reasoning_effort` for Kimi K3 (AWS documents this) |
| Converse | `none` | 12.1 s | still reasoned |
| Chat Completions (new route) | default (`max`) | 21.2 s | 1151 characters of reasoning |
| Chat Completions | **`low`** (new default) | **2.1 s**, first token 0.8 s | 72 characters of reasoning |
| Chat Completions | `none` | 6.6 s | no reasoning |

The proxy's own overhead is negligible next to these numbers, so rewriting it in another language
wouldn't help. What matters is the effort level and the Bedrock API. The gateway therefore:

* reaches Kimi through Bedrock's **Chat Completions** API, which AWS recommends for this model and
  which honours `reasoning_effort` (`"kimi_api": "chat_completions"`; `"converse"` remains available);
* sends **`reasoning_effort: low`** on every Kimi request (`"kimi_reasoning_effort"` in
  `settings.json`). Set it to `none`, `medium`, `high`, `xhigh` or `max`, or to `client` to honour
  Claude Code's `/effort` setting instead. Note that Claude Code sends `high` unless you change it.
  Lower effort answers faster but can be less thorough on hard tasks; restart the gateway after
  changing it.

**Prompt caching (`"kimi_prompt_cache": true`, the default).** Every Claude Code request
carries about 16.7K prompt tokens (tool definitions plus system prompt). The gateway makes Bedrock
cache them:

* It adds Bedrock's explicit `prompt_cache_breakpoint` marker at the end of the system prompt and on
  the latest user message. Bedrock ignores `cache_control` for Kimi K3 (tested), and LiteLLM drops
  Claude Code's own markers for non-Claude models.
* It replaces LiteLLM's per-session `prompt_cache_key` with a hash of model, tools and system
  prompt, so new sessions reuse the cache.
* The launcher sets `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, removing the per-conversation fingerprint
  that would otherwise sit at the very start of every prompt.

Measured live with real Claude Code: from the second session on, **16,727 of 16,734 prompt tokens
were read from cache**. Cache reads bill at about one tenth of the input rate; writes at 1.25×.

**Bedrock queueing varies a lot and dominates sometimes.** Even with a full cache hit, some
requests took 65–97 s, one upstream request sat for 368 s before Claude Code retried, and Bedrock
returned intermittent `internal_server_error`s, which Claude Code retries. This happened on **both**
profiles:

| Claude Code "hello", sessions 1/2/3 | India `in.` | Global `global.` |
|---|---|---|
| wall time | 5.4 s / 7.9 s / 96.9 s | 4.2 s / 10.9 s / 64.8 s |

Global wasn't consistently faster in these samples, and it routes outside India. If data
residency allows, try it with
`./gateway.sh set-region ap-south-1 --model-id global.moonshotai.kimi-k3`. Bedrock's `priority`
service tier (1.75× the price) is untested here.

Implementation notes (verified live and covered by tests):

* LiteLLM 1.106.0 silently falls back to Converse when a Chat Completions request carries tools,
  unless the model is flagged for "tools with reasoning". Kimi K3 isn't flagged, although AWS serves
  it correctly (verified live). The gateway sets the flag for the configured Kimi model only.
* On Chat Completions, Kimi K3 still rejects regex look-around in tool schemas (the same error as
  before, verified live), and LiteLLM only removes those patterns on Converse. The gateway adapter
  removes them on this route.
* Replaying earlier `reasoning_content` fails on Chat Completions too (`internal_server_error`,
  verified live), so earlier reasoning is still stripped.

## 7. Auto Mode

### What was verified (Claude Code 2.1.303, macOS arm64)

These findings come from debug logs and captured requests, not from what the permission selector
displays:

1. **Gate:** Claude Code treats the unknown model `kimi-k3` as a current model
   (`modelSupported=true canEnterAuto=true`), so the selector offers Auto. That alone proves nothing.
2. **Server-side review:** in Auto Mode Claude Code first asks the server to review actions, adding a
   `safeguards` field and `dangerous-tool-use`/`afk-mode` beta headers to the *Kimi* request. Only
   Anthropic's API can answer that. Behind this gateway the result is `server_no_result`, and Claude
   Code falls back to its own classifier requests. The launcher sets `CLAUDE_CODE_AUTO_MODE_SERVER=0`
   to skip the doomed attempt.
3. **Classifier routing:** Claude Code's own classifier requests go to the **same
   `ANTHROPIC_BASE_URL`**, with `model: "claude-sonnet-5"`, no tools, a "security monitor" system
   prompt and `stop_sequences: ["</block>"]`. There is no separate classifier endpoint setting.
   `ANTHROPIC_DEFAULT_SONNET_MODEL` changes the model ID the classifier requests.
4. **Dangerous fallback:** if `claude-sonnet-5` fails (for example 404), Claude Code logs
   `Got error trying Sonnet 5 as auto mode classifier, using kimi-k3` and **re-sends the classifier
   prompt to the session model, Kimi K3**. Setting `ANTHROPIC_DEFAULT_SONNET_MODEL=kimi-k3` does the
   same thing immediately.
5. Kimi K3 is **not** a supported classifier. The official docs list only Claude Sonnet 5 or later,
   Opus 4.7 or later, Haiku 5.5 and Fable for Bedrock and gateways
   ([permission modes](https://code.claude.com/docs/en/permission-modes)).

### What the gateway does about it

* **No classifier configured (the default):** the launcher starts Claude Code with
  `--settings '{"disableAutoMode":"disable"}'`, which is process-scoped. Auto Mode is removed from
  the mode cycle, and `--permission-mode auto` falls back to Manual (verified:
  `disabledBySettings=true canEnterAuto=false`, and no classifier requests are sent). Use
  **Manual** or **Accept edits**, or the supported alternative: **`/sandbox` in auto-allow mode**,
  where sandboxed Bash commands run without prompts.
* **Classifier configured** (`./gateway.sh classifier enable`): the gateway serves `claude-sonnet-5`
  from a real Claude Sonnet 5. Use Bedrock's `in.`/`us.`/`eu.`/`au.`/`global.anthropic.claude-sonnet-5`,
  with the same Bedrock key or `--separate-credential`, or the Anthropic API with a separate key
  (`--backend anthropic`). Setup checks that the account can actually invoke it. Each launch sends a
  classifier-shaped probe through the gateway, and Auto Mode stays disabled for that session if the
  probe fails.
* **Defense in depth, enforced in the gateway** (`gateway/kimi_gateway_hooks.py`):
  * any classifier-shaped request addressed to `kimi-k3` is **refused with 403**, so Claude Code
    blocks the action: it fails closed and nothing runs unreviewed;
  * the `claude-sonnet-5` route **accepts only classifier-shaped requests**, so coding traffic can't
    silently move from Kimi to Claude.

### Test results (offline, real Claude Code binary, mocked Bedrock: `tests/test_e2e_mock.py`)

| Scenario | Outcome |
|---|---|
| No classifier, `--permission-mode auto`, Kimi proposes `python3 -c "print(6*7)"` | Auto Mode disabled; action not auto-approved; **0** classifier requests |
| Classifier on Sonnet 5 (mock verdict `<block>no</block>`) | Classifier evaluated on `in.anthropic.claude-sonnet-5` (stages `xml_s1`), command **ran** |
| Classifier blocks (mock verdict `<block>yes</block>…`) | Both stages evaluated; Claude Code logged `Auto mode classifier blocked action: [Irreversible Local Destruction]…`; command **denied** |
| Classifier model unavailable after launch (404) | Claude Code fell back to `kimi-k3`; gateway **refused**; action denied (fail closed); Kimi received **0** classifier prompts |

Not every command reaches the classifier. Read-only commands (`echo`, `ls`) and edits or removals
inside the working directory are auto-allowed by Claude Code itself, as documented. Use a command
like `python3 -c "print(1)"` to see a classifier decision. For a live check, run Claude Code with
`--debug-file /tmp/cc.log` and look for `classifier_request_started … model=claude-sonnet-5`.

To configure Auto Mode with a real classifier:

```bash
./gateway.sh classifier enable                          # Bedrock Claude Sonnet 5, same Bedrock key
./gateway.sh classifier enable --separate-credential    # Bedrock, separate key
./gateway.sh classifier enable --backend anthropic      # Anthropic API key (stored separately)
./gateway.sh classifier status | disable
```

Classifier calls are billed: each reviewed action sends part of the transcript to the classifier model.

## 8. Troubleshooting: authentication and model access

Run `./gateway.sh doctor --live` first. Errors are classified as follows:

| Code / message | Meaning | Fix |
|---|---|---|
| `invalid_token`: *Authentication failed … API Key is valid* | wrong, expired or revoked Bedrock key (short-term keys last ≤ 12 h) | `./update-bedrock-key.sh --restart` |
| `model_access_denied`: *don't have access to the model* | key works, but IAM/SCP denies the profile or a destination Region | allow `bedrock:InvokeModel*` on the inference profile **and** the foundation model in Mumbai and Hyderabad (for `in.`) |
| `unsupported_model_or_region` / `model_not_found` | profile can't be called from this Region | see the table in §6; `./gateway.sh set-region …` |
| `throttled` | quota exceeded | wait, or request a quota increase in Service Quotas |
| `network_error` | can't reach `bedrock-runtime.<region>.amazonaws.com` | check `HTTPS_PROXY`, firewall and DNS |
| `port_in_use` | another program holds 127.0.0.1:4000 | stop it, or change `proxy_port` |
| `foreign_listener` | a different LiteLLM/proxy answers on the port | stop it; the gateway won't use it |
| `no_keyring` (Linux) | no Secret Service | see "Credential storage on headless Ubuntu" |
| `claude_missing` | Claude Code not installed | see §1 |
| Claude Code shows *401*, or *400 No connected db* | Claude Code is using a different key (LiteLLM looks up unknown `sk-…` keys in a database it doesn't have, hence the odd message) | always launch with `claude-kimi`; check `ANTHROPIC_API_KEY` isn't forced elsewhere |
| *auto mode cannot determine the safety …* / `kgw_classifier_refused` in logs | classifier unreachable, so the gateway kept it off Kimi | `./gateway.sh classifier status`; fix model access, or use Manual mode |

**A proxy that started doesn't prove the model works.** `status` and `health` show liveness and
routing only. `health --live`, `doctor --live` and setup's live test send a real request.

## 9. Troubleshooting the structured-output regex error

```
structured output schema uses unsupported regex negative look-ahead;
replace look-around assertions with a regular expression using supported constructs
```

**Root cause.** Kimi K3 on Bedrock compiles **tool input schemas** into a constrained-decoding
grammar, and the grammar engine rejects regex look-around (`(?=`, `(?!`, `(?<=`, `(?<!`). The
offending `pattern` sits in a tool's `input_schema`, typically from an MCP server. Claude Code
2.1.303's built-in tools contain no look-around patterns; this was checked against captured
requests and the binary. It does *not* come from Claude Code's `output_config.format`. That's why
`CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1` didn't help: that variable only removes
`output_config.format`. LiteLLM **1.105.1** forwarded tool schemas unchanged.

**Fix.**

1. **LiteLLM 1.106.0** (stable, 2026-10-18) marks `in./us./global.moonshotai.kimi-k3` with
   `supports_regex_lookaround: false` and drops only the look-around `pattern` keywords from tool
   schemas on the Converse path (upstream PR #44138). This was verified in the source and end to end
   against the mock.
2. The bundled adapter (`gateway/kimi_gateway_hooks.py`) applies the same LiteLLM helper to tool
   schemas on the **Chat Completions** route (the default), which rejects look-around too (verified
   live), and to **structured-output** schemas (`output_config.format`, `response_format`), which
   LiteLLM 1.106.0 forwards unchanged. It acts only on the Kimi route, removes only look-around `pattern`
   keywords and keeps every other constraint, and it logs tool names, never schema contents.
   Claude Code and the MCP server still validate tool inputs before execution, so the dropped regex
   still constrains what actually runs. Covered by `tests/test_hooks.py`.

**Another Kimi K3 incompatibility fixed by the same adapter.** AWS documents that Kimi K3 on
Converse fails with `InternalServerException` when **reasoning from earlier turns** is sent back,
and Claude Code always replays thinking blocks. The remedy AWS documents is to remove them, so the
adapter strips `thinking`/`redacted_thinking` blocks from earlier assistant turns on the Kimi route
only. New responses still include reasoning. Without this, every second turn of a Claude Code
session would fail.

If you still see the regex error: `./gateway.sh version` must report LiteLLM ≥ 1.106.0, and
`./gateway.sh logs` should show `kimi adapter: dropped look-around regex patterns from tool:<name>`.

## 10. Updating dependencies safely

Versions are pinned in `requirements.txt`. Development tools are in `requirements-dev.txt`.
Experimental options are documented separately in `requirements-experimental.txt` and are **never
installed by default**.

```bash
./gateway.sh upgrade --litellm 1.107.0          # stable releases only, unless --allow-prerelease
./gateway.sh upgrade --rollback                  # return to the previous environment
```

`upgrade` works like this:

1. Backs up your settings.
2. Builds a **separate staging venv** with the new version.
3. Checks that the version still drops look-around for your Kimi profile.
4. Runs the unit tests against the staging venv.
5. Only then swaps it in, keeping the old venv as `.venv.previous`.

Afterwards run `./gateway.sh health --live`. Re-running setup reinstalls automatically when the
pinned versions change.

## 11. Uninstall

```bash
./uninstall.sh            # macOS / Ubuntu
.\uninstall.ps1           # Windows
./uninstall.sh --remove-venv --remove-config --remove-credentials --yes   # everything, no prompts
```

Uninstall explains what it will remove and asks before each optional deletion. It stops the gateway
only if the running process is verifiably this gateway, and always removes the generated runtime
files (rendered config, PID file, logs). It deletes the venv, settings/backups and **credentials only
on request**; credentials are kept by default. Every deletion is restricted to the gateway's private
directory and the repo's own `.venv*` folders, and it never deletes the repository, your home folder
or anything else.

## 12. Security considerations

* **No secrets in Git.** `.gitignore` covers `.env*`, `secrets.json`, `settings.json`, rendered configs
  and logs. `./gateway.sh scan-secrets` scans tracked and untracked files **locally**, for credential
  patterns and for exact matches with your stored secrets. It uploads nothing and prints no values.
* **No secrets in output.** Every message passes through a redactor that knows the stored values and
  credential shapes: `ABSK…`, `bedrock-api-key-…`, `sk-…`, `AKIA…`, `Bearer …`. Only 8-character
  SHA-256 fingerprints are shown. LiteLLM runs with `turn_off_message_logging` and
  `redact_user_api_key_info`, and without `--detailed_debug`.
* **No secrets on command lines.** Secrets reach the proxy only through its process environment.
  Claude Code receives only the local master key; the Bedrock token is explicitly unset for it.
* **Separate keys.** The local master key is never the Bedrock token; reuse is rejected. Rotate the
  key with `rotate-master-key`, and update or roll back the token with `update-bedrock-key`.
* **Local only.** The proxy binds to 127.0.0.1 by default. LiteLLM's admin UI is disabled
  (`DISABLE_ADMIN_UI`), no database is used, and the bundled model map is used instead of a runtime
  download (`LITELLM_LOCAL_MODEL_COST_MAP`).
* **Credential validation goes only to AWS** (or to the Anthropic API for that classifier backend).
  Redirects are never followed with credentials attached.
* **No system-wide changes.** No `sudo`, no administrator rights, no execution-policy change, no
  PATH or global environment edits. Windows ACLs are set only on the gateway's own files.
* **Background mode** keeps credentials in the memory of a long-lived process owned by you. Prefer
  foreground mode on shared machines, and stop the gateway when you're done.
* **Auto Mode** classification never runs on Kimi K3 (§7). Without a Claude classifier, Auto Mode is
  disabled rather than weakened.

## 13. Supported versions, test status and known limitations

| Component | Version |
|---|---|
| LiteLLM | **1.106.0** (stable; 1.105.1 is affected by the regex issue) |
| Claude Code | **2.1.303** tested; ≥ 2.1.288 required |
| Python | 3.11 – 3.14 supported; **tested on 3.14.7** |
| keyring / psutil / prisma | 25.7.0 / 7.2.2 / 0.15.0 |
| Bedrock model | `in.moonshotai.kimi-k3` (also `us.`/`global.`); auth by Bedrock API key (bearer token) |

**What has actually been tested**

* ✅ **macOS 27 on Apple Silicon (arm64)**, Python 3.14.7. Covered: 107 unit tests, 11 offline
  end-to-end tests (real LiteLLM proxy, real Claude Code 2.1.303, mocked Bedrock that reproduces Kimi
  K3's documented failures), `shellcheck` (clean), and PowerShell 7.6 parser checks on every `.ps1`.
  The live Bedrock result is reported separately in the project notes when it has been run.
* ⚠️ **Ubuntu: not tested on a real Ubuntu machine.** The scripts are POSIX/bash and pass
  shellcheck, and the Linux keyring path uses `keyring` with Secret Service. Expect to need
  `python3-venv`, and a keyring or `--credential-backend file` on headless hosts.
* ⚠️ **Windows 10/11: not tested on Windows.** The `.ps1` files parse under PowerShell 7.6, but
  Windows PowerShell 5.1, Credential Manager, `icacls` ACLs, `py.exe` discovery and detached
  background mode haven't been run. Please report problems.

**Known limitations**

* Kimi K3 is reached through Bedrock **Chat Completions** with `reasoning_effort: low` by default
  (see "Latency and reasoning effort"). Requests that need a Converse-only feature, such as a `stop`
  list or a `json_object` response format, are still sent through Converse by LiteLLM, without
  the reasoning-effort setting or the gateway's cache markers.
* Bedrock's queueing time for Kimi K3 is highly variable (0.8 s to minutes), independent of this
  gateway.
* Claude Code assumes a 200K context window for the unrecognized alias, although Kimi K3 supports 1M.
* `/model` choices other than the default aren't served: Sonnet maps to the classifier-only route,
  and others don't exist.
* The classifier-detection heuristic (system-prompt marker, `</block>` stop sequence, no tools, hint
  header) is verified against Claude Code 2.1.303 and may need updating for future versions. The
  failure mode is safe: an unrecognized classifier request on the Kimi route would be served by
  Kimi, but only after Sonnet 5 failed, and the launcher's per-session probe disables Auto Mode when
  the classifier route is down.
* The fast-mode availability check and WebFetch domain check call `api.anthropic.com` directly
  (Claude Code behavior), and may report errors on networks that block it.

## 14. Repository layout and development

```
config.yaml                    LiteLLM config template (placeholders + os.environ refs only)
requirements*.txt              pinned runtime / dev / experimental dependencies
setup.{sh,ps1}                 one-command install (bootstrap venv → manage.py setup)
start.{sh,ps1}                 start proxy (foreground; --background)
gateway.{sh,ps1}               all management commands (status, stop, restart, logs, doctor, …)
claude-kimi.{sh,ps1}           launch Claude Code through the gateway
update-bedrock-key.{sh,ps1}    rotate the Bedrock token
uninstall.{sh,ps1}             cleanup
scripts/common.py              settings, validation, secret stores, redaction, platform, processes (stdlib only)
scripts/validate.py            Bedrock / proxy / Claude Code probes and error classification
scripts/manage.py              the CLI every wrapper calls
scripts/lib.{sh,ps1}           wrapper helpers (Python discovery, venv lookup)
scripts/claude-code-env.ps1    dot-source to configure the current PowerShell session
gateway/kimi_gateway_hooks.py  LiteLLM pre-call hook: Kimi adapter + classifier guard
tests/                         unit, offline e2e (mock_bedrock.py), opt-in live tests
```

The shared Python manager carries all business logic; the `.sh` and `.ps1` files only find Python
and forward arguments. This differs from the suggested layout, which had no `manage.py` or wrapper
helpers: a single CLI keeps behavior identical across platforms.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -m "not e2e"     # unit tests (~4 s)
.venv/bin/python -m pytest -q -m e2e           # offline end-to-end incl. real Claude Code (~1 min)
KGW_LIVE_BEDROCK=1 .venv/bin/python -m pytest -m live   # OPTIONAL: real Bedrock, uses your stored key, billed
.venv/bin/shellcheck -x *.sh scripts/lib.sh
```
