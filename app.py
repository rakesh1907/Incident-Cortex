import hashlib
import hmac
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

from integrations.monitoring import start_incident_insight_jobs, stop_incident_insight_jobs

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SLACK_INCIDENT_CHANNEL_ID = os.getenv("SLACK_INCIDENT_CHANNEL_ID")
FIREHYDRANT_API_KEY = os.getenv("FIREHYDRANT_API_KEY")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "120"))
# Faster path for declare-time draft (title + impact + status in one call, or parallel workers)
OLLAMA_DRAFT_TIMEOUT = float(os.getenv("OLLAMA_DRAFT_TIMEOUT", "45"))
OLLAMA_USE_SINGLE_DRAFT = os.getenv("OLLAMA_USE_SINGLE_DRAFT", "true").lower() in ("1", "true", "yes")

TRIGGER_EMOJI = "rotating_light"

# Comma-separated slugs in lifecycle order — must match your FireHydrant org milestones (Settings → Lifecycle).
FH_MILESTONE_ORDER = [
    s.strip()
    for s in os.getenv("FH_MILESTONE_SLUGS", "acknowledged,investigating,identified,mitigated,resolved").split(",")
    if s.strip()
]

STATUSES = ["acknowledged", "investigating", "identified", "mitigated", "resolved"]
SEVERITIES = ["SEV1", "SEV2", "SEV3"]
SEVERITY_META = {
    "SEV1": {"emoji": "🔴", "label": "SEV1 — Critical"},
    "SEV2": {"emoji": "🟠", "label": "SEV2 — Major"},
    "SEV3": {"emoji": "🟡", "label": "SEV3 — Minor"},
}
STATUS_META = {
    "acknowledged":  {"emoji": "✋", "label": "Acknowledged",  "fh_milestone": "acknowledged"},
    "investigating": {"emoji": "🔍", "label": "Investigating", "fh_milestone": "investigating"},
    "identified":    {"emoji": "🔎", "label": "Identified",    "fh_milestone": "identified"},
    "mitigated":     {"emoji": "🛠", "label": "Mitigated",     "fh_milestone": "mitigated"},
    "resolved":      {"emoji": "✅", "label": "Resolved",      "fh_milestone": "resolved"},
}
# Old Slack button payloads still in flight
STATUS_LEGACY_MAP = {"identifying": "identified", "mitigating": "mitigated"}

app = FastAPI()
processed_events: set[str] = set()
incident_counter = 0


@app.on_event("startup")
def _startup_incident_state_notice():
    print(
        "[incidents] State is in-memory only. Avoid --workers >1; restart clears buttons on old Slack messages."
    )


# ═══════════════════════════════════════════════
# Incident State Model
# ═══════════════════════════════════════════════

@dataclass
class TimelineEvent:
    timestamp: str
    text: str
    user: str = ""

    def display(self) -> str:
        who = f" — {self.user}" if self.user else ""
        return f"`{self.timestamp}`{who}: {self.text}"


@dataclass
class IncidentState:
    id: str
    number: int
    title: str
    severity: str
    status: str = "acknowledged"
    commander_id: str = ""
    commander_name: str = ""
    impact_summary: str = ""
    current_status_text: str = ""
    source_channel: str = ""
    source_message_ts: str = ""
    thread_permalink: str = ""
    incident_url: str = ""
    jira_key: str = ""
    jira_url: str = ""
    meet_link: str = ""
    inc_channel_id: str = ""
    inc_channel_name: str = ""
    thread_context: str = ""
    brief_message_ts: str = ""  # ts of the updatable message in inc channel
    announcement_ts: str = ""   # ts of announcement in #incident
    created_at: str = ""
    last_updated: str = ""
    teams_engaged: list = field(default_factory=lambda: ["On-Call SRE"])
    timeline: list = field(default_factory=list)
    fh_id: str = ""
    resolution_summaries_posted: bool = False
    # Original command-center message (for RCCA / keyword matching)
    source_message_text: str = ""
    # Pending RCCA match: raw doc + metadata until POC chooses Yes (then LLM extract) or No
    rcca_match_payload: Optional[dict] = None

    def add_event(self, text: str, user: str = ""):
        ts = datetime.now().strftime("%I:%M %p")
        self.timeline.append(TimelineEvent(timestamp=ts, text=text, user=user))
        self.last_updated = datetime.now().strftime("%b %d, %Y %I:%M %p")

    def status_meta(self) -> dict:
        return STATUS_META.get(self.status, STATUS_META["acknowledged"])


incidents: dict[str, IncidentState] = {}
incidents_by_fh_id: dict[str, IncidentState] = {}


def register_incident(inc: IncidentState) -> None:
    """Index by incident number (string) and FireHydrant id so buttons resolve reliably."""
    key = str(inc.number).strip()
    incidents[key] = inc
    if inc.fh_id:
        incidents_by_fh_id[inc.fh_id] = inc


def resolve_incident_from_action_value(value: str) -> tuple[Optional[IncidentState], str]:
    """
    Slack button value formats:
      - legacy: "{number}|{status_or_severity}"
      - current: "{number}|{fh_id}|{status_or_severity}"
    """
    value = (value or "").strip()
    if not value or "|" not in value:
        return None, ""
    parts = value.split("|", 2)
    new_value = parts[-1].strip()

    def by_num(num_s: str) -> Optional[IncidentState]:
        num_s = num_s.strip()
        inc = incidents.get(num_s)
        if inc:
            return inc
        try:
            return incidents.get(str(int(num_s)))
        except ValueError:
            return None

    if len(parts) == 2:
        return by_num(parts[0]), new_value
    if len(parts) == 3:
        inc_num, fh_id, _ = parts[0].strip(), parts[1].strip(), new_value
        inc = by_num(inc_num)
        if not inc and fh_id:
            inc = incidents_by_fh_id.get(fh_id)
        return inc, new_value
    return None, ""


SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json",
}


# ═══════════════════════════════════════════════
# Utility
# ═══════════════════════════════════════════════

def slugify(text: str, max_len: int = 50) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len]


def now_display() -> str:
    return datetime.now().strftime("%b %d, %Y %I:%M %p")


def now_short() -> str:
    return datetime.now().strftime("%I:%M %p")


# ═══════════════════════════════════════════════
# Slack API Helpers
# ═══════════════════════════════════════════════

def slack_get(endpoint: str, params: dict = None) -> dict:
    return requests.get(f"https://slack.com/api/{endpoint}", headers=SLACK_HEADERS, params=params or {}).json()


