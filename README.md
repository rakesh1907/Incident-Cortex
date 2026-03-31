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

---

## Architecture (high level)

1. Slack sends events to `POST /slack/events` (emoji reaction).
2. App fetches message + thread, calls Ollama, creates FireHydrant + Jira + Slack channel + posts.
3. Button clicks go to `POST /slack/interactivity`; app updates in-memory state, FireHydrant milestones, and refreshes Slack messages.
4. **Resolved** triggers Ollama summaries and posts them to the incident channel.

---

## Requirements

- Python **3.10+** (recommended)
- **Slack** app with Event Subscriptions + Interactivity
- **FireHydrant** bot API token
- **Jira Cloud** API token + project
- **Ollama** running where the app can reach it (`OLLAMA_HOST`), with your model pulled (`ollama pull <model>`)

---

## Setup

### 1. Clone and install

```bash
cd "AI driven incident"
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file in the project root:

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
OLLAMA_TIMEOUT=120

# Optional — comma-separated milestone slugs in order (must match your FireHydrant org)
# FH_MILESTONE_SLUGS=acknowledged,investigating,identified,mitigated,resolved
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
├── app.py              # FastAPI app — Slack, FH, Jira, Ollama
├── requirements.txt
├── .env                # not committed — your secrets
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
