# Kimi Bedrock Gateway

Use **Kimi K3 on Amazon Bedrock** as the model behind **Claude Code**, through a local
**LiteLLM** proxy, on Windows 10/11, Ubuntu and Apple Silicon macOS.

```
Claude Code ──(Anthropic Messages, ANTHROPIC_BASE_URL)──► LiteLLM proxy on 127.0.0.1:4000 ──(bearer token or SigV4)──► Bedrock: Kimi K3
   process-scoped env from the launcher                    + kimi_gateway_hooks adapter                              in.moonshotai.kimi-k3, global./us. profile,
                                                                                                                      or an admin's application-profile ARN
                                                            └─ optional classifier route ──────────────────────────► Bedrock Claude (profile or ARN), Anthropic API,
                                                                                                                      or (unsafe opt-in) Kimi K3 itself
```

* `kimi-k3` is the coding model for every Claude Code request: main loop, subagents, and the
  background/Haiku/Opus slots.
* Claude Code authenticates to the proxy with a randomly generated local **master key**. The proxy
  authenticates to Bedrock with either a **Bedrock API key** (bearer token) or an **IAM access key
  pair** (SigV4). All credentials are kept in the OS credential store, and none is ever written to a
  file in this repository.
* Kimi K3 can be reached through a standard inference profile or through an **application inference
  profile ARN** that an admin created.
* Setup is one-time. Afterwards `claude-kimi` starts the gateway on demand, and the gateway (and
  optionally Claude Code) can also **start automatically at login**.