def slack_post(endpoint: str, payload: dict) -> dict:
    return requests.post(f"https://slack.com/api/{endpoint}", headers=SLACK_HEADERS, json=payload).json()


def get_message(channel: str, ts: str) -> str:
    data = slack_get("conversations.history", {"channel": channel, "latest": ts, "inclusive": True, "limit": 1})
    return data["messages"][0].get("text", "") if data.get("ok") and data.get("messages") else ""


def get_message_author_id(channel: str, ts: str) -> str:
    """User ID of the person who posted the message (request raiser in #commandcenter)."""
    data = slack_get("conversations.history", {"channel": channel, "latest": ts, "inclusive": True, "limit": 1})
    if data.get("ok") and data.get("messages"):
        return data["messages"][0].get("user", "") or ""
    return ""


def get_thread_context(channel: str, ts: str, limit: int = 15) -> str:
    data = slack_get("conversations.replies", {"channel": channel, "ts": ts, "limit": limit})
    if data.get("ok") and data.get("messages"):
        return "\n".join(m.get("text", "") for m in data["messages"])
    return ""


def get_incident_channel_transcript(channel_id: str, limit: int = 100) -> str:
    """Chronological text from the dedicated incident Slack channel (where the team actually discusses the incident)."""
    if not channel_id:
        return ""
    data = slack_get(
        "conversations.history",
        {"channel": channel_id, "limit": min(limit, 200)},
    )
    if not data.get("ok"):
        print(f"[Slack] conversations.history incident channel: {data.get('error', data)}")
        return ""
    messages = data.get("messages") or []
    messages = list(reversed(messages))
    name_cache: dict[str, str] = {}
    lines: list[str] = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        uid = m.get("user")
        if uid:
            if uid not in name_cache:
                name_cache[uid] = get_user_name(uid)
            lines.append(f"{name_cache[uid]}: {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def get_user_name(user_id: str) -> str:
    data = slack_get("users.info", {"user": user_id})
    return data["user"].get("real_name", user_id) if data.get("ok") else user_id


def send_message(channel: str, text: str, thread_ts: str = None) -> dict:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_post("chat.postMessage", payload)


def send_blocks(channel: str, text: str, blocks: list, thread_ts: str = None) -> dict:
    payload: dict = {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_post("chat.postMessage", payload)


def send_ephemeral(channel: str, user_id: str, text: str) -> dict:
    return slack_post("chat.postEphemeral", {"channel": channel, "user": user_id, "text": text})


def _post_rcca_owner_prompt(inc: IncidentState, match: dict) -> None:
    """Notify POC in the incident channel as a standalone message (not threaded under the brief)."""
    if not inc.inc_channel_id:
        return
    inc.rcca_match_payload = {**match}
    src = match.get("source_file_name") or "RCCA records"
    score = match.get("similarity_score")
    score_h = f" _(relevance {score})_" if score is not None else ""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"📬 <@{inc.commander_id}> I found a *relevant past incident* in our RCCA library "
                    f"(`{src}`){score_h}, similar to this one.\n\n"
                    "Would you like me to *fetch a summary and possible solutions* from that incident and post them here?\n\n"
                    "_Nothing is fetched until you choose._"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"rcca_{inc.number}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes, fetch summary"},
                    "style": "primary",
                    "action_id": "rcca_share_yes",
                    "value": f"{inc.number}|{inc.fh_id}|rcca_yes",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "No"},
                    "action_id": "rcca_share_no",
                    "value": f"{inc.number}|{inc.fh_id}|rcca_no",
                },
            ],
        },
    ]
    send_blocks(
        inc.inc_channel_id,
        "Similar past incident — your choice",
        blocks,
    )


def _rcca_feature_enabled() -> bool:
    """RCCA lookup runs if a local sync folder is set OR Google Drive API mode is enabled."""
    from integrations.gdrive_rcca import _gdrive_enabled
    from integrations.local_rcca import local_rcca_configured

    return local_rcca_configured() or _gdrive_enabled()


def _rcca_background_lookup(inc_number: str) -> None:
    from integrations.gdrive_rcca import search_similar_rcca

    if not _rcca_feature_enabled():
        return
    inc = incidents.get(str(inc_number))
    if not inc:
        return
    match = search_similar_rcca(
        title=inc.title,
        message_text=inc.source_message_text or "",
        thread_context=inc.thread_context or "",
        impact_summary=inc.impact_summary,
        ollama_model=OLLAMA_MODEL,
        ollama_host=OLLAMA_HOST,
        ollama_generate_fn=llm_generate,
    )
    inc = incidents.get(str(inc_number))
    if not match or not inc:
        return
    if inc.rcca_match_payload is not None:
        return
    _post_rcca_owner_prompt(inc, match)


def _nr_background_monitor(inc_number: str, stop_ev: threading.Event) -> None:
    from integrations.newrelic_data import _nr_enabled, fetch_live_incident_snapshot_safe

    if not _nr_enabled():
        return
    interval = max(60, int(os.getenv("NR_MONITOR_INTERVAL_SEC", "300")))
    while not stop_ev.is_set():
        inc = incidents.get(str(inc_number))
        if not inc or inc.status == "resolved":
            break
        svc = ""
        if isinstance(inc.impact_summary, dict):
            svc = str(inc.impact_summary.get("service") or "")
        text = fetch_live_incident_snapshot_safe(svc or "production")
        if text and inc.inc_channel_id and inc.brief_message_ts:
            send_message(inc.inc_channel_id, text, thread_ts=inc.brief_message_ts)
        if stop_ev.wait(timeout=interval):
            break


