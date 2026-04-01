# AI-Driven Incident Automation

FastAPI service that turns a **🚨 (rotating light) emoji reaction** in a Slack **command center** channel into a full incident workflow: **FireHydrant** incident, **Jira** ticket, **Google Meet** link, a **dedicated Slack war-room channel**, an announcement in **#incident**, and **AI-generated** titles, impact text, and (on resolve) **issue + solution summaries** via **Ollama**.

**POC (point of contact)** is the **author of the original message** in the command center—not the person who added the emoji.

---

## Features

| Area | What it does |
|------|----------------|
| **Trigger** | `reaction_added` with `:rotating_light:` on a message in the configured command-center channel |
| **AI (Ollama)** | Incident title, impact structure, status one-liner, post-resolution **issue** & **solution** summaries (uses **#inc-… channel history** + original thread) |
| **FireHydrant** | Create incident; **milestone sync** via bulk API when status buttons are used |
| **Jira** | Create a task with description from Slack context |
| **Slack** | New private channel per incident, invite POC (+ reactor if different), rich brief with **status** & **severity** buttons, `#incident` card with the same controls |
| **Lifecycle** | Acknowledged → Investigating → Identified → Mitigated → Resolved (configurable slugs for FireHydrant) |
| **RCCA (optional)** | Google Drive folder search + Ollama embeddings; POC must click **Yes** before similar-incident text is posted in the incident thread |
| **New Relic (optional)** | NRQL via NerdGraph; posts a **Live Incident Data** update on a timer (default **5 min**) in the incident thread until **Resolved** |

---

## Architecture (high level)

1. Slack sends events to `POST /slack/events` (emoji reaction).
2. App fetches message + thread, calls Ollama, creates FireHydrant + Jira + Slack channel + posts.
3. Button clicks go to `POST /slack/interactivity`; app updates in-memory state, FireHydrant milestones, and refreshes Slack messages.
4. **Resolved** triggers Ollama summaries and posts them to the incident channel.

---

## Requirements

- Python **3.10+** (recommended; **3.12+** works well)
- **Slack** app with Event Subscriptions + Interactivity
- **FireHydrant** bot API token
- **Jira Cloud** API token + project
- **Ollama** running where the app can reach it (`OLLAMA_HOST`), with your model pulled (`ollama pull <model>`)

---

## Share this repo with teammates (macOS)

These steps assume **Apple Silicon or Intel Macs** running a recent **macOS** (Sonoma / Sequoia or similar). Use a **Git host** (GitHub, GitLab, etc.) so everyone clones the same code. **Do not commit `.env`** — it is already in `.gitignore`.

### One-time Mac prerequisites (each machine)

1. **Apple developer tools** (for `git` and build tools, if you do not have them yet):
   ```bash
   xcode-select --install
   ```
2. **Homebrew** (recommended package manager for Mac): follow [https://brew.sh](https://brew.sh), then:
   ```bash
   brew update
   ```
3. **Python 3.12+** (Homebrew avoids macOS “externally managed” Python issues):
   ```bash
   brew install python@3.12
   ```
   Use `python3.12` in bootstrap by running:
   ```bash
   export PYTHON=python3.12
   bash scripts/bootstrap.sh
   ```
   Or symlink / ensure `python3` on your PATH points to 3.10+ (`which python3`).
4. **Ollama** (local LLM — menu bar app + CLI):
   ```bash
   brew install --cask ollama
   ```
   Open **Ollama** once from Applications so the daemon runs, then in Terminal:
   ```bash
   ollama pull llama3
   ollama pull nomic-embed-text
   ```
   In `.env`, set `OLLAMA_HOST=http://127.0.0.1:11434` (default) and optionally `OLLAMA_EMBED_MODEL=nomic-embed-text` for RCCA.
5. **ngrok** (public HTTPS URL for Slack → your laptop):
   ```bash
   brew install ngrok/ngrok/ngrok
   ```
   Sign up at [ngrok.com](https://ngrok.com), add your authtoken (`ngrok config add-authtoken ...`). When the app runs on port 8000:
   ```bash
   ngrok http 8000
   ```
   Use the **https** forwarding URL in the Slack app (Event Subscriptions + Interactivity).

### RCCA folder on Mac (optional)

If you use **Google Drive for desktop** (File Stream), put RCCA PDFs/text in a synced folder and set in `.env`, for example:

```env
RCCA_LOCAL_FOLDER=/Users/YOU/Library/CloudStorage/GoogleDrive-YOUR@EMAIL/My Drive/Your RCCA Folder
```

Use your real username and Google account path (Finder → right-click folder → **Hold Option** → **Copy … as Pathname**).

### What each Mac developer does (after prerequisites)

1. **Clone** the repo into a path **without** spaces if you want fewer shell surprises, e.g. `~/Projects/ai-driven-incident`. Paths with spaces still work if you quote them.
2. From the project root:
   ```bash
   bash scripts/bootstrap.sh
   ```
   This creates `.venv`, installs dependencies, and copies **`.env.example` → `.env`** if needed.
3. **Edit `.env`** (VS Code, Cursor, or `nano .env`) with Slack, FireHydrant, Jira, and channel IDs.
4. **Activate venv and run** (always **one** worker):
   ```bash
   source .venv/bin/activate
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
5. In another Terminal tab: **`ngrok http 8000`**, then paste the HTTPS URL into Slack **Event Subscriptions** and **Interactivity** (append `/slack/events` and `/slack/interactivity`).

If two developers run the app **at the same time**, each needs their own **ngrok URL** (and usually their own Slack app copy, or only one runs the server).

### Team secrets (choose one model)

| Model | Pros | Cons |
|--------|------|------|
| **Shared `.env` via 1Password / Vault / internal doc** | One Slack app, same FH/Jira sandbox | Rotation is manual; treat like production secrets |
| **Each dev has own tokens** | Clear accountability | More Slack apps or shared bot token only on one machine |

Never paste real tokens in Slack or email; use your company’s secret store.

### Non-Mac teammates

On Windows, use **WSL2** or manual `py -m venv .venv`, `pip install -r requirements.txt`, `copy .env.example .env`, plus [Ollama for Windows](https://ollama.com/download) and ngrok’s Windows installer.

### Files teammates rely on

| File | Purpose |
|------|---------|
| `requirements.txt` | Python dependencies |
| `.env.example` | Full list of variables (safe to commit) |
| `.env` | **Local only** — secrets (never commit) |
| `scripts/bootstrap.sh` | Optional quick venv + install + `.env` copy |
| `README.md` | This document |

---

## Setup

### 1. Clone and install

```bash
cd "AI driven incident"
bash scripts/bootstrap.sh
# or manually:
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Environment variables

Edit **`.env`** in the project root (start from **`.env.example`**). All variables are documented there.

Highlights:

```env
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL_ID=C01234567          # command center / trigger channel
SLACK_INCIDENT_CHANNEL_ID=C0...     # public #incident (or equivalent)

# FireHydrant
FIREHYDRANT_API_KEY=fhb-...

# Jira Cloud
JIRA_DOMAIN=your-domain.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=SCRUM

# Ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3
OLLAMA_EMBED_MODEL=nomic-embed-text   # recommended for RCCA
OLLAMA_TIMEOUT=120

# Optional — comma-separated milestone slugs in order (must match your FireHydrant org)
# FH_MILESTONE_SLUGS=acknowledged,investigating,identified,mitigated,resolved

# ── RCCA similarity (pick ONE approach) ──
#
# A) Local synced folder (macOS Google Drive / File Stream) — no API keys:
# RCCA_LOCAL_FOLDER=/Users/you/Library/CloudStorage/GoogleDrive-your@email.com/My Drive/RCCA document
# Put .txt or .md RCCA write-ups in that folder (native Google Docs are shortcuts; export as .txt/.md).
#
# B) Google Drive API (read-only service account):
# GDRIVE_RCCA_ENABLED=true
# GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
# GDRIVE_RCCA_FOLDER_ID=your_drive_folder_id
# Share the folder with the service account email (Viewer).
# GDRIVE_SIMILARITY_THRESHOLD=0.38
# GDRIVE_KEYWORD_WEIGHT=0.25
# RCCA_KEYWORD_ONLY_THRESHOLD=0.15

# ── Optional: New Relic live NRQL (User API key) ──
# NEW_RELIC_ENABLED=true
# NEW_RELIC_API_KEY=...
# NEW_RELIC_ACCOUNT_ID=12345678
# NEW_RELIC_REGION=us
# NR_MONITOR_INTERVAL_SEC=300
# NR_QUERY_WINDOW_MINUTES=30
```

### 3. Run the API

Use **a single worker** so in-memory incident state is consistent:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Avoid `uvicorn --workers 2+` unless you add a shared store (Redis/DB).

### 4. Slack app configuration

| Setting | URL |
|--------|-----|
| **Event Subscriptions** | `https://<your-public-host>/slack/events` |
| **Interactivity** | `https://<your-public-host>/slack/interactivity` |

Subscribe to bot event: **`reaction_added`**.

Typical OAuth scopes include: `channels:history`, `channels:read`, `channels:manage`, `chat:write`, `reactions:read`, `users:read`, `usergroups:read` (as needed), and any scope required to **create channels** and **invite users** your workspace uses.

For local dev, use **ngrok** (or similar) and paste the HTTPS URL into Slack.

### 5. Install Ollama model

```bash
ollama pull llama3
# or match OLLAMA_MODEL exactly, e.g. ollama pull llama3.2
```

---

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Simple JSON health message |
| `GET` | `/health` | Dependencies + active incident count |
| `POST` | `/slack/events` | Slack Events API (URL verification + `reaction_added`) |
| `POST` | `/slack/interactivity` | Block Kit button actions (status / severity) |

---

## Operational notes

- **In-memory state:** Restarting the server clears incident lookups. Buttons on **old** messages may show “not found in state” until you use a **new** incident or add persistence.
- **FireHydrant milestones:** If sync fails, check server logs and align **`FH_MILESTONE_SLUGS`** with `GET /v1/incidents/{id}/milestones` `type` values for your org.
- **Resolution summaries:** Require Ollama to be reachable and the model name to exist; the app reads **incident channel** history for context.

---

## Project layout

```
.
├── app.py                 # FastAPI app — Slack, FH, Jira, Ollama
├── integrations/
│   ├── gdrive_rcca.py     # Drive listing/export + embedding match + RCCA field extraction
│   ├── newrelic_data.py   # NerdGraph NRQL helpers
│   └── monitoring.py      # Background RCCA + NR threads
├── requirements.txt
├── .env.example           # template — safe to commit
├── .env                   # not committed — your secrets
├── scripts/
│   └── bootstrap.sh       # venv + pip + copy .env.example → .env
└── README.md
```

---

## Hackathon / demo tips

- Show: reaction in command center → FH + Jira + new channel + `#incident` post.
- Show: status buttons updating Slack + FireHydrant.
- Show: resolve → AI **issue** + **solution** block in the incident channel.

---

## License

Use or adapt for your hackathon / organization as needed.
