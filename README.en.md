<div align="center">

# WorkBuddy Manager

**Web console for Tencent CodeBuddy account pools · OpenAI-compatible reverse proxy**

A web frontend for [`workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api):
bulk account onboarding via QR code, automatic daily check-in, API key distribution,
IP access control, request logs and usage stats — all in one panel.

> The upstream workbuddy2api source **ships inside this project's release package**
> (MIT). Existing deployments are unaffected — for reinstall/migration, see the
> [deployment guide](deploy/README.md#〇上游源码从哪来随发布包分发).

![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4-06B6D4?logo=tailwindcss&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

[![Release](https://img.shields.io/github/v/release/ithtelab/workbuddy-manager?color=22c55e&label=Release)](https://github.com/ithtelab/workbuddy-manager/releases)
[![Changelog](https://img.shields.io/badge/Changelog-CHANGELOG-blue)](CHANGELOG.md)
[![Issues](https://img.shields.io/github/issues/ithtelab/workbuddy-manager?color=f59e0b&label=Issues)](https://github.com/ithtelab/workbuddy-manager/issues)
[![LINUX DO](https://img.shields.io/badge/Community-LINUX%20DO-1f6feb)](https://linux.do)

**English** · [简体中文](README.md)

Published and discussed in the [**LINUX DO**](https://linux.do) community — 佬友 welcome.

<img src="docs/images/dashboard.png" alt="WorkBuddy Manager dashboard" width="100%" />

</div>

---

## What is this

`workbuddy2api` (its original repository has been deleted by its author) wraps a Tencent CodeBuddy
account pool into an OpenAI-compatible API (written in Go). Its capabilities are complete,
but they are command-line only: adding an account means running a script, checking status
means `curl /status`, and handing out API keys has no interface at all.

This project fills that gap — a web console you can safely run on the public internet:

| What you used to do | What you do now |
|---|---|
| Run `login.sh` on the server to scan a QR code | Click "Add account", scan, and it is checked in and managed automatically |
| `curl /status` to see which account is down | Dashboard shows health, cooldown and token expiry in real time |
| Hand-edit `config.json` for check-in / concurrency | Visual settings with toggles and number inputs |
| Every downstream client shares one global key | Issue multiple keys, each with its own quota, IP and model allowlist |
| No idea who consumed how much | Every call is logged: model, tokens, latency, source IP |
| No IP protection at all | Inbound allow/deny lists plus per-key IP limits and allowlists |

> **Not a single line of workbuddy2api is modified.** Account rotation, concurrency and
> circuit breaking stay its job; this project is a separate console and gateway.

**How this relates to workbuddy2api**: this project is the **visual companion** to it —
it makes a capable upstream gateway visible and manageable. The two fit together naturally:

- **The upstream provides the capabilities, the panel presents them**: account scheduling,
  token refresh and circuit breaking are workbuddy2api's job; the panel visualises those
  capabilities and adds the operational side — key distribution, IP control, usage stats
- **We learn from each other and evolve together**: when the upstream gains a capability
  this project follows, and operational needs discovered on the panel side feed back to the
  upstream. The upstream lists this project among its "community front-end panels", and we
  hope to make that ecosystem better together
- **The upstream stays focused on its core**: the panel does not ask the upstream to change
  code for it, so the upstream can stay lean

Contributions are welcome: both panel and upstream-source issues and ideas belong in
[this repository](https://github.com/ithtelab/workbuddy-manager/issues) — the original
upstream repo is gone, and its source is now maintained here.

---

## Features

### Account management
- **QR onboarding** — scan with WeChat / QQ; on success the account is checked in daily,
  written to an auth file, and the upstream container is reloaded
- **Token monitoring** — expiry progress bar with warnings under 1 hour; one-click manual
  check-in, connectivity probe and token refresh
  - The bar also shows a **"last renewed"** timestamp: remaining days get reset by a
    refresh, so the raw number is easy to misread (`7 days` may be freshly renewed while
    `60 days` may never have been refreshed). The timestamp makes it unambiguous
  - **Automatic renewal**: once under 3 days of validity remain, the panel obtains a new
    token from Tencent in the background (the upstream only refreshes at its keep-alive
    hour, or when the account is actively serving traffic — idle accounts would otherwise
    run all the way to expiry). Results are recorded on the Tasks page
  - The **refresh token** button now **actually renews and saves** the token instead of
    just reloading the upstream; if the token can no longer be renewed, it says so and
    tells you to sign in again
- **Runtime status** — merged with the upstream pool state (online / cooling down /
  disabled / expired); **per-model rate limiting** is flagged separately (the account is
  still online, only one model is temporarily limited — hover for the reset time)
- **Credit balance** — current spendable credits per account, colour-coded by level, and
  **queried live from Tencent** (the upstream `/status` value can lag by hours). Fetched on
  page load, updated right after check-in, plus a "Refresh credits" button. Each number is
  labelled **`live`** or **`cached Ns ago`** so you can tell how fresh it is
- **Credit expiry countdown** — credits expire per package and are forfeited once they do,
  each package on its own schedule. The balance shows the nearest package's amount and
  countdown (e.g. `300 · expires in 8 days`, colour-coded by urgency); hover for every
  package's amount, exact expiry time and the total. The dashboard credit card also flags
  the nearest expiry and its amount, so credits don't quietly go to waste
- **Temporary disable** — when an account is dragging the pool down, take it out instead of
  deleting it (deleting loses the credentials and forces a re-scan). While disabled it is
  **never picked for chat traffic, but check-in and token keep-alive keep running**, so its
  credits and credentials stay alive and you can bring it back at any time. On older
  upstream versions the panel falls back to fully removing the account from the pool and
  says so in the message
- **Credit change ledger** — every channel that increases the balance is recorded. The
  upstream only logs travel rewards; check-in and activity reports log nothing, so we
  compare balances after each credit query and record any increase
  (e.g. `balance +100（1300 → 1400）`), filterable as "credit change" in the task log page
- **Task log page** (bottom dock → "Tasks") — check-in results, raw upstream logs, and
  automated task rewards in one place instead of crowding the account list; auto-refreshes
  every 30 seconds. All three panels scroll inside a fixed height and support time-range
  filtering; 200 records per fetch with a "showing latest N" notice when truncated
- **Automated task & reward log** — results and **credit gains** for cat travel / activity
  report / auto check-in / keep-alive (e.g. travel reward `+100`, adopt Buddy `+300`),
  filterable by type with running totals. "Already checked in today" is shown as a normal
  idempotent success, each round has a summary line, and token refresh failures are
  attributed to the right account. Upstream logs are English and are translated for
  display (hover for the original). Because the upstream only writes these to container
  logs — which are lost when the container is recreated — a background collector parses
  and persists them every 45 seconds; "Collect now" is also available

### Reverse proxy gateway (`/v1`)
- **OpenAI compatible** — point any standard SDK at it; streaming (SSE) and non-streaming
- **Multi-key distribution** — per-key **realm** (CN / Global), expiry, max IPs,
  IP allowlist, model allowlist and token quota
- **Key safety** — only a SHA-256 hash is stored; the plaintext is shown once at creation
- **Model alias mapping** — map e.g. `gpt-4o-mini` to a real model so downstream clients
  can migrate without changes
- **CN / Global switch** — one toggle in the top right (the upstream serves both realms
  from a single instance sharing one account pool): accounts, models, playground, task
  logs, request logs and usage stats are all filtered by realm, and "Add account" follows
  the switch (global accounts go through region registration and a one-time trial).
  **Keys can be realm-scoped too** — a CN key can only call CN models and vice versa
- **Model catalogue** — a dedicated page for the models an account can actually use:
  display name, description, context, max output, **reasoning effort levels**,
  **credit multiplier**, and capability badges (vision / reasoning-only), grouped by series,
  with search, capability filters and **sorting by credit multiplier** (the practical way to
  find the cheapest model). Data comes straight from Tencent's model endpoint, which carries
  more fields than the upstream. When unavailable it falls back to the upstream list and
  **states the source honestly** rather than inventing data
- **Global realm limitations** (upstream behaviour, not something missing here): no
  check-in / cat travel / school season / night cat tasks, and credits come only from the
  one-time trial. **Keep-alive and activity reporting still run.** The task page explains
  this instead of showing an empty panel
- **Chat playground** — try a model without creating a key (same account pool as
  downstream): real model picker plus **thinking effort** (wired to `reasoning_effort`,
  only showing levels that model supports), interruptible streaming, and **live credit
  consumption in the corner** (from upstream `usage.credit`). Admins only
- **Inbound IP control** — global allow/deny lists with CIDR support; allowlist mode can
  restrict access to trusted sources only
- **Full audit trail** — per call: key, IP, model, status code, **time to first token**,
  total latency, token usage, **actual credit charged** and **prompt-cache hits**
  (credit comes from upstream `usage.credit`;
  shown as `—` when the upstream does not report it, which is different from charging 0)

### Visual settings
- **Scheduled tasks** — check-in / cat travel / activity report / keep-alive each with an
  independent toggle and schedule (hour arrays like `9, 21`), explained in plain language
  instead of hand-editing JSON
- **System prompt** — `prompt.mode` switches between `passthrough` (**default**, forwards
  the client's system message) and `custom` (the gateway replaces it with its own prompt).
  Keep the default if you want the downstream system prompt preserved; switch to `custom`
  if you rely on the gateway prompt for stable behaviour or want to eliminate template
  fingerprint false positives. A custom prompt file is also supported (custom mode only).
  This group carries an explicit risk warning
- **Rate limiting & cooldowns** — soft rate-limit cooldown base and backoff ceiling
  (durations like `600s` / `2h`)
- **Concurrency & circuit breaking** — per-account concurrency, failure threshold, breaker
  cooldown and ceiling, idle compensation weight, and the expiring-credits window
  (credits expiring inside the window are spent first; empty or 0 disables it)
- **Feature flags / session stickiness** — outbound fingerprint sanitisation, session
  binding TTL and cleanup interval
- **Available models** — fetched live from the upstream with the source stated,
  plus a manual "Refetch" (the upstream caches for
  one hour). The list is fetched using one randomly chosen account, so **what you see
  depends on that account's entitlements**
- Everything is validated on input (hours limited to 0-23, deduplicated and sorted;
  durations must look like `30s / 10m / 2h / 1d`) with server-side checks as a second line
  of defence — invalid values are rejected rather than written into the config
- Only changed fields are submitted, so untouched settings are never overwritten;
  uncommon options stay editable via "Advanced settings"
- When the config cannot be read, the reason is shown and saving is blocked, so an empty
  config can never overwrite the real file

### Mobile
- **The task page collapses into a single card on phones**: segmented tabs for
  check-in records / automated tasks / raw logs show one panel at a time instead of three
  stacked sections; desktop keeps all three side by side
- **Usable on phones**: the account list and task logs become vertical cards on narrow
  screens (nickname / UID / status / credits / expiry / actions all visible at once);
  settings tabs scroll horizontally, and tables scroll sideways rather than breaking the layout
- Header buttons shorten their labels on phones; the bottom dock stays centred

### Interface & interaction
- **Floating dock** — draggable with a magnetic zoom effect, **anchored at its centre** so
  it does not drift while hovering; stays put when dialogs open
- **Semantic colours** — token expiry in four tiers (expired / expiring / tight / healthy)
  shared by the account list and dashboard
- **Prominent toasts** — springy pop-ups with semantic borders, a countdown bar and
  🎉 / ⛔ / ⚠️ / 💡 icons by severity
- Destructive actions (delete account, clear logs, sign out) always require confirmation;
  the profile panel notes "click outside or press Esc to close"
- Light / dark themes, following the system by default

### One-click update
- **Update from the web UI**, no server login needed: Settings → System update
- **New version detection**: notifies when either the console (GitHub Release) or the
  upstream (latest commit) has updates, with a version comparison and commit summaries
- Three modes: **update everything** / **upstream only** (`workbuddy2api`) /
  **console only**
- Live progress and logs; account auth files, upstream config, keys and log data are all
  preserved
- Port confinement is re-applied after an upstream update so the security baseline cannot
  be silently reverted by upstream defaults
- Admins only; the target is a fixed enum (no client-supplied commands or paths)
- **Release packages are always signature-verified** (supply-chain protection): the
  updater embeds the maintainer's public key and verifies before extracting. A missing,
  tampered or mismatched signature aborts the install. The panel shows a "verified" badge,
  Once verification passes, `deploy/` is updated from the package too (the updater
  itself lives there and needs to be upgradeable). Set `WB_SYNC_DEPLOY=0` to keep
  your local `deploy/` untouched.
  See [docs/release-signing.md](docs/release-signing.md)

### Changelog
- Built-in under Settings → Changelog, reading `CHANGELOG.md` from the install directory
  (with a copy inside `server/` as a fallback)
- **Works offline**: the file ships with the release package, no GitHub access required
- Collapsible per version (latest expanded), marking the running version and labelling
  unreleased entries as "in development"
- Colour-coded category tags (security / added / fixed / improved) with no markdown
  dependency
- One-click update also syncs `CHANGELOG.md` and `README.md`; even if an older deployment
  lacks those files, the copies inside `server/` get replaced and the UI still shows the
  latest notes

### Access control
- Console login with username + password, stored as salted PBKDF2-SHA256
- **Console identity is accepted only from a signed cookie** (HMAC-SHA256). Gateway API
  keys **cannot** be used to log in — keys authorise model calls on `/v1` only, fully
  separated from admin privileges
- Changing a password / role, or deleting a user, **immediately revokes that user's
  sessions** (other users are unaffected)
- **Audit log**: successful and failed logins, password changes and user changes are
  recorded (time / actor / source IP) and viewable under Security
- Post-compromise credential cleanup: `python3 deploy/purge_credentials.py` (rotates the
  session secret, clears leftover `api_keys`, revokes all sessions; backs up before writing)
- Roles: `admin` can read and write, `viewer` is read-only (handy for letting colleagues
  watch status)
- HttpOnly signed-cookie sessions; 5 consecutive failures from one IP lock it for 10 minutes

---

## Screenshots

### Dashboard
> Account health, upstream connectivity, 14-day call volume

<img src="docs/images/dashboard.png" alt="Dashboard" width="100%" />

<details>
<summary><b>Dark mode</b> (click to expand)</summary>

<img src="docs/images/dashboard-dark.png" alt="Dashboard in dark mode" width="100%" />

</details>

### Accounts
> Token expiry colour-coded by remaining time (expired / expiring / tight / healthy)

<img src="docs/images/accounts.png" alt="Accounts" width="100%" />

<details>
<summary><b>Adding an account by QR code</b> (click to expand)</summary>

<img src="docs/images/add-account.png" alt="Add account by QR code" width="100%" />

</details>

### Task logs
> Check-in results, raw upstream logs, and automated task rewards on one page (30s refresh)

<img src="docs/images/tasks.png" alt="Task logs" width="100%" />

### API keys
> Realm-scoped (CN / Global), independent quota, IP limits and model allowlists;
> plaintext shown once at creation

<img src="docs/images/keys.png" alt="API keys" width="100%" />

### Request logs
> Filter by time / key / status / model / IP, with **the account actually used**, **time to first token**, total latency, tokens and **prompt-cache hits**

"First token" = from sending the upstream request to the first delta containing content.
It reflects **how fast the upstream starts responding**. "Total latency" includes the whole
generation, so it grows with answer length — useful for overall cost per request.
Non-streaming requests have no intermediate steps, so the first-token column shows `—`.

**Account** is which upstream account served this call, shown as `nickname(uid8)`. The upstream
picks it and does not return it in the response, so the panel reads the upstream's container log
and matches entries by time — that is why it **appears a few seconds after** the request (a
just-finished one may still show `—`). When the container log is unavailable (upstream on another
host, no `docker.sock`, native deployment) the column stays `—`; nothing else is affected.

The cache marker after the token count (green "cache N%" / amber "no cache hit") comes from the
usage data the upstream returns; it tells you whether a repeated prefix is **actually hitting the
cache**, which is billed much cheaper. Prefix caches are stored **per account**, so "the account
changed" and "no cache hit" often show up together — read the two columns side by side. When the
upstream does not return this data the marker is omitted (the detail view says "not captured") —
that is not the same as "no cache hit".

<img src="docs/images/logs.png" alt="Request logs" width="100%" />

### Usage stats
> Token consumption broken down by day, model and key

<img src="docs/images/stats.png" alt="Usage stats" width="100%" />

### Model catalogue
> Models available to your accounts: display name, context, max output, reasoning effort, series

Data comes straight from Tencent's model endpoint, which is why display names and
**reasoning effort levels** are present (the upstream `/v1/models` drops both). Summary
cards and the list are computed from real data; when the source is unavailable it falls
back to the upstream list and labels it honestly rather than inventing fields.

<img src="docs/images/models.png" alt="Model catalogue" width="100%" />

### Chat playground
> Try models without creating a key: real model picker, thinking effort, live credit use

Requests go through the console session to the upstream (same account pool as downstream)
and are **admin-only** — trying a model really does consume credits. The corner shows the
running total for the session, and each reply lists its charge and token count.

<img src="docs/images/playground.png" alt="Chat playground" width="100%" />

### Security & IP control
> Global allow/deny lists, CIDR rules, access audit and blocked requests

<img src="docs/images/security.png" alt="Security and IP control" width="100%" />

### Settings
> Upstream config made visual, with toggles and number inputs

<img src="docs/images/settings.png" alt="Settings" width="100%" />

---

## Architecture

```
    Downstream clients / sub2api (OpenAI SDK)
              │  Authorization: Bearer wbk_xxx
              ▼
   ┌──────────────────────────────────────────────┐
   │  WorkBuddy Manager                     :7864 │
   │  ┌────────────────────────────────────────┐  │
   │  │ Gateway  /v1  /v2  /healthz            │  │
   │  │  key auth → IP control → model mapping │  │
   │  │  → streaming proxy → logs & usage (SQLite) │
   │  ├────────────────────────────────────────┤  │
   │  │ Admin API  /api/*                      │  │
   │  │  login / accounts / keys / logs /      │  │
   │  │  usage / security / settings           │  │
   │  ├────────────────────────────────────────┤  │
   │  │ Web frontend (Next.js static export)   │  │
   │  └────────────────────────────────────────┘  │
   └───────────────┬──────────────────────────────┘
                   │ shares auths/*.json; calls /status /v1/models
                   ▼
   ┌──────────────────────────────────────────────┐
   │  workbuddy2api (Go, unmodified)        :7863 │
   │  account rotation · scheduling · breaker · refresh │
   └───────────────┬──────────────────────────────┘
                   ▼
        Tencent CodeBuddy / copilot.tencent.com
```

**One process, one port**: the frontend is a `next build` static export served by FastAPI,
so `/api` and `/v1` are same-origin and no CORS setup is needed.

**Stack**

| Layer | Choice |
|---|---|
| Frontend | Next.js 15 (App Router) · React 19 · TypeScript · shadcn/ui · Tailwind CSS v4 · motion · recharts · sonner |
| Backend | Python 3.11+ · FastAPI · uvicorn · httpx · SQLite (stdlib, no heavy dependencies) |
| Deployment | systemd + reverse proxy + Let's Encrypt HTTPS |

---

## Quick start

### 1. Local development

```bash
# 0) Optional: run a mock upstream if you don't have a real workbuddy2api
#    It serves a model list and sample accounts so you can see the full UI
python dev/mock_upstream.py         # listens on 127.0.0.1:7863

# 1) Backend (terminal A)
python -m pip install -r server/requirements.txt
WB_ADMIN_PASSWORD=admin123 \
WB_DATA_DIR=./data \
WB_AUTH_DIR=/opt/workbuddy2api/auths \
WB2API_BASE=http://127.0.0.1:7863 \
python -m uvicorn server.main:app --reload --port 7864

# 2) Frontend (terminal B) — next dev proxies /api and /v1 to :7864
cd web
npm install
npm run dev                          # http://localhost:3000
```

On first start `users.json` and a random session secret are generated. If
`WB_ADMIN_PASSWORD` is not set, a random admin password is printed to the log once.

> **Proxy caveat**
> If you run proxy software (Clash / V2Ray …), **especially in TUN mode**, requests to
> `127.0.0.1:7863` may be hijacked and appear to hang.
> Internal requests use `trust_env=False` by default (system proxy is ignored); set
> `WB_HTTP_PROXY` if you really need a proxy.
> In TUN mode, add `127.0.0.1` to your direct / bypass list.

#### Running tests

```bash
# Config read/write regression tests: hour arrays / duration strings /
# partial patches not clobbering other keys in the same section
python -m unittest discover -s server/tests -t . -v
```

> Tests use a temporary directory to simulate the upstream `config.json`, so they never
> touch your real config and can be run repeatedly.
> They guard two bugs that once corrupted configs: treating an hour array as a numeric
> interval, and treating a duration string as seconds.

### 2. Native Windows deployment (without Docker)

Prerequisite: `workbuddy2api` is already running locally and has working start/stop
scripts plus a file log.

```powershell
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager
Copy-Item .env.example .env
```

Set at least these values in `.env` (forward slashes are recommended in Windows paths):

```dotenv
WB_MANAGER_HOST=127.0.0.1
WB_SECURE_COOKIE=false
WB2API_MODE=native
WB_UPSTREAM_DIR=C:/path/to/workbuddy2api
WB_AUTH_DIR=C:/path/to/workbuddy2api/auths
WB_UPSTREAM_CONFIG=C:/path/to/workbuddy2api/config.json
WB2API_START_SCRIPT=C:/path/to/workbuddy2api/start-workbuddy2api.cmd
WB2API_STOP_SCRIPT=C:/path/to/workbuddy2api/stop-workbuddy2api.cmd
WB2API_LOG_FILE=C:/path/to/workbuddy2api/data/server.err.log
```

Install dependencies, export the frontend, and start the background process:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
Set-Location web
npm ci
npm run build:export
Set-Location ..
powershell -ExecutionPolicy Bypass -File .\service-tools.ps1 start
```

Use `service-tools.ps1 status|restart|stop` to manage the process. Native Windows mode
supports restarting the upstream after configuration changes and reading its file log.
The web one-click updater depends on Linux/Docker and is rejected with an explicit message;
update the code manually and restart the service instead.

> **You need to supply the upstream start/stop scripts yourself**: the upstream project
> ships Docker deployment only, with no native Windows scripts. `deploy/windows-native/`
> contains a ready-to-adapt pair of templates (plus the two rules they must follow: the
> start script has to return immediately, and logs must go to `WB2API_LOG_FILE`).

### 3. Docker

The repo ships a `Dockerfile` and `docker-compose.yml` for users already running the
upstream in Docker:

```bash
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager
# Adjust WB2API_BASE and volume paths if needed (defaults assume upstream at ../workbuddy2api)
docker compose up -d --build
docker compose logs workbuddy-manager | grep -A2 password   # first-boot random password
```

> **The frontend is built inside the image automatically.** `web/out` (the frontend
> build output) is not committed, so a fresh `git clone` does not contain it. If the
> build finds it missing, it runs `npm ci && next build` inside the container (one to
> two minutes, and it pulls the Node image the first time). If it is already present
> (for example from a release tarball), it is reused and this step is skipped. Neither
> path requires you to install Node or build the frontend by hand.
>
> On slow networks you can point npm at a mirror:
> `docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com`

Or pull the prebuilt image (pushed to GHCR on every release):

```bash
docker pull ghcr.io/ithtelab/workbuddy-manager:latest
```

> The image ships for **both `linux/amd64` and `linux/arm64`** (Apple Silicon and ARM
> cloud hosts can pull it directly, with no QEMU emulation). `docker pull` picks the
> right one for your machine automatically.

**Want an image you built yourself? Just fork the repo** — the one above is built by the
maintainer on each release. If you need to change something for your own use (different
defaults, an extra dependency, or you simply prefer not to depend on someone else's
registry), fork this repository, drop the fork-only workflow into `.github/workflows/`
and push once:

```bash
mkdir -p .github/workflows
cp deploy/fork-image/build-image.yml .github/workflows/
git add .github/workflows/build-image.yml && git commit -m "ci: build my own image" && git push
```

The workflow needs **no edits at all**: the image's namespace, the branch it watches, and
the provenance labels baked into the image are all derived from your fork at run time
(whoever forks publishes under their own name, and renaming the default branch does not
break it). Once the build finishes (a few minutes), pull your own copy — the run summary
prints the real username:

```bash
docker pull ghcr.io/<your-username>/workbuddy-manager-multiarch:latest
```

> The extra `-multiarch` suffix is **not a typo**: the `workbuddy-manager` package name
> may already be taken in the namespace by a package that is not linked to your repo, and
> a fork has no write access to that one, so the push would fail. You can also publish to
> Docker Hub at the same time (two secrets enable it automatically). Full details in
> [deploy/fork-image/README.md](deploy/fork-image/README.md).

**The container build has the same capabilities as a host install** — the compose file
mounts three things to make that true:

| Mount | Purpose |
|---|---|
| Upstream repo directory | Read its compose file for port confinement; `git pull` to update it; read/write `config.json` and `auths/` (**adding an account writes to auths**, so it cannot be read-only) |
| `./data` | Database, logs, update state. Must be persisted |
| `/var/run/docker.sock` | Lets the console inside the container restart/rebuild the upstream container — i.e. "update upstream", "auto-reload after saving settings" and "read upstream logs" |

> **On mounting docker.sock**: it grants this container host-root privileges. But this is
> **not a new risk level** — a host install already runs as root (the systemd unit has no
> `User=`, and the installer requires root), and a root process can already reach the host
> filesystem via `docker run -v /:/host`. The two are equivalent.
> If you need least privilege, comment that line out: docker-dependent features **degrade
> gracefully** to "run this on the host" with a clear notice in the UI, never failing silently.

Two other differences from a host install (both surfaced in the UI):

- **Updating the console restarts the whole container**: a container cannot restart itself.
  The flow is "replace code → exit container → compose's `restart` policy brings it back
  with the new code", so `restart: unless-stopped` must stay in the compose file.
- **The port binds to `127.0.0.1` by default**: the console holds every account credential
  and belongs behind a reverse proxy. Change the compose file if you must expose it
  directly, and make sure HTTPS is in place.

> As with a host install, one-click updates **always verify the release signature**. The
> image itself is outside that signature (a separate trust chain based on the GHCR digest
> and GitHub account security).

### 4. Server deployment (one-click script)

This project depends on the upstream workbuddy2api (account pool and OpenAI-compatible
API) — **cloning this repo alone will not run**. The release package **ships the upstream source**, so the script installs both.
To use your own copy instead, see the
[deployment guide](deploy/README.md#〇上游源码从哪来随发布包分发).
A one-click script installs both on a clean machine:

```bash
# Recommended: use the release package (includes the built frontend, no Node.js needed)
wget https://github.com/ithtelab/workbuddy-manager/releases/latest/download/workbuddy-manager-<version>.tar.gz
tar xzf workbuddy-manager-*.tar.gz && cd workbuddy-manager-*

sudo bash deploy/install.sh
```

The script will:

1. Pre-flight checks (Python / Docker / ports)
2. **Install the upstream workbuddy2api** — clone, generate a random `api_key`, fix
   directory ownership, build and start the container, wait for readiness
3. Install the console — deploy code, install dependencies, register the systemd service
4. Verify and print the access URL and initial password

**No manual config editing required.** If you already have the upstream, pass
`--skip-upstream` and it will not touch your existing config or accounts.

> When deploying via `git clone` you do **not** need to build the frontend by hand:
> the installer notices that `web/out` is missing (build artifacts are not committed)
> and runs `npm ci && npm run build:export` for you (requires Node.js on the machine;
> if it is absent the installer tells you to use the release package instead, which
> already contains the built output).

To read the initial admin password:

```bash
journalctl -u workbuddy-web | grep -A3 'initial admin'
```

**Put HTTPS in front of it before exposing it publicly** (otherwise session cookies and
passwords can be intercepted). Reverse proxy target: `http://127.0.0.1:7864`, then obtain
a certificate and enable forced HTTPS.
Full deployment notes (Nginx config, hardening, FAQ) are in [deploy/README.md](deploy/README.md).

<details>
<summary><b>Environment variables</b></summary>

| Variable | Default | Description |
|---|---|---|
| `WB_MANAGER_PORT` | `7864` | Listen port |
| `WB2API_BASE` | `http://127.0.0.1:7863` | workbuddy2api address |
| `WB2API_KEY` | from config.json | Upstream API key |
| `WB2API_MODE` | `docker` | Upstream runtime: `docker` or `native` |
| `WB2API_CONTAINER` | `workbuddy2api` | Container name used for reloads |
| `WB_AUTH_DIR` | `/opt/workbuddy2api/auths` | Account auth directory |
| `WB_UPSTREAM_CONFIG` | `/opt/workbuddy2api/config.json` | Upstream config file |
| `WB2API_START_SCRIPT` | `.cmd` under upstream dir | Native-mode start script |
| `WB2API_STOP_SCRIPT` | `.cmd` under upstream dir | Native-mode stop script |
| `WB2API_LOG_FILE` | `data/server.err.log` | Native-mode upstream log |
| `WB_DATA_DIR` | `./data` | This service's data directory |
| `WB_STATIC_DIR` | `./web/out` | Static export directory |
| `WB_ADMIN_PASSWORD` | random | Initial admin password |
| `WB_SECURE_COOKIE` | `auto` | Decided from `X-Forwarded-Proto` |
| `WB_GATEWAY_RATE_PER_MIN` | `120` | Outbound gateway per-key limit: admitted requests per 60s (`0` = unlimited) |
| `WB_GATEWAY_MAX_BODY_MB` | `32` | Outbound gateway request body limit (MB) |
| `WB_SYNC_DEPLOY` | `1` | Update `deploy/` together with the manager (`0` = leave it untouched) |
| `WB_HTTP_PROXY` | empty | Outbound proxy; empty means direct |

The full list is in [`.env.example`](.env.example).

</details>

---

## Usage guide

### 1. Onboard an account

Sign in, open **Accounts**, click **Add account** in the top right, scan with WeChat / QQ.
On success the account is checked in, written to disk and the upstream container reloads.

### 2. Distribute a key

Open **Keys**, click **New key** and configure as needed:

- **Realm** — a CN key may only call CN models; a Global key may only call models
  prefixed with `global:` (cross-realm calls are rejected, and `/v1/models` returns only
  that realm's models). Defaults to the realm you are currently viewing; pick
  "Unrestricted" to allow both
- **Expiry** — empty or 0 means it never expires
- **Max IPs** — how many distinct source IPs may use the key
- **IP allowlist** — stricter: only the listed IPs / CIDRs may call
- **Model allowlist** — narrows the key down to specific models within the chosen realm
- **Quota** — requests are rejected once the token budget is exhausted

The key plaintext is **shown only once at creation** — save it immediately.

### 3. Connect a downstream client

Fully OpenAI-compatible; point the base URL at this service's `/v1`:

```bash
curl https://wb.example.com/v1/chat/completions \
  -H "Authorization: Bearer wbk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'
```

Python example:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://wb.example.com/v1",
    api_key="wbk_xxxxxxxx",
)

resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "hello"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

> Streaming requests automatically get `stream_options.include_usage=true` so token usage
> is accounted for precisely.

<details>
<summary><b>OpenAI Responses API (Codex / DeepSeek Harness, etc.)</b></summary>

This service also speaks the Responses protocol (`/v1/responses`; `/responses` works too
when the SDK's `baseURL` omits `/v1`). The request body uses `input` instead of `messages`,
and `instructions` carries the system text:

```python
from openai import OpenAI

client = OpenAI(base_url="https://wb.example.com/v1", api_key="wbk_xxxxxxxx")

resp = client.responses.create(
    model="glm-5.2",
    instructions="You are a terse assistant",
    input="Hello",
    stream=True,
)
for event in resp:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
```

Tool calls are supported as well: send flat-shaped `tools`
(`{type:"function", name, parameters}`) and you get back `function_call` output items
plus `response.function_call_arguments.delta` events.

> A client that speaks only `openai-responses` (such as a DeepSeek Harness custom
> provider) should set its API protocol to `openai-responses`. It is not the same
> protocol as `openai-completions`, so it needs its own provider entry.

</details>

<details>
<summary><b>Anthropic Messages API (Claude Code / Cursor / Cline, etc.)</b></summary>

Clients that only speak the Anthropic protocol can point their base URL straight at this
service — set `ANTHROPIC_BASE_URL` to the root (the wire format of `/v1/messages` matches
the official one):

```bash
export ANTHROPIC_BASE_URL=https://wb.example.com
export ANTHROPIC_AUTH_TOKEN=wbk_xxxxxxxx   # x-api-key header also accepted
export ANTHROPIC_MODEL=glm-5.2
```

Model names are the **same set** as on the OpenAI side (including the `global:` prefix
version rule and per-key realm isolation), and `/v1/messages/count_tokens` is available.

</details>

<details>
<summary><b>Available models</b></summary>

Check Settings → Available models for the live list. Commonly (all with a 131072 context):

`glm-5.2` · `glm-5.1` · `glm-5v-turbo` · `kimi-k2.7` · `minimax-m3` · `hy3` · `hy3-preview`

</details>

---

## API reference

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | gateway key | OpenAI-compatible chat (streaming / non-streaming) |
| `POST` | `/v2/chat/completions` | gateway key | Same as above (v2 path) |
| `POST` | `/v1/responses` `/responses` | gateway key | OpenAI Responses API compatible (streaming / non-streaming) |
| `POST` | `/v1/messages` | gateway key | Anthropic Messages API compatible (Claude Code, etc.) |
| `POST` | `/v1/messages/count_tokens` | gateway key | Rough input-token estimate (by character count) |
| `GET` | `/v1/models` | gateway key | Model list |
| `GET` | `/healthz` | none | Liveness probe (includes upstream connectivity) |
| `GET` | `/api/me` | session | Current user |
| `POST` | `/api/login` `/api/logout` | none | Sign in / out |
| `GET` | `/api/accounts` | session | Account list |
| `POST` | `/api/auth/start` `/api/auth/poll` | admin | QR authorisation flow |
| `POST` | `/api/accounts/{file}/checkin` `/test` `/refresh` | admin | Check-in / probe / refresh |
| `DELETE` | `/api/accounts/{file}` | admin | Delete an account |
| `GET/POST/PATCH/DELETE` | `/api/keys[/{id}]` | session / admin | Key management (handed to downstream callers) |
| `GET/POST/PATCH/DELETE` | `/api/tokens[/{id}]` | session (admin) | Admin API tokens (for scripts / CI, see [docs/api-tokens.md](docs/api-tokens.md)) |
| `GET` | `/api/logs` `/api/stats/*` | session | Logs and usage |
| `GET/POST/DELETE` | `/api/security/*` | session / admin | IP rules and audit |
| `GET/POST` | `/api/settings/*` | session / admin | Upstream config, model mapping |

Admin API details are available at `/docs` (Swagger UI) when enabled.

---

## Project layout

```
workbuddy-manager/
├─ server/                       # FastAPI backend
│  ├─ main.py                    # entry: router registration + static hosting
│  ├─ config.py                  # all env vars and the http_client factory
│  ├─ db.py                      # SQLite (keys / logs / usage / IP / settings)
│  ├─ security.py                # PBKDF2 + signed cookies + brute-force protection
│  ├─ keysvc.py                  # key generation, validation, quota checks
│  ├─ iputil.py                  # real client IP resolution + CIDR matching
│  ├─ services/
│  │  ├─ tencent.py              # Tencent login / check-in / probe protocol
│  │  └─ wb2api.py               # workbuddy2api interaction (incl. config validation)
│  ├─ tests/                     # regression tests
│  └─ routers/                   # auth accounts keys logs stats security settings gateway
├─ web/                          # Next.js 15 frontend
│  ├─ app/(main)/                # dashboard accounts keys logs stats security settings
│  ├─ app/(auth)/login/          # sign-in page
│  ├─ components/ui/             # shadcn primitives (incl. floating-dock)
│  └─ components/common/         # floating dock, stat cards, feature components
├─ dev/mock_upstream.py          # mock upstream for local development
├─ deploy/                       # systemd unit + install scripts
└─ docs/                         # design docs + screenshots
```

---

## Security

- Gateway keys are stored as SHA-256 hashes only; the plaintext is returned once at creation
- Console passwords use salted PBKDF2-SHA256 (260k iterations)
- Sessions use HttpOnly + SameSite=Lax signed cookies, with `Secure` enabled automatically in production
- 5 failed logins from one IP lock it for 10 minutes
- All file operations are checked for path traversal
- **The real client IP comes from the `X-Real-IP` written by the reverse proxy** (the first
  `X-Forwarded-For` entry is client-controlled), so IP allow/deny lists, per-key IP limits
  and login lockout cannot be spoofed
- Failed logins are locked **per IP and per username**, blocking both single-host and
  distributed brute force
- The admin API supports **scoped API tokens** (read-only / admin, revocable, expiring;
  only a hash is stored and every use is auditable) so scripts / CI can call it without
  logging in. **High-risk endpoints and token management itself accept sessions only**,
  so a leaked token cannot escalate privileges or persist itself
  (see [docs/api-tokens.md](docs/api-tokens.md))
- `/docs` and `/openapi.json` are disabled in production (`WB_ENABLE_DOCS=1` to enable)
- The gateway limits request body size (8 MiB) and per-key request rate (120/min by default)
- Security headers (CSP, `X-Frame-Options`, `X-Content-Type-Options`, …) are set
- `users.json`, `data/*.db`, `.env` and account auth files are all excluded via `.gitignore`

> The full audit is in [the security audit report](docs/SECURITY-AUDIT.md).

### Known accounting details

- Usage is bucketed by **local timezone** (write, backfill and display share one rule).
  Historically writes used local time and backfills used UTC, which on a UTC+8 machine
  counted early-morning calls on two different days. If your data was affected, use
  Usage → Rebuild stats to recompute from the request logs

### Post-deployment hardening

1. **Change the initial password** — do not keep the script's default
2. **Always access over HTTPS**: bind 7863 / 7864 to `127.0.0.1` only and expose them
   through a reverse proxy
3. If you put a CDN in front, set `WB_TRUSTED_PROXY_HOPS` to the number of proxy layers
4. Report issues through the [private channel](https://github.com/ithtelab/workbuddy-manager/security/advisories/new),
   **not** a public issue

> ⚠️ Public exposure **requires** HTTPS, otherwise session cookies and passwords can be
> intercepted by a man-in-the-middle. Additional IP allowlisting or an access proxy is
> recommended on top.

---

## Known limitations

- **No outbound IP pool**: only **inbound** IP control is implemented. Binding a dedicated
  egress IP / proxy per Tencent account would require proxy-pool support in the upstream Go
  service and is out of scope here.
- Request and response **bodies are not stored** — only metadata (model, status, tokens,
  latency, source), to protect privacy.
- Usage is aggregated per day × key × model; hourly granularity would require extending the
  `usage_daily` table.

---

## Changelog & feedback

- **Changelog**: [CHANGELOG.md](CHANGELOG.md) — additions, fixes and changes per version
- **Releases**: [Releases](https://github.com/ithtelab/workbuddy-manager/releases) — each
  version ships a deployable `.tar.gz` / `.zip` (with the built frontend); unpack and run
  `sudo bash deploy/install.sh`
- **Feedback**: [report a bug](https://github.com/ithtelab/workbuddy-manager/issues/new?template=bug_report.yml) ·
  [request a feature](https://github.com/ithtelab/workbuddy-manager/issues/new?template=feature_request.yml)

> Please include the version and error logs, and **remove any keys or tokens first**.
> For issues with the upstream workbuddy2api itself, use
> [this repository](https://github.com/ithtelab/workbuddy-manager/issues) — the upstream
> source ships with our releases.

### Release process

Maintainers only need to push a tag:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

CI builds the frontend, packages the artifacts, extracts the matching CHANGELOG section as
release notes, and creates a Release with the archives attached.

---

## Sponsor & Promotion

> Disclosure: this is a partner promotion. Deploying this project needs a server that can
> run Docker; the listing below is for reference. **This repository has no technical
> dependency on it** — any other provider works just as well.

### Aiwei Cloud (爱维云) — cloud servers, no ICP filing required

[![Aiwei Cloud · lovevps.cn](docs/images/lovevps.png)](https://lovevps.cn/)

**25% off, ongoing** — use promo code **`catfk`** at checkout ｜ <https://lovevps.cn/>

- **No ICP filing, ready in minutes** — Hong Kong (5 zones), US, Japan, Singapore,
  Malaysia, Germany and more; plus many mainland China locations
- **Optimised routes** — CN2 / 9929 / BGP premium lines; DDoS-protected plans with
  200G mitigation
- **Residential IPs** — native US residential broadband IPs available
- **Elastic billing** — create and release on demand, resize freely
- **Credentials** — licensed IDC / ISP / CDN operator (B1-20263321, 苏B2-20263329)

---

## Related projects

- [**sanguine886/workbuddy-sdk**](https://github.com/sanguine886/workbuddy-sdk) (Go, MIT) —
  a community-maintained Go client library covering both of this project's API surfaces:
  the control plane `/api/*` (accounts, keys, stats, logs, security, settings, users,
  updates) and the data plane `/v1/*` (Chat Completions / Responses / Anthropic
  Messages / Models). Handy for Go tooling — `go get` it instead of wiring HTTP and
  session auth by hand.

> A community project with **no code dependency on this repository**; please report
> issues to [its tracker](https://github.com/sanguine886/workbuddy-sdk/issues).

## Credits

- [**LINUX DO**](https://linux.do) — the community where this project is published and discussed
- [**linux-do/cdk**](https://github.com/linux-do/cdk) (MIT) — design tokens and floating
  dock component; this project's UI follows its visual language
- [**Sliverkiss/workbuddy2api**](https://github.com/Sliverkiss/workbuddy2api) — the account
  pool and OpenAI-compatible proxy underneath (MIT; its source is distributed with
  this project's releases)
- [**lbjlaq/Antigravity-Manager**](https://github.com/lbjlaq/Antigravity-Manager) — feature
  reference for the console

## Hello World

As tradition dictates, here is an annotated Hello World. This project's server side is
Python (FastAPI), so let it do the greeting:

```python
# -*- coding: utf-8 -*-
# The classic Hello World: define the greeting first, then print it
greeting = "Hello, World!"   # define a string variable holding the greeting
print(greeting)              # print it to the console: Hello, World!
```

## License

[MIT](LICENSE)