def update_blocks(channel: str, ts: str, text: str, blocks: list) -> dict:
    return slack_post("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False})


def get_permalink(channel: str, ts: str) -> str:
    data = slack_get("chat.getPermalink", {"channel": channel, "message_ts": ts})
    return data.get("permalink", "") if data.get("ok") else ""


def create_channel(name: str) -> dict:
    data = slack_post("conversations.create", {"name": name[:80]})
    if data.get("ok"):
        print(f"[Slack] Created #{name}")
        return data["channel"]
    print(f"[Slack] Channel error: {data.get('error')}")
    return {}


def set_topic(channel_id: str, topic: str):
    slack_post("conversations.setTopic", {"channel": channel_id, "topic": topic[:250]})


def invite_user(channel_id: str, user_id: str):
    slack_post("conversations.invite", {"channel": channel_id, "users": user_id})


# ═══════════════════════════════════════════════
# Ollama LLM
# ═══════════════════════════════════════════════

def llm_generate(
    prompt: str,
    timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Call Ollama; try /api/generate then /api/chat (Llama 3+ often fills `message.content` only on chat)."""
    t = timeout if timeout is not None else OLLAMA_TIMEOUT
    base = OLLAMA_HOST.rstrip("/")
    gen_body: dict = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    chat_body: dict = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if max_tokens is not None:
        gen_body["options"] = {"num_predict": max_tokens}
        chat_body["options"] = {"num_predict": max_tokens}
    try:
        r = httpx.post(
            f"{base}/api/generate",
            json=gen_body,
            timeout=t,
        )
        data = r.json() if r.content else {}
        if r.status_code != 200:
            print(f"[Ollama] /api/generate HTTP {r.status_code}: {str(data)[:500]}")
        err = data.get("error")
        if err:
            print(f"[Ollama] /api/generate error: {err}")
        out = (data.get("response") or "").strip().strip('"').strip("'")
        if out:
            return out

        r2 = httpx.post(
            f"{base}/api/chat",
            json=chat_body,
            timeout=t,
        )
        d2 = r2.json() if r2.content else {}
        if r2.status_code != 200:
            print(f"[Ollama] /api/chat HTTP {r2.status_code}: {str(d2)[:500]}")
        if d2.get("error"):
            print(f"[Ollama] /api/chat error: {d2.get('error')}")
        msg = d2.get("message")
        if isinstance(msg, dict):
            out2 = (msg.get("content") or "").strip().strip('"').strip("'")
            if out2:
                return out2
        print(f"[Ollama] Empty model output (check model name `OLLAMA_MODEL={OLLAMA_MODEL}` and `ollama pull {OLLAMA_MODEL}`).")
    except Exception as e:
        print(f"[Ollama] Error: {e}")
    return ""


def _parse_incident_draft_block(raw: str) -> Optional[tuple[str, dict, str]]:
    """Parse single-call SRE draft (TITLE/SCOPE/SERVICE/USER_IMPACT/SYMPTOMS/STATUS lines)."""
    data: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        k = key.strip().upper().replace(" ", "_").strip("*_")
        val = rest.strip().strip("*")
        if k in ("USER_IMPACT", "USERIMPACT") or (k.startswith("USER") and "IMPACT" in k):
            data["USER_IMPACT"] = val
        elif k in ("TITLE", "SCOPE", "SERVICE", "SYMPTOMS", "STATUS"):
            data[k] = val
    title = (data.get("TITLE") or "").strip().strip('"').strip("'")
    if len(title) < 3:
        return None
    impact = {
        "scope": (data.get("SCOPE") or "Assessing").strip() or "Assessing",
        "service": (data.get("SERVICE") or "Unknown").strip() or "Unknown",
        "user_impact": (data.get("USER_IMPACT") or "Under investigation").strip() or "Under investigation",
        "symptoms": (data.get("SYMPTOMS") or "Under investigation").strip() or "Under investigation",
    }
    status = (data.get("STATUS") or "Investigation in progress.").strip() or "Investigation in progress."
    return title, impact, status


def generate_incident_draft_fast(message: str, context: str = "") -> Optional[tuple[str, dict, str]]:
    """One Ollama round-trip for title + impact + status (much faster than 3 sequential calls)."""
    ctx = (context or "")[:2200]
    msg = (message or "")[:2200]
    block = f"Thread (excerpt):\n{ctx}\n\nPrimary message:\n{msg}" if ctx.strip() else f"Message:\n{msg}"
    prompt = (
        "You are an SRE. From the incident text below, output EXACTLY 6 lines. "
        "Each line must start with one of these keywords, a colon, then a space, then the value. "
        "Use the keyword exactly as written.\n"
        "TITLE: <5-8 words, no quotes>\n"
        "SCOPE: <Global|Regional|Local or best guess>\n"
        "SERVICE: <affected system or Unknown>\n"
        "USER_IMPACT: <one sentence>\n"
        "SYMPTOMS: <one sentence>\n"
        "STATUS: <one sentence for stakeholders>\n"
        "No other lines. No markdown.\n\n"
        f"{block}"
    )
    raw = llm_generate(prompt, timeout=OLLAMA_DRAFT_TIMEOUT, max_tokens=320)
    parsed = _parse_incident_draft_block(raw)
    if not parsed:
        print("[Ollama] Combined draft parse failed, falling back to parallel calls.")
    return parsed


def generate_incident_draft_parallel(message: str, context: str = "") -> tuple[str, dict, str]:
    """Three LLM calls at once (helps when single-call format fails; still faster than sequential)."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_title = pool.submit(
            generate_title, message, context, OLLAMA_DRAFT_TIMEOUT, 80
        )
        f_impact = pool.submit(
            generate_impact, message, context, OLLAMA_DRAFT_TIMEOUT, 200
        )
        f_status = pool.submit(
            generate_status_summary, message, context, OLLAMA_DRAFT_TIMEOUT, 120
        )
        title = f_title.result()
        impact = f_impact.result()
        status_summary = f_status.result()
    return title, impact, status_summary


def generate_title(
    message: str,
    context: str = "",
    llm_timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    full = f"Thread:\n{context}\n\nMessage:\n{message}" if context else message
    t = llm_timeout if llm_timeout is not None else OLLAMA_DRAFT_TIMEOUT
    result = llm_generate(
        "You are an SRE incident manager. Generate a concise incident title in 5-7 words. "
        f"Return ONLY the title.\n\n{full}",
        timeout=t,
        max_tokens=max_tokens or 96,
    )
    return result or "Incident reported via Slack"


def generate_impact(
    message: str,
    context: str = "",
    llm_timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    full = f"{message}\n{context}" if context else message
    t = llm_timeout if llm_timeout is not None else OLLAMA_DRAFT_TIMEOUT
    result = llm_generate(
        "Based on this incident, respond with EXACTLY 4 lines, no labels, no bullets:\n"
        "Line 1: Scope (Global/Regional/Local)\n"
        "Line 2: Affected service name\n"
        "Line 3: User impact in one sentence\n"
        "Line 4: Key symptom in one sentence\n\n"
        f"{full}",
        timeout=t,
        max_tokens=max_tokens or 220,
    )
    lines = [l.strip() for l in result.split("\n") if l.strip()]
    return {
        "scope": lines[0] if len(lines) > 0 else "Assessing",
        "service": lines[1] if len(lines) > 1 else "Unknown",
        "user_impact": lines[2] if len(lines) > 2 else "Under investigation",
        "symptoms": lines[3] if len(lines) > 3 else "Under investigation",
    }


def slack_escape_mrkdwn(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_resolution_summaries(inc: IncidentState) -> tuple[str, str]:
    """Ollama: issue summary + solution summary for resolved incidents."""
    timeline_txt = "\n".join(e.display() for e in inc.timeline[-25:])
    impact = inc.impact_summary
    if isinstance(impact, dict):
        impact = json.dumps(impact, indent=2)
    # Investigation happens in the incident channel — not in the original #commandcenter thread alone.
    channel_log = get_incident_channel_transcript(inc.inc_channel_id, limit=100)
    channel_log = channel_log[:7000]
    origin_thread = (inc.thread_context or "")[:2500]
    ctx = (
        f"Title: {inc.title}\nSeverity: {inc.severity}\n"
        f"Impact / context:\n{impact}\n\nStatus note:\n{inc.current_status_text}\n\n"
        f"Original request / command-center thread:\n{origin_thread}\n\n"
        f"Incident channel discussion (primary source for what happened and the fix):\n{channel_log or '(no channel messages fetched)'}\n\n"
        f"Status / timeline from the bot:\n{timeline_txt}"
    )
    issue = llm_generate(
        "You are writing an internal incident report. Based on the information below, produce a concise "
        "Issue Summary: what failed, scope, and customer/user impact. "
        "Use 2-5 short bullet lines starting with - or one tight paragraph. Output plain text only, no title line.\n\n"
        + ctx,
        timeout=OLLAMA_TIMEOUT,
    )
    solution = llm_generate(
        "Using the same incident information below, write a concise Solution Summary: root cause if stated, "
        "mitigations, verification, and how it was resolved. "
        "Use 2-5 short bullet lines starting with - or one tight paragraph. Output plain text only, no title line.\n\n"
        + ctx,
        timeout=OLLAMA_TIMEOUT,
    )
    return (
        (issue or "Unable to generate issue summary (Ollama empty response).").strip(),
        (solution or "Unable to generate solution summary (Ollama empty response).").strip(),
    )


def generate_status_summary(
    message: str,
    context: str = "",
    llm_timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    full = f"{message}\n{context}" if context else message
    t = llm_timeout if llm_timeout is not None else OLLAMA_DRAFT_TIMEOUT
    result = llm_generate(
        "Summarize this incident in one concise sentence for a status update. "
        f"Return ONLY the sentence.\n\n{full}",
        timeout=t,
        max_tokens=max_tokens or 120,
    )
    return result or "Investigation in progress."


# ═══════════════════════════════════════════════
# FireHydrant
# ═══════════════════════════════════════════════

def fh_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('FIREHYDRANT_API_KEY')}", "Content-Type": "application/json"}


def fh_create_incident(title: str, summary: str) -> dict:
    try:
        print(f"[FH] Creating: {title}")
        resp = requests.post("https://api.firehydrant.io/v1/incidents", headers=fh_headers(), json={"name": title, "summary": summary})
        result = resp.json()
        print(f"[FH] id={result.get('id', 'N/A')}, number={result.get('number', 'N/A')}")
        return result
    except Exception as e:
        print(f"[FH] Error: {e}")
        return {"error": str(e)}


def _parse_fh_time(s: str) -> datetime:
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _fmt_fh_time(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def fh_list_milestones(incident_id: str) -> list:
    try:
        r = requests.get(
            f"https://api.firehydrant.io/v1/incidents/{incident_id}/milestones",
            headers=fh_headers(),
            timeout=30,
        )
        data = r.json()
        if not r.ok:
            print(f"[FH] list milestones HTTP {r.status_code}: {str(data)[:400]}")
            return []
        return data.get("data") or []
    except Exception as e:
        print(f"[FH] list milestones error: {e}")
        return []


def fh_sync_milestones_to(incident_id: str, target_slug: str) -> bool:
    """
    FireHydrant expects PUT .../milestones/bulk_update with a milestones[] array.
    Milestones must be chronological; we merge existing occurred_at from FH and backfill gaps.
    """
    order = FH_MILESTONE_ORDER
    if not order:
        print("[FH] FH_MILESTONE_ORDER empty — set FH_MILESTONE_SLUGS in .env")
        return False
    if target_slug not in order:
        print(f"[FH] target milestone {target_slug!r} not in configured order {order}")
        return False
    target_idx = order.index(target_slug)
    chain = order[: target_idx + 1]

    existing_rows = fh_list_milestones(incident_id)
    by_type: dict[str, str] = {}
    for row in existing_rows:
        t, oa = row.get("type"), row.get("occurred_at")
        if t and oa:
            by_type[t] = oa

    now = datetime.utcnow().replace(microsecond=0)
    milestones_payload: list[dict] = []
    last_dt: Optional[datetime] = None

    for i, slug in enumerate(chain):
        if slug in by_type:
            milestones_payload.append({"type": slug, "occurred_at": by_type[slug]})
            try:
                last_dt = _parse_fh_time(by_type[slug])
            except Exception:
                last_dt = None
        else:
            if last_dt is None:
                ts = now - timedelta(seconds=(len(chain) - i))
            else:
                ts = last_dt + timedelta(seconds=1)
            if ts > now:
                ts = now
            milestones_payload.append({"type": slug, "occurred_at": _fmt_fh_time(ts)})
            last_dt = ts

    url = f"https://api.firehydrant.io/v1/incidents/{incident_id}/milestones/bulk_update"
    try:
        r = requests.put(url, headers=fh_headers(), json={"milestones": milestones_payload}, timeout=30)
        snippet = r.text[:800]
        print(f"[FH] bulk_update → HTTP {r.status_code} {snippet}")
        return r.ok
    except Exception as e:
        print(f"[FH] bulk_update error: {e}")
        return False


# ═══════════════════════════════════════════════
# Jira
# ═══════════════════════════════════════════════

def create_jira_ticket(title: str, description: str, severity: str = "SEV3") -> dict:
    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": f"[{severity}] {title}",
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": description[:2000]}]}
            ]},
            "issuetype": {"name": "Task"},
            "priority": {"name": "High"},
        }
    }
    try:
        resp = requests.post(f"https://{JIRA_DOMAIN}/rest/api/3/issue", auth=(JIRA_EMAIL, JIRA_API_TOKEN), json=payload)
        result = resp.json()
        if result.get("key"):
            key = result["key"]
            print(f"[Jira] Created: {key}")
            return {"key": key, "url": f"https://{JIRA_DOMAIN}/browse/{key}"}
        print(f"[Jira] Error: {json.dumps(result)[:200]}")
        return {"error": str(result)}
    except Exception as e:
        print(f"[Jira] Error: {e}")
        return {"error": str(e)}


# ═══════════════════════════════════════════════
# Dynamic Investigation Links (New Relic NRQL)
# ═══════════════════════════════════════════════

def generate_investigation_links(title: str, service: str = "") -> list[dict]:
    """Generate dynamic NRQL dashboard links based on incident context."""
    svc = service or "production"
    svc_encoded = svc.replace(" ", "+")
    base = "https://one.newrelic.com/launcher/nr1-core.explorer"

    title_lower = title.lower()

    links = []

    if any(w in title_lower for w in ["error", "500", "exception", "failure", "fail"]):
        links.append({
            "label": "📈 Error Rate Dashboard",
            "url": f"{base}?query=name+like+%27{svc_encoded}%27&nrql=SELECT+count(*)+FROM+TransactionError+WHERE+appName+%3D+%27{svc_encoded}%27+TIMESERIES",
        })

    if any(w in title_lower for w in ["slow", "latency", "timeout", "delay", "performance"]):
        links.append({
            "label": "⏱ Latency Dashboard",
            "url": f"{base}?nrql=SELECT+average(duration)+FROM+Transaction+WHERE+appName+%3D+%27{svc_encoded}%27+TIMESERIES",
        })

    if any(w in title_lower for w in ["throughput", "traffic", "load", "capacity", "pool"]):
        links.append({
            "label": "📊 Throughput Dashboard",
            "url": f"{base}?nrql=SELECT+rate(count(*),1+minute)+FROM+Transaction+WHERE+appName+%3D+%27{svc_encoded}%27+TIMESERIES",
        })

    if any(w in title_lower for w in ["database", "db", "connection", "query", "sql"]):
        links.append({
            "label": "🗄 Database Performance",
            "url": f"{base}?nrql=SELECT+average(databaseDuration)+FROM+Transaction+WHERE+appName+%3D+%27{svc_encoded}%27+TIMESERIES",
        })

    if any(w in title_lower for w in ["memory", "cpu", "disk", "resource", "oom"]):
        links.append({
            "label": "💻 Infrastructure Metrics",
            "url": f"{base}?nrql=SELECT+average(cpuPercent),average(memoryUsedPercent)+FROM+SystemSample+TIMESERIES",
        })

    if not links:
        links.append({
            "label": "🔍 APM Overview",
            "url": f"{base}?query=name+like+%27{svc_encoded}%27",
        })
        links.append({
            "label": "📈 Error Rate",
            "url": f"{base}?nrql=SELECT+count(*)+FROM+TransactionError+TIMESERIES",
        })

    return links


# ═══════════════════════════════════════════════
# Slack Message Builder
# ═══════════════════════════════════════════════

def build_thread_discussion_block(inc: IncidentState) -> list[dict]:
    """Build the thread discussion section with quoted messages."""
    if not inc.thread_context:
        return []

    permalink_text = f"  (<{inc.thread_permalink}|View full thread>)" if inc.thread_permalink else ""
    source_label = f"<#{inc.source_channel}>" if inc.source_channel else "#commandcenter"

    lines = inc.thread_context.strip().split("\n")
    quoted = "\n".join(f"> {line.strip()}" for line in lines[:12] if line.strip())

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*💬 Thread Discussion from {source_label}:*{permalink_text}\n{quoted[:2900]}",
            },
        },
        {"type": "divider"},
    ]