* Auto Mode is designed around an independent Claude classifier. Without one it's disabled. Kimi K3
  can review its own actions only through an explicit, **unsafe opt-in** (see [Auto Mode](#7-auto-mode)).

**New here? Start with the [Quick start](#quick-start).**

---

## Contents

0. [Quick start](#quick-start): set up, daily use, command cheat sheet, settings reference
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

## Quick start

### Step 1: install the prerequisites

You need Python 3.11–3.14 and Claude Code ≥ 2.1.288. You don't need admin rights. See
[§1](#1-prerequisites) for details.

### Step 2: decide how the gateway authenticates to Bedrock

| You have | Use | Your IAM user needs |
|---|---|---|
| A **Bedrock API key** (`ABSK…` or `bedrock-api-key-…`) | `--auth bearer` (default) | `bedrock:InvokeModel*` on the Kimi profile and model, **and** `bedrock:CallWithBearerToken` |
| An **IAM access key ID + secret** (`AKIA…`) | `--auth access-key` | `bedrock:InvokeModel*` on the Kimi profile and model |

Then decide which Kimi K3 model ID to use:

| Your IAM policy allows | Model ID to pass |
|---|---|
| Standard inference profiles | nothing: the default is `in.moonshotai.kimi-k3` (or `us.`/`global.`, see §6) |
| Only an **application inference profile** (the resource ends in `:application-inference-profile/<id>`) | `--model-id arn:aws:bedrock:<region>:<account>:application-inference-profile/<id>` |

If you're not sure, ask your AWS admin for the IAM policy on your user. If Bedrock refuses a model,
`./gateway.sh doctor --live` explains why.

### Step 3: run setup once

```bash
# macOS / Ubuntu. Windows: .\setup.ps1 with the same options.

# Bedrock API key, default India profile:
./setup.sh

# IAM access keys + an admin's application inference profile, starting at every login:
./setup.sh --auth access-key \
  --model-id arn:aws:bedrock:ap-south-1:<account>:application-inference-profile/<id> \
  --autostart --open-claude-at-login
```

Setup asks for the credential with input hidden, checks it with one tiny request to Bedrock, and
stores it in the OS credential store. It then renders the LiteLLM config and runs a short live test
through the gateway. Re-running setup is safe and keeps what already works.

### Step 4: use it every day

```bash
./claude-kimi.sh          # Windows: .\claude-kimi.ps1
```

That's all. If the gateway isn't running, `claude-kimi` starts it in the background, or waits for
one that's already starting. Anything after the command goes to Claude Code, for example
`./claude-kimi.sh -p "explain this repo"` or `./claude-kimi.sh --resume`.

**Use it from any project, not just this repo.** The launcher finds the gateway's files from its own
location and doesn't change folders. Claude Code opens in **the folder you run it from**, with that
folder's permissions, `CLAUDE.md` and project settings, just like plain `claude`:

```bash
cd ~/code/my-project
~/Desktop/kimi-bedrock-gateway/claude-kimi.sh --permission-mode auto
```

To shorten that, add a shell alias once (zsh shown; use `~/.bashrc` for bash). Replace the path with
wherever you cloned this repo:

```bash
echo "alias claude-kimi='$HOME/Desktop/kimi-bedrock-gateway/claude-kimi.sh'" >> ~/.zshrc
source ~/.zshrc

cd ~/code/my-project
claude-kimi                           # or: claude-kimi --permission-mode auto
```

Use an alias, not a symlink in `~/.local/bin`. The script looks for its helper files next to its own
path, so a symlinked copy can't find them. On Windows, call
`& "$HOME\Desktop\kimi-bedrock-gateway\claude-kimi.ps1"` from your project folder, or add a function
to your PowerShell profile:
`function claude-kimi { & "$HOME\Desktop\kimi-bedrock-gateway\claude-kimi.ps1" @args }`.

To skip even that, enable autostart once (§3, "Start automatically at login"):

```bash
./gateway.sh autostart enable                                   # gateway at login
./gateway.sh autostart enable --open-claude --claude-workdir ~/code/my-project   # + Claude Code in a terminal
```

### Step 5 (optional): choose an Auto Mode reviewer

| Option | Command | Safety |
|---|---|---|
| None (default) | nothing | Auto Mode is disabled; use Manual, Accept edits or `/sandbox` |
| **Claude classifier** (recommended) | `./gateway.sh classifier enable` (Bedrock Sonnet 5), `--model-id <Claude profile or ARN>`, or `--backend anthropic` | independent reviewer, as Claude Code intends |
| Kimi K3 reviews itself | `./gateway.sh classifier enable --backend kimi --accept-risk` | **unsafe**: no independent check; see §7 |

Add `--restart` to apply it to a running gateway.

**Shift+Tab doesn't offer "auto"?** That's expected until you choose a reviewer. With none configured,
the launcher prints "Auto mode is DISABLED for this session" and starts Claude Code with Auto Mode
removed from the Shift+Tab cycle. Enable a reviewer, then quit and relaunch Claude Code; see
"Using Auto Mode with the opt-in" in §7.

### Step 6: check and fix

```bash
./gateway.sh doctor --live     # full diagnosis plus one real Bedrock request
./gateway.sh status            # running? healthy? which model, auth and reviewer? autostart?
./gateway.sh logs              # recent gateway log, secrets redacted
```

[§8](#8-troubleshooting-authentication-and-model-access) maps every error code to a fix.

### Command cheat sheet

On Windows, use the `.ps1` version of each script (`.\gateway.ps1 <command>`).

| Task | Command |
|---|---|
| Start / stop / restart the gateway | `./start.sh [--background]`, `./gateway.sh stop`, `./gateway.sh restart` |
| Launch Claude Code through the gateway | `./claude-kimi.sh [claude args…]` |
| Start at login (± Claude Code) | `./gateway.sh autostart enable [--open-claude] [--claude-workdir DIR]` / `disable` / `status` |
| Replace the Bedrock API key | `./update-bedrock-key.sh [--restart]` (`--rollback` restores the previous one) |
| Switch to / replace IAM access keys | `./gateway.sh set-auth access-key [--replace] [--restart]` |
| Switch back to the API key | `./gateway.sh set-auth bearer [--forget-access-key]` |
| Change Region or model / profile ARN | `./gateway.sh set-region <region> [--model-id <id or ARN>]` |
| Auto Mode reviewer | `./gateway.sh classifier enable [...]` / `disable` / `status` |
| Rotate the local master key | `./gateway.sh rotate-master-key` |
| Show or validate settings | `./gateway.sh config show` / `config validate` |
| Scan the repo for leaked secrets | `./gateway.sh scan-secrets` |
| Upgrade LiteLLM safely | `./gateway.sh upgrade [--rollback]` |
| Remove everything | `./uninstall.sh [--remove-venv --remove-config --remove-credentials]` |

### Settings reference

Everything persists in `settings.json`; see "Where things are stored" in §2. Prefer the commands
above, which validate before saving. If you edit the file by hand, run `./gateway.sh config validate`,
then `./gateway.sh restart`.

| Setting | Default | Meaning |
|---|---|---|
| `aws_region` | `ap-south-1` | Bedrock Region the gateway calls |
| `bedrock_model_id` | `in.moonshotai.kimi-k3` | Kimi K3 profile ID, or an application-profile ARN |
| `kimi_api` | `chat_completions` | Bedrock API for Kimi; set to `converse` automatically for an ARN |
| `kimi_reasoning_effort` | `low` | `none`…`max`, or `client` (Chat Completions only) |
| `kimi_prompt_cache` | `true` | explicit Bedrock cache markers (Chat Completions only) |
| `aws_auth` | `bearer` | `bearer` (Bedrock API key) or `access_key` (IAM key pair, SigV4) |
| `credential_backend` | `keyring` | `keyring` (OS store) or `file` (0600 `secrets.json`) |
| `start_gateway_on_launch` | `true` | `claude-kimi` starts the gateway when it isn't running |
| `model_alias` | `kimi-k3` | model name Claude Code uses for Kimi |
| `proxy_host` / `proxy_port` | `127.0.0.1` / `4000` | where the gateway listens |
| `classifier.backend` | `none` | `none`, `bedrock`, `anthropic`, or `kimi` (unsafe opt-in) |
| `classifier.bedrock_model_id` | per Region | Claude profile ID or application-profile ARN |
| `classifier.base_model` | `anthropic.claude-sonnet-5` | for a classifier ARN: the Claude model behind it |
| `classifier.accept_kimi_self_review` | `false` | must be `true` for `classifier.backend = kimi` |
| `bedrock_endpoint_url` | `null` | VPC endpoint override |

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

* **Either** a Bedrock API key, created in the console under *Amazon Bedrock → API keys* (long-term
  keys start with `ABSK…`; short-term keys start with `bedrock-api-key-…` and expire within 12
  hours), **or** an IAM user access key pair (`AKIA…` plus a secret; see §5).
* The IAM identity must be allowed to call `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream` on the inference profile (or application profile ARN), and
  on the foundation model in each destination Region. A Bedrock API key additionally needs
  `bedrock:CallWithBearerToken`.

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
| Bedrock token or AWS access key pair, master key, classifier keys | **Windows Credential Manager** / **macOS Keychain** / **Secret Service (libsecret)**, service name `kimi-bedrock-gateway`. Optional fallback: a user-only `secrets.json` file (see below) |
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

### Start automatically at login

Settings and credentials already persist (`settings.json` and the OS credential store), so setup is a
one-time step. To avoid starting the gateway by hand:

```bash
./gateway.sh autostart enable                      # gateway starts at every login
./gateway.sh autostart enable --open-claude        # ...and Claude Code (Kimi K3) opens in a terminal
./gateway.sh autostart enable --open-claude --claude-workdir ~/code/my-project
./gateway.sh autostart status
./gateway.sh autostart disable                     # removes the login items; uninstall does too
# or during setup: ./setup.sh --autostart [--open-claude-at-login]
```

Separately, `claude-kimi` **starts the gateway in the background whenever it isn't running**. If one
is already starting, for example from the login item, it waits for that one instead. So even without
autostart there's no separate start step. To get the old "ask first" behaviour, set
`"start_gateway_on_launch": false`.

| Platform | Login item (per user, no admin rights) |
|---|---|
| macOS | `~/Library/LaunchAgents/com.kimi-bedrock-gateway.plist` runs `manage.py start --background` at login, in the GUI session where the Keychain is unlocked. With `--open-claude`, `com.kimi-bedrock-gateway.claude.plist` opens Terminal on a generated `claude-kimi.command`. |
| Ubuntu | XDG autostart entries in `~/.config/autostart/`, which run inside the desktop session after GNOME Keyring unlocks. The Claude entry uses `Terminal=true`. |
| Windows | `.cmd` files in the user's Startup folder. The gateway runs via `pythonw` with no console; Claude Code opens with `powershell -NoExit -ExecutionPolicy Bypass -File claude-kimi.ps1`, a per-process flag that leaves the machine's execution policy unchanged. |

Notes:
- **Stopping:** the login item only starts the gateway; it doesn't restart it. After `gateway stop` it
  stays stopped until the next login, or the next `claude-kimi`.
- **Headless servers:** XDG autostart needs a desktop login, so it doesn't run on a server with no
  graphical session. Use `start.sh --background` from your own service manager instead.
- **Separate installs:** a non-default `KGW_HOME` gets its own login-item label and its own credential
  store name (`kimi-bedrock-gateway@<hash>`). A second install never reads, overwrites or deletes
  the default install's credentials or login items.

Verified live on macOS (2026-10-26) with a scratch install. Its login item was written to a scratch
folder and loaded into launchd under its own label:
- launchd started the gateway, and it read the access key pair from the Keychain without a prompt.
- In a simulated login, the gateway item and the Claude Code `.command` started at the same moment.
  The launcher waited for the starting gateway, only one gateway ran, and Claude Code (2.1.310) answered
  19 s after "login". Most of that time was gateway startup.
- `uninstall` unloaded the item and deleted the files and Keychain entries.

Not exercised: the Terminal window opened by the Claude Code login item (its `.command` was run
directly instead), a real reboot, and the Ubuntu and Windows login items. The Ubuntu and Windows
items are only covered by offline tests of the generated files.

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

### Using an IAM access key pair instead of a Bedrock API key

If your account can't use Bedrock API keys, the gateway can sign requests with an IAM user's access
key ID and secret access key instead (AWS Signature Version 4). One case is an IAM user without
`bedrock:CallWithBearerToken`. This mode doesn't need that permission.

```bash
./gateway.sh set-auth access-key --restart     # prompts for the key ID and secret (hidden)
./gateway.sh doctor --live
./gateway.sh set-auth bearer                   # switch back; --forget-access-key deletes the pair
# or at install time:
./setup.sh --auth access-key
```

- The key ID and secret are stored like the bearer token: in the OS credential store, or the 0600
  file fallback. They're validated with one signed request before they're saved, and a pair that
  fails validation is not saved.
- The LiteLLM config refers to them only as `os.environ/KGW_AWS_ACCESS_KEY_ID` and
  `os.environ/KGW_AWS_SECRET_ACCESS_KEY`. Output shows only the last four characters of the key ID
  and a fingerprint of the secret.
- The auto-mode classifier route uses the same pair unless it has a separate credential.
- Only long-term IAM user keys (`AKIA…`) are supported. Temporary credentials (`ASIA…` plus a
  session token) and AWS CLI profiles or SSO are not.
- Least privilege: the user needs only `bedrock:InvokeModel*` on the Kimi profile and its foundation
  model. A long-lived secret key is a stronger credential than a Bedrock API key that's scoped to
  Bedrock, so rotate it regularly.

Verified live on 2026-10-26: setup with `--auth access-key` validated the pair, `doctor --live` passed,
and Claude Code 2.1.310 through the gateway answered "hello" and ran a Bash tool call. The target was an
application inference profile in ap-south-1. Neither key value appeared in the logs or the rendered
config. Offline, the mock Bedrock re-verifies every SigV4 signature (the manager's own signer and LiteLLM's).

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

### Application inference profiles (admin-managed ARNs)

Some organisations only allow Kimi K3 through an *application inference profile* their admin
created. You can recognise one in your IAM policy: the resource looks like
`arn:aws:bedrock:<region>:<account>:application-inference-profile/<id>`, and the foundation-model
statement has a `bedrock:InferenceProfileArn` condition. With a policy like that, `in.`, `global.`
and the bare model ID are all denied, so use the ARN as the model ID:

```bash
./setup.sh --model-id arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123
# or, on an existing install:
./gateway.sh set-region ap-south-1 --model-id arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123
```

The gateway then switches `kimi_api` to `converse`, because Bedrock's Chat Completions API rejects
application profiles ("Application inference profiles are not supported by this API"). Going back to
an `in.`/`global.` profile switches it back to Chat Completions. On Converse,
`kimi_reasoning_effort` has no reliable effect and the explicit `prompt_cache_breakpoint` markers
aren't used. Bedrock still caches repeated prefixes on its own: in a live test, 10,218 of 10,339
prompt tokens were served from cache by the third request. The gateway also strips Claude Code's
`cache_control` markers on this route. LiteLLM can't tell that the ARN is Kimi, so it would turn them
into `cachePoint` fields, which Kimi rejects.

**Bearer keys also need `bedrock:CallWithBearerToken`.** A Bedrock API key acts as its IAM user,
and that user needs this permission in addition to `bedrock:InvokeModel*`. The
`AmazonBedrockLimitedAccess` managed policy includes it. Without it, every call fails with
`bearer_token_not_allowed`.

Verified live on 2026-10-26 with Claude Code 2.1.303 against a real application profile in
ap-south-1: a "hello" took 2.8 s, and a Bash tool call ran and returned its output. These requests
were signed with the same IAM user's access keys, because the API key under test still lacked
`bedrock:CallWithBearerToken`. The bearer-token path through an ARN is covered only by the offline tests.

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
* **Unsafe opt-in** (`./gateway.sh classifier enable --backend kimi --accept-risk`): Kimi K3 serves
  the classifier slot itself. See "Opt-in: Kimi K3 reviews its own actions" below.
* **Defense in depth, enforced in the gateway** (`gateway/kimi_gateway_hooks.py`):
  * any classifier-shaped request addressed to `kimi-k3` is **refused with 403**, so Claude Code
    blocks the action: it fails closed and nothing runs unreviewed;
  * the `claude-sonnet-5` route **accepts only classifier-shaped requests**, so coding traffic can't
    silently move from Kimi to Claude.

### Classifier behind an application inference profile

If your IAM policy only allows models through application inference profiles (see §6), the
classifier needs one of its own. It must point at a **Claude** model, separate from the Kimi profile:

```bash
./gateway.sh classifier enable --model-id arn:aws:bedrock:ap-south-1:<account>:application-inference-profile/<claude-id> --restart
# if the profile points at a Claude model other than Sonnet 5:
#   ... --base-model anthropic.claude-opus-4-7
```

An ARN doesn't say which model is behind it, so the gateway renders the route with the Claude model
for request formatting and the ARN as the target:

```yaml
  - model_name: "claude-sonnet-5"          # what Claude Code requests for the classifier
    litellm_params:
      model: "bedrock/anthropic.claude-sonnet-5"   # classifier.base_model
      model_id: "arn:aws:bedrock:...:application-inference-profile/<claude-id>"
```

Setup refuses a `base_model` that isn't Claude, a profile in another Region, and a classifier ARN that
equals the Kimi ARN. Before saving, `classifier enable` sends one real request to the profile; if it
fails, Auto Mode stays off. The route uses the gateway's credential (bearer token or access key pair).

**What an admin has to provide.** This only works if the IAM user may invoke a Claude model. For
example, an admin can copy the system profile into an application profile:

```bash
aws bedrock create-inference-profile --region ap-south-1 --inference-profile-name kgw-classifier \
  --model-source copyFrom=arn:aws:bedrock:ap-south-1:<account>:inference-profile/in.anthropic.claude-sonnet-5
```

They then allow it the same way as the Kimi profile: `bedrock:InvokeModel*` on the new profile ARN,
plus the foundation model `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5` with the
condition `bedrock:InferenceProfileArn` set to the new ARN. Anthropic models may also require the
account's one-time Anthropic use-case form.

Offline test (real Claude Code 2.1.310, `--permission-mode auto`, mocked Bedrock): Kimi and the
classifier were each behind their own ARN, and every request was SigV4-signed with an access key pair.
The harmless command was approved, and every classifier call went to the Claude ARN, none to Kimi.
**Not yet verified against real Bedrock**, because the account this was built for had no Claude access.

### Opt-in: Kimi K3 reviews its own actions (unsafe)

If you can't get a Claude classifier, you can let Kimi K3 answer Claude Code's Auto Mode safety checks:

```bash
./gateway.sh classifier enable --backend kimi --accept-risk --restart
./gateway.sh classifier disable --restart   # undo
```

#### Using Auto Mode with the opt-in

1. Quit Claude Code if it's running (`/exit`, or Ctrl+C twice). The launcher decides Auto Mode
   availability once per launch, so an open session doesn't pick up the change.
2. From the gateway folder, enable the opt-in:
   ```bash
   cd ~/Desktop/kimi-bedrock-gateway
   ./gateway.sh classifier enable --backend kimi --accept-risk --restart
   ```
   It prints a warning, then
   `Auto-mode classifier route enabled: 'claude-sonnet-5' -> Kimi K3 … [UNSAFE opt-in: self-review]`.
3. Launch from your project:
   ```bash
   cd ~/code/my-project
   claude-kimi                              # or: claude-kimi --permission-mode auto
   ```
   Before Claude Code opens, the launcher prints
   `Auto mode is available, but its reviewer is Kimi K3 itself (UNSAFE opt-in …)`.
4. Shift+Tab now cycles through the modes, including **auto**. `--permission-mode auto` starts in it
   directly.

**If the launcher still says "Auto mode is DISABLED for this session"**, check the line just above it:

| Launcher says | Fix |
|---|---|
| `Classifier route check failed: …` | The gateway couldn't reach Kimi for the safety check. Run `./gateway.sh doctor --live`, fix what it reports, then `./gateway.sh restart` |
| `… the running gateway does not serve it; restart the gateway` | The gateway is still running the old config: `./gateway.sh restart`, then relaunch |
| `no supported Claude classifier is configured` | The opt-in isn't saved. `./gateway.sh classifier status` should say `Kimi K3 reviewing its own actions (UNSAFE opt-in)`; if not, rerun step 2 |

Not every command gets a review. Claude Code approves read-only commands (`echo`, `ls`) and edits
inside the project folder by itself, so Kimi only judges the rest. Each review is logged in
`./gateway.sh logs` as `classifier request served by Kimi K3`.

**Understand what this means first.** The model that proposes a command also approves it, so this is
**not an independent safety check**. A prompt injection (in a file, web page, issue or tool output) that
steers Kimi into a dangerous command can steer its approval too. Claude Code's Auto Mode is designed
around a separate Claude classifier, and Kimi K3 is not on Claude Code's list of supported classifiers.
Prefer a Claude classifier, Manual or Accept-edits mode, or `/sandbox`.

How it works, compared with what a plain LiteLLM setup does by accident:
- **A plain setup** has no `claude-sonnet-5` route. Every safety check fails first, and Claude Code then
  silently retries it on the session model, Kimi. That means two requests per check, plus error noise.
- **This opt-in** renders an explicit `claude-sonnet-5` route that is served by your Kimi model. Each check
  is one call, and the route still accepts *only* classifier-shaped requests.
- The `kimi-k3` coding route keeps refusing safety checks, and the launcher's Sonnet setting stays on
  the classifier route.
- The gateway removes the `</block>` stop sequence, because Kimi K3 rejects stop sequences ("This model
  doesn't support the stopSequences field", verified live). Claude Code still parses the verdict.
- It needs `--accept-risk`, or an explicit "yes" at an interactive prompt. Every launch prints a warning,
  and `status`, `doctor` and `config validate` show it as **UNSAFE**.

Verified live on 2026-10-26 with Claude Code 2.1.310 in `--permission-mode auto`, through an
application-profile ARN with access-key auth:
- **Harmless command:** a `python3 -c` command that writes and reads a scratch file went to Kimi for
  review. It was approved and ran.
- **Gateway log:** no "Invalid model name" and no stop-sequence errors.
- **Not tested:** a case where Kimi *should* block a command, deliberately, because that would mean
  asking for a genuinely risky command. The offline tests cover allow and block verdicts.

### Test results (offline, real Claude Code binary, mocked Bedrock: `tests/test_e2e_mock.py`)

| Scenario | Outcome |
|---|---|
| No classifier, `--permission-mode auto`, Kimi proposes `python3 -c "print(6*7)"` | Auto Mode disabled; action not auto-approved; **0** classifier requests |
| Classifier on Sonnet 5 (mock verdict `<block>no</block>`) | Classifier evaluated on `in.anthropic.claude-sonnet-5` (stages `xml_s1`), command **ran** |
| Classifier blocks (mock verdict `<block>yes</block>…`) | Both stages evaluated; Claude Code logged `Auto mode classifier blocked action: [Irreversible Local Destruction]…`; command **denied** |
| Classifier model unavailable after launch (404) | Claude Code fell back to `kimi-k3`; gateway **refused**; action denied (fail closed); Kimi received **0** classifier prompts |
| Kimi and classifier each behind an application-profile ARN, SigV4 auth | every classifier call went to the Claude ARN, every request was signed and verified; command **ran** |
| Opt-in `--backend kimi` | every check went to the Kimi model through the `claude-sonnet-5` route, with no stop sequences and no failed first attempt; command **ran** |
| Opt-in without `--accept-risk` | refused; settings unchanged |

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
| `model_access_denied`: *not authorized to perform: bedrock:InvokeModel* (Chat Completions reports this as HTTP 401) | the IAM policy doesn't allow this model ID, often because it only allows an application inference profile | use the profile ARN as the model ID (§6, "Application inference profiles") |
| `bearer_token_not_allowed` | the API key's IAM user lacks `bedrock:CallWithBearerToken` | ask your admin to add it (it's in `AmazonBedrockLimitedAccess`), or use an access key pair: `./gateway.sh set-auth access-key` |
| `invalid_access_key`: *signature we calculated does not match* / *security token … invalid* | wrong or deactivated access key ID or secret | `./gateway.sh set-auth access-key --replace` |
| `unsupported_api`: *Application inference profiles are not supported by this API* | an ARN with `kimi_api: chat_completions` | set `"kimi_api": "converse"`, or re-run `set-region … --model-id <ARN>` |
| `unsupported_model_or_region` / `model_not_found` | profile can't be called from this Region | see the table in §6; `./gateway.sh set-region …` |
| `throttled` | quota exceeded | wait, or request a quota increase in Service Quotas |
| `network_error` | can't reach `bedrock-runtime.<region>.amazonaws.com` | check `HTTPS_PROXY`, firewall and DNS |
| `port_in_use` | another program holds 127.0.0.1:4000 | stop it, or change `proxy_port` |
| `foreign_listener` | a different LiteLLM/proxy answers on the port | stop it; the gateway won't use it |
| `no_keyring` (Linux) | no Secret Service | see "Credential storage on headless Ubuntu" |
| `claude_missing` | Claude Code not installed | see §1 |
| `claude_broken`: *placeholder* / *Exec format error* on `…/.nvm/…/bin/claude` | npm installed Claude Code but skipped its install step (`ignore-scripts`, `--omit=optional`), leaving a text placeholder instead of the native binary | `curl -fsSL https://claude.ai/install.sh \| bash`, then `npm uninstall -g @anthropic-ai/claude-code`; check that `claude --version` works |
| Claude Code shows *401*, or *400 No connected db* | Claude Code is using a different key (LiteLLM looks up unknown `sk-…` keys in a database it doesn't have, hence the odd message) | always launch with `claude-kimi`; check `ANTHROPIC_API_KEY` isn't forced elsewhere |
| Shift+Tab never shows **auto**; launcher says *Auto mode is DISABLED for this session* | no Auto Mode reviewer is configured, or the running gateway predates it | enable one (`classifier enable …`, see §7), then `./gateway.sh restart` and relaunch Claude Code |
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
* **Auto Mode** classification never runs on Kimi K3 unless you explicitly opt in with
  `classifier enable --backend kimi --accept-risk` (§7), and every launch then warns about it.
  Without a classifier, Auto Mode is disabled rather than weakened.
* **Access keys** (`set-auth access-key`) are long-lived and broader than a Bedrock API key. Keep the
  IAM user limited to `bedrock:InvokeModel*` on the Kimi (and classifier) profiles, and rotate the keys.
* **Login items** are per user (LaunchAgent, XDG autostart, Startup folder). They contain no secrets
  and are removed by `autostart disable` and `uninstall`.

## 13. Supported versions, test status and known limitations

| Component | Version |
|---|---|
| LiteLLM | **1.106.0** (stable; 1.105.1 is affected by the regex issue) |
| Claude Code | **2.1.303** and **2.1.310** tested; ≥ 2.1.288 required |
| Python | 3.11 – 3.14 supported; **tested on 3.14.7** |
| keyring / psutil / prisma | 25.7.0 / 7.2.2 / 0.15.0 |
| Bedrock model | `in.moonshotai.kimi-k3` (also `us.`/`global.`), or an application-profile ARN; auth by Bedrock API key (bearer) or IAM access key pair (SigV4) |

**What has actually been tested**

* ✅ **macOS 27 on Apple Silicon (arm64)**, Python 3.14.7.
  * **Automated:** 164 tests: 146 unit tests and 18 offline end-to-end tests. The end-to-end tests
    use a real LiteLLM proxy, real Claude Code and a mocked Bedrock that reproduces Kimi K3's
    documented failures and verifies SigV4 signatures. In the latest run 163 passed and 1 was skipped:
    the PowerShell parser check, because `pwsh` wasn't installed (it passed with PowerShell 7.6 in an
    earlier run). `shellcheck` is clean.
  * **Live against real Bedrock** (2026-10-26):
    * Kimi K3 through an application-profile ARN, with SigV4 access-key auth.
    * Claude Code 2.1.310 for chat, a Bash tool call and Auto Mode with the Kimi self-review opt-in.
    * A macOS login agent starting the gateway, including the race with the Claude Code login item.
  * **Earlier:** Chat Completions with low effort and prompt caching on `in.`/`global.moonshotai.kimi-k3`
    (§6).
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
scripts/autostart.py           login items: LaunchAgent (macOS), XDG autostart (Linux), Startup folder (Windows)
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