def build_status_severity_interactive_blocks(inc: IncidentState) -> list[dict]:
    """Status + severity button rows and hint (shared by incident channel brief and #incident announcement)."""
    buttons = []
    for status_key in STATUSES:
        meta = STATUS_META[status_key]
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": f"{meta['emoji']} {meta['label']}", "emoji": True},
            "value": f"{inc.number}|{inc.fh_id}|{status_key}",
            "action_id": f"status_{status_key}",
        }
        if status_key == inc.status:
            btn["style"] = "primary"
        buttons.append(btn)

    sev_buttons = []
    for sev_key in SEVERITIES:
        smeta = SEVERITY_META[sev_key]
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": f"{smeta['emoji']} {sev_key}", "emoji": True},
            "value": f"{inc.number}|{inc.fh_id}|{sev_key}",
            "action_id": f"severity_{sev_key}",
        }
        if sev_key == inc.severity:
            btn["style"] = "primary"
        sev_buttons.append(btn)

    return [
        {"type": "actions", "block_id": f"status_{inc.number}", "elements": buttons},
        {"type": "actions", "block_id": f"severity_{inc.number}", "elements": sev_buttons},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Update status or severity using the buttons above."}],
        },
    ]


def build_incident_brief(inc: IncidentState) -> list[dict]:
    """Build the updatable incident brief blocks for the incident channel."""
    sm = inc.status_meta()
    impact = inc.impact_summary if isinstance(inc.impact_summary, str) else ""

    # Parse impact dict if stored as string
    impact_text = impact
    if isinstance(inc.impact_summary, dict):
        d = inc.impact_summary
        impact_text = (
            f"*Scope:* {d.get('scope', 'N/A')}  •  *Service:* {d.get('service', 'N/A')}\n"
            f"*User Impact:* {d.get('user_impact', 'N/A')}\n"
            f"*Symptoms:* {d.get('symptoms', 'N/A')}"
        )

    # Investigation links
    service = ""
    if isinstance(inc.impact_summary, dict):
        service = inc.impact_summary.get("service", "")
    inv_links = generate_investigation_links(inc.title, service)
    inv_text = "\n".join(f"• <{l['url']}|{l['label']}>" for l in inv_links)

    # Resources
    res_parts = []
    if inc.thread_permalink:
        res_parts.append(f"• <{inc.thread_permalink}|💬 View Full Original Thread>")
    res_parts.append(f"• <{inc.meet_link}|🎥 Join Google Meet>")
    if inc.jira_key:
        res_parts.append(f"• <{inc.jira_url}|🎫 Jira Ticket: {inc.jira_key}>")
    res_parts.append(f"• <{inc.incident_url}|🖥 FireHydrant Command Center>")

    sev_meta = SEVERITY_META.get(inc.severity, SEVERITY_META["SEV3"])

    blocks = [
        # Header
        {"type": "header", "text": {"type": "plain_text", "text": f"INC-{inc.number}: {inc.title}", "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity:* {sev_meta['emoji']} {inc.severity}"},
                {"type": "mrkdwn", "text": f"*Status:* {sm['emoji']} {sm['label']}"},
                {"type": "mrkdwn", "text": f"*POC:* <@{inc.commander_id}>"},
                {"type": "mrkdwn", "text": f"*Started:* {inc.created_at}"},
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Last updated: {inc.last_updated}  •  Teams: {', '.join(inc.teams_engaged)}"},
            ],
        },
        {"type": "divider"},
        *build_status_severity_interactive_blocks(inc),
        {"type": "divider"},

        # Impact
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🎯 Impact*\n{impact_text}"}},
        {"type": "divider"},

        # Thread Discussion
        *build_thread_discussion_block(inc),

        # Resources
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🔗 Resources*\n" + "\n".join(res_parts)}},
        {"type": "divider"},

        # Investigation Links
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔬 Investigation Links*\n{inv_text}"}},
    ]
    return blocks


def build_announcement_blocks(inc: IncidentState) -> list[dict]:
    """#incident: same summary card as brief top (screenshot layout), then status/severity buttons, then quick links."""
    sm = inc.status_meta()
    sev_meta = SEVERITY_META.get(inc.severity, SEVERITY_META["SEV3"])

    res_elements = [
        {"type": "mrkdwn", "text": f"<{inc.incident_url}|Command Center>"},
        {"type": "mrkdwn", "text": f"<#{inc.inc_channel_id}|{inc.inc_channel_name}>"},
    ]
    if inc.jira_key:
        res_elements.append({"type": "mrkdwn", "text": f"<{inc.jira_url}|{inc.jira_key}>"})
    res_elements.append({"type": "mrkdwn", "text": f"<{inc.meet_link}|Google Meet>"})

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"INC-{inc.number}: {inc.title}", "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity:* {sev_meta['emoji']} {inc.severity}"},
                {"type": "mrkdwn", "text": f"*Status:* {sm['emoji']} {sm['label']}"},
                {"type": "mrkdwn", "text": f"*POC:* <@{inc.commander_id}>"},
                {"type": "mrkdwn", "text": f"*Started:* {inc.created_at}"},
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Last updated: {inc.last_updated}  •  Teams: {', '.join(inc.teams_engaged)}"},
            ],
        },
        {"type": "divider"},
        *build_status_severity_interactive_blocks(inc),
        {"type": "divider"},
        {"type": "context", "elements": res_elements},
    ]
    return blocks


# ═══════════════════════════════════════════════
# Status Update Handler
# ═══════════════════════════════════════════════

@app.post("/slack/interactivity")
async def slack_interactivity(req: Request):
    form = await req.form()
    payload = json.loads(form.get("payload", "{}"))

    if payload.get("type") != "block_actions":
        return Response(status_code=200)

    action = payload["actions"][0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")
    user = payload.get("user", {})
    user_name = user.get("real_name", user.get("username", "Unknown"))
    user_id_slack = user.get("id", "")
    channel_id = payload.get("channel", {}).get("id", "")

    # ── RCCA share (POC only) — before status/severity routing ──
    if action_id in ("rcca_share_yes", "rcca_share_no"):
        parts = value.split("|", 2)
        if len(parts) != 3:
            return Response(status_code=200)
        inc_num, fh_id, _dec = parts[0].strip(), parts[1].strip(), parts[2].strip()
        rcc_inc = incidents.get(inc_num) or incidents_by_fh_id.get(fh_id)
        if not rcc_inc:
            try:
                rcc_inc = incidents.get(str(int(inc_num)))
            except ValueError:
                pass
        if not rcc_inc:
            return Response(status_code=200)
        if not rcc_inc.rcca_match_payload:
            if user_id_slack:
                send_ephemeral(channel_id, user_id_slack, "This RCCA prompt was already answered or is no longer valid.")
            return Response(status_code=200)
        if user_id_slack and user_id_slack != rcc_inc.commander_id:
            send_ephemeral(channel_id, user_id_slack, "Only the incident owner (POC) can confirm RCCA sharing.")
            return Response(status_code=200)

        from integrations.gdrive_rcca import _extract_rcca_fields, build_rcca_summary_blocks

        if action_id == "rcca_share_yes":
            pay = dict(rcc_inc.rcca_match_payload)
            raw = (pay.get("rcca_raw_doc_text") or "").strip()
            if raw:
                fields = _extract_rcca_fields(raw, llm_generate)
                merged = {
                    **fields,
                    "source_file_name": pay.get("source_file_name", "RCCA"),
                    "similarity_score": pay.get("similarity_score"),
                }
            else:
                merged = pay
            if rcc_inc.inc_channel_id:
                blocks = build_rcca_summary_blocks(merged)
                send_blocks(
                    rcc_inc.inc_channel_id,
                    "Related past incident (RCCA summary)",
                    blocks,
                )
            rcc_inc.add_event("📎 Past RCCA summary & fixes fetched and shared (POC confirmed)", user_name)
        else:
            pass
        rcc_inc.rcca_match_payload = None
        return Response(status_code=200)

    inc, new_value = resolve_incident_from_action_value(value)
    if not inc:
        send_message(
            channel_id,
            "⚠️ *Incident not found in app memory.*\n"
            "This usually happens after the server restarted (state is in RAM) or when running "
            "*multiple uvicorn workers* (each worker has its own memory). "
            "Fix: run a single process, e.g. `uvicorn app:app --host 0.0.0.0 --port 8000` "
            "with no `--workers 2+`, then use buttons on a *new* incident message.",
        )
        return Response(status_code=200)
    if not new_value:
        return Response(status_code=200)

    # ── Handle severity change ──
    if action_id.startswith("severity_"):
        if new_value == inc.severity:
            return Response(status_code=200)

        old_sev = inc.severity
        new_sev_meta = SEVERITY_META.get(new_value, SEVERITY_META["SEV3"])
        inc.severity = new_value
        inc.add_event(f"{new_sev_meta['emoji']} Severity → *{new_value}*", user_name)

        if inc.brief_message_ts and inc.inc_channel_id:
            blocks = build_incident_brief(inc)
            update_blocks(inc.inc_channel_id, inc.brief_message_ts, f"INC-{inc.number}: {inc.title}", blocks)

        send_message(
            inc.inc_channel_id,
            f"{new_sev_meta['emoji']} *Severity → {new_value}* (was {old_sev}) by {user_name} at {now_short()}",
        )

        if inc.announcement_ts and SLACK_INCIDENT_CHANNEL_ID:
            ann_blocks = build_announcement_blocks(inc)
            update_blocks(SLACK_INCIDENT_CHANNEL_ID, inc.announcement_ts, f"INC-{inc.number}: {inc.title}", ann_blocks)

        return Response(status_code=200)

    # ── Handle status change ──
    if not action_id.startswith("status_"):
        return Response(status_code=200)

    new_status = STATUS_LEGACY_MAP.get(new_value, new_value)
    if new_status not in STATUS_META:
        return Response(status_code=200)

    if inc.status in STATUS_LEGACY_MAP:
        inc.status = STATUS_LEGACY_MAP[inc.status]

    old_status = inc.status
    if new_status == inc.status:
        return Response(status_code=200)

    old_meta = STATUS_META.get(old_status, STATUS_META["acknowledged"])
    new_meta = STATUS_META[new_status]

    inc.status = new_status
    inc.add_event(f"{new_meta['emoji']} Status → *{new_meta['label']}*", user_name)

    if new_status == "resolved":
        inc.add_event("🎉 Incident resolved", user_name)
        stop_incident_insight_jobs(str(inc.number))

    fh_ok = True
    if inc.fh_id:
        fh_ok = fh_sync_milestones_to(inc.fh_id, new_meta["fh_milestone"])
        if not fh_ok and inc.inc_channel_id:
            send_message(
                inc.inc_channel_id,
                "⚠️ Slack status updated but FireHydrant milestone sync failed. "
                "Check server logs and `FH_MILESTONE_SLUGS` vs your org’s milestone names in FireHydrant.",
            )

    if inc.brief_message_ts and inc.inc_channel_id:
        blocks = build_incident_brief(inc)
        update_blocks(inc.inc_channel_id, inc.brief_message_ts, f"INC-{inc.number}: {inc.title}", blocks)

    send_message(
        inc.inc_channel_id,
        f"{new_meta['emoji']} *Status → {new_meta['label']}* by {user_name} at {now_short()}",
    )

    if inc.announcement_ts and SLACK_INCIDENT_CHANNEL_ID:
        ann_blocks = build_announcement_blocks(inc)
        update_blocks(SLACK_INCIDENT_CHANNEL_ID, inc.announcement_ts, f"INC-{inc.number}: {inc.title}", ann_blocks)

    if new_status == "resolved" and old_status != "resolved" and not inc.resolution_summaries_posted:
        inc.resolution_summaries_posted = True
        issue_sum, sol_sum = generate_resolution_summaries(inc)
        summary_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🎉 Resolved — AI summaries", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📋 Issue summary*\n{slack_escape_mrkdwn(issue_sum)}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*✅ Solution summary*\n{slack_escape_mrkdwn(sol_sum)}"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Generated with Ollama — review and copy to FireHydrant / Jira as needed._",
                    }
                ],
            },
        ]
        send_blocks(inc.inc_channel_id, f"INC-{inc.number} resolved — issue & solution summary", summary_blocks)
        send_message(
            inc.inc_channel_id,
            "🎉 *Incident Resolved*\n"
            "• Retrospective / RCA can be tracked in Jira\n"
            "• Channel can be archived after review",
        )

    return Response(status_code=200)


# ═══════════════════════════════════════════════
# Main Routes
# ═══════════════════════════════════════════════

def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


@app.get("/")
def root():
    return {"message": "Zero-Touch Incident Automation Server"}


@app.post("/slack/events")
async def slack_events(req: Request):
    global incident_counter

    body_bytes = await req.body()
    body = await req.json()

    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if ts and sig and not verify_slack_signature(body_bytes, ts, sig):
        return Response(content="Invalid signature", status_code=403)

    event_id = body.get("event_id", "")
    if event_id in processed_events:
        return {"ok": True}
    processed_events.add(event_id)

    event = body.get("event", {})
    if event.get("type") != "reaction_added" or event.get("reaction") != TRIGGER_EMOJI:
        return {"ok": True}

    channel = event["item"]["channel"]
    message_ts = event["item"]["ts"]
    user_id = event["user"]

    reactor_id = user_id  # who added 🚨
    # POC = author of the message (who raised the request in #commandcenter), not the reactor
    poc_user_id = event.get("item_user") or get_message_author_id(channel, message_ts) or reactor_id
    poc_name = get_user_name(poc_user_id)
    reactor_name = get_user_name(reactor_id)

    print(f"🚨 Trigger: reactor={reactor_id}, POC (request author)={poc_user_id} in {channel}")

    # ── 1. Gather context ──
    message_text = get_message(channel, message_ts)
    if not message_text:
        send_message(channel, "⚠️ Could not fetch the original message.", message_ts)
        return {"ok": True}

    thread_context = get_thread_context(channel, message_ts)

    send_message(channel, "🤖 Incident detected! Generating AI brief and creating incident...", message_ts)

    # ── 2. AI generation (one combined Ollama call by default — ~3× faster than 3 sequential calls)
    title: str
    impact: dict
    status_summary: str
    if OLLAMA_USE_SINGLE_DRAFT:
        bundle = generate_incident_draft_fast(message_text, thread_context)
        if bundle:
            title, impact, status_summary = bundle
        else:
            title, impact, status_summary = generate_incident_draft_parallel(message_text, thread_context)
    else:
        title, impact, status_summary = generate_incident_draft_parallel(message_text, thread_context)
    title_slug = slugify(title)

    # ── 3. FireHydrant + Jira in parallel (independent HTTP; saves wall-clock vs sequential)
    who_line = f"Raised by {poc_name}"
    if poc_user_id != reactor_id:
        who_line += f", declared by {reactor_name}"
    summary = f"{who_line}. {message_text}"[:250]
    jira_lines = [f"POC / Request raised by: {poc_name}", f"Message: {message_text}"]
    if poc_user_id != reactor_id:
        jira_lines.insert(1, f"Incident declared by (emoji): {reactor_name}")
    jira_lines.append(f"Thread:\n{thread_context}")
    jira_desc = "\n\n".join(jira_lines)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_fh = pool.submit(fh_create_incident, title, summary)
        fut_jira = pool.submit(create_jira_ticket, title, jira_desc)
        fh = fut_fh.result()
        jira = fut_jira.result()

    if not fh.get("id"):
        send_message(channel, f"⚠️ FireHydrant error: {fh.get('messages', fh.get('error'))}", message_ts)
        return {"ok": True}

    inc_number = fh.get("number", incident_counter + 1000)
    incident_counter += 1
    incident_url = fh.get("incident_url", f"https://app.firehydrant.io/incidents/{fh['id']}")

    # ── 5. Slack channel ──
    date_str = datetime.now().strftime("%Y%m%d")
    ch = create_channel(f"inc{inc_number}-{date_str}-{title_slug}")
    inc_channel_id = ch.get("id", "")
    inc_channel_name = ch.get("name", f"inc{inc_number}")

    # ── 6. Permalink + Meet ──
    thread_permalink = get_permalink(channel, message_ts)
    meet_link = "https://meet.google.com/new"

    # ── 7. Build state ──
    inc = IncidentState(
        id=fh["id"],
        fh_id=fh["id"],
        number=inc_number,
        title=title,
        severity="SEV3",
        commander_id=poc_user_id,
        commander_name=poc_name,
        impact_summary=impact,
        current_status_text=status_summary,
        thread_context=thread_context,
        source_message_text=message_text,
        source_channel=channel,
        source_message_ts=message_ts,
        thread_permalink=thread_permalink,
        incident_url=incident_url,
        jira_key=jira.get("key", ""),
        jira_url=jira.get("url", ""),
        meet_link=meet_link,
        inc_channel_id=inc_channel_id,
        inc_channel_name=inc_channel_name,
        created_at=now_display(),
        last_updated=now_display(),
    )

    decl_user = reactor_name if poc_user_id != reactor_id else poc_name
    inc.add_event("🚨 Incident declared via emoji reaction", decl_user)
    inc.add_event(f"📋 FireHydrant incident created (#{inc_number})")
    if jira.get("key"):
        inc.add_event(f"🎫 Jira ticket created ({jira['key']})")
    inc.add_event(f"💬 Incident channel <#{inc_channel_id}> created")

    register_incident(inc)

    if inc.fh_id:
        fh_sync_milestones_to(inc.fh_id, "acknowledged")

    # ── 8. Set up channel ──
    if inc_channel_id:
        topic = f"🚨 {title} | SEV3 | {inc.status_meta()['label']}"
        if inc.jira_key:
            topic += f" | {inc.jira_key}"
        set_topic(inc_channel_id, topic)
        to_invite = [poc_user_id]
        if reactor_id != poc_user_id:
            to_invite.append(reactor_id)
        invite_user(inc_channel_id, ",".join(to_invite))

    # ── 9–10. Post brief + #incident announcement in parallel (two Slack posts)
    ann_blocks = build_announcement_blocks(inc)
    if inc_channel_id:
        blocks = build_incident_brief(inc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_brief = pool.submit(
                send_blocks, inc_channel_id, f"INC-{inc_number}: {title}", blocks
            )
            fut_ann = pool.submit(
                send_blocks, SLACK_INCIDENT_CHANNEL_ID, f"🚨 INC-{inc_number}: {title}", ann_blocks
            )
            resp = fut_brief.result()
            ann_resp = fut_ann.result()
        inc.brief_message_ts = resp.get("ts", "")
        inc.announcement_ts = ann_resp.get("ts", "")
    else:
        ann_resp = send_blocks(SLACK_INCIDENT_CHANNEL_ID, f"🚨 INC-{inc_number}: {title}", ann_blocks)
        inc.announcement_ts = ann_resp.get("ts", "")

    send_message(
        SLACK_INCIDENT_CHANNEL_ID,
        f"A formal incident has been declared. Join <#{inc_channel_id}|{inc_channel_name}> for updates.",
    )

    # ── 11. Source thread confirmation ──
    jira_line = f"🎫 <{inc.jira_url}|{inc.jira_key}>\n" if inc.jira_key else ""
    poc_line = f"👤 *POC for Incident:* <@{poc_user_id}> ({poc_name})\n"
    if poc_user_id != reactor_id:
        poc_line += f"_Declared via 🚨 by <@{reactor_id}> ({reactor_name})_\n"
    sm0 = inc.status_meta()
    sev0 = SEVERITY_META.get(inc.severity, SEVERITY_META["SEV3"])
    send_message(
        channel,
        f"✅ *Incident Declared — INC-{inc_number}*\n\n"
        f"*{title}*  •  {sev0['emoji']} {inc.severity}  •  {sm0['emoji']} {sm0['label']}\n\n"
        f"{poc_line}"
        f"{jira_line}"
        f"🎥 <{meet_link}|Google Meet>\n\n"
        f"➡️ _All coordination in <#{inc_channel_id}|{inc_channel_name}>_",
        message_ts,
    )

    # ── Optional: Google Drive RCCA lookup + New Relic interval monitor (non-blocking) ──
    _inc_key = str(inc_number)
    start_incident_insight_jobs(
        _inc_key,
        get_incident=lambda k=_inc_key: incidents.get(k),
        run_rcca_lookup=lambda k=_inc_key: _rcca_background_lookup(k),
        run_nr_monitor=lambda ev, k=_inc_key: _nr_background_monitor(k, ev),
    )

    return {"ok": True}


@app.get("/health")
def health():
    ollama_ok = False
    try:
        ollama_ok = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0).status_code == 200
    except Exception:
        pass

    gdrive_on = os.getenv("GDRIVE_RCCA_ENABLED", "").lower() in ("1", "true", "yes")
    nr_on = os.getenv("NEW_RELIC_ENABLED", "").lower() in ("1", "true", "yes")

    from integrations.gdrive_rcca import _ollama_embed, _rcca_embed_model
    from integrations.local_rcca import check_local_rcca_connectivity

    rcca_local = check_local_rcca_connectivity()
    rcca_mode = "local_folder" if rcca_local.get("configured") and rcca_local.get("is_dir") else None
    if not rcca_mode and gdrive_on:
        rcca_mode = "drive_api"

    rcca_embed_ok: Optional[bool] = None
    if ollama_ok and rcca_mode and rcca_mode != "off":
        em = _rcca_embed_model(os.getenv("OLLAMA_MODEL", "llama3"))
        rcca_embed_ok = _ollama_embed("health", em, OLLAMA_HOST) is not None

    return {
        "server": "running",
        "ollama": "connected" if ollama_ok else "disconnected",
        "slack": "configured" if SLACK_BOT_TOKEN else "missing",
        "firehydrant": "configured" if FIREHYDRANT_API_KEY else "missing",
        "jira": "configured" if JIRA_API_TOKEN else "missing",
        "gdrive_rcca": "enabled" if gdrive_on else "off",
        "rcca_local_folder": rcca_local,
        "rcca_lookup": rcca_mode or "off",
        "rcca_ollama_embeddings": rcca_embed_ok,
        "new_relic_monitor": "enabled" if nr_on else "off",
        "active_incidents": len(incidents),
    }
