"""
Google Drive (read-only) RCCA / past incident docs + semantic match via Ollama embeddings.
Disabled unless GDRIVE_RCCA_ENABLED=true and credentials + folder ID are set.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
from typing import Any, Optional

import httpx

# Lazy-loaded clients
_drive_service = None
_creds = None


def _gdrive_enabled() -> bool:
    return os.getenv("GDRIVE_RCCA_ENABLED", "").lower() in ("1", "true", "yes")


def _get_drive_service():
    global _drive_service, _creds
    if _drive_service is not None:
        return _drive_service
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as e:
        print(f"[GDRIVE] Missing google packages: {e}")
        return None

    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not path or not os.path.isfile(path):
        print("[GDRIVE] Set GOOGLE_APPLICATION_CREDENTIALS or GDRIVE_SERVICE_ACCOUNT_JSON to a service account JSON file.")
        return None

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    _creds = Credentials.from_service_account_file(path, scopes=scopes)
    _drive_service = build("drive", "v3", credentials=_creds, cache_discovery=False)
    return _drive_service


def _rcca_embed_model(chat_model: str) -> str:
    """Embeddings often need a dedicated model (e.g. nomic-embed-text); chat LLMs may not support /api/embeddings."""
    v = os.getenv("OLLAMA_EMBED_MODEL", "").strip()
    return v if v else chat_model


def _rcca_thresholds() -> tuple[float, float]:
    """(semantic_blend_min, keyword_only_min) — see _rcca_candidate_score."""
    sem = float(os.getenv("GDRIVE_SIMILARITY_THRESHOLD", "0.38"))
    kw = float(os.getenv("RCCA_KEYWORD_ONLY_THRESHOLD", "0.15"))
    return sem, kw


def _rcca_candidate_score(
    query_text: str,
    doc_text: str,
    query_emb: Optional[list[float]],
    doc_emb: Optional[list[float]],
    kw_weight: float,
) -> tuple[float, str]:
    """
    If both embeddings exist, blended semantic + keyword score (0..1).
    Otherwise keyword overlap only — chat models like llama3 often return no embeddings, so the old
    formula (1-w)*0 + w*kw capped at w made it impossible to pass a 0.38 threshold.
    Returns (score, "semantic" | "keyword").
    """
    kw = min(_keyword_score(query_text, doc_text), 1.0)
    if query_emb and doc_emb:
        sem = _cosine(query_emb, doc_emb)
        return (1.0 - kw_weight) * sem + kw_weight * kw, "semantic"
    return kw, "keyword"


def _ollama_embed(text: str, model: str, host: str) -> Optional[list[float]]:
    text = (text or "")[:8000]
    if not text.strip():
        return None
    base = host.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/api/embeddings",
            json={"model": model, "input": text},
            timeout=60.0,
        )
        data = r.json() if r.content else {}
        if r.status_code != 200:
            print(f"[GDRIVE] embeddings HTTP {r.status_code}: {str(data)[:200]}")
            return None
        emb = data.get("embedding")
        if isinstance(emb, list):
            return emb
        embs = data.get("embeddings")
        if isinstance(embs, list) and embs and isinstance(embs[0], list):
            return embs[0]
    except Exception as e:
        print(f"[GDRIVE] embed error: {e}")
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_score(query: str, doc_text: str) -> float:
    q = set(re.findall(r"[a-zA-Z0-9]{3,}", query.lower()))
    d = doc_text.lower()
    if not q:
        return 0.0
    hits = sum(1 for w in q if w in d)
    return hits / max(len(q), 1)


def _list_folder_files(service, folder_id: str, max_files: int = 40) -> list[dict]:
    out = []
    page_token = None
    q = f"'{folder_id}' in parents and trashed = false"
    while len(out) < max_files:
        resp = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=min(50, max_files - len(out)),
            )
            .execute()
        )
        for f in resp.get("files", []):
            out.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _fetch_file_text(service, file_id: str, mime: str, name: str) -> str:
    from googleapiclient.http import MediaIoBaseDownload

    if mime == "application/vnd.google-apps.document":
        req = service.files().export_media(fileId=file_id, mimeType="text/plain")
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue().decode("utf-8", errors="replace")
    if mime == "text/plain":
        req = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue().decode("utf-8", errors="replace")
    # PDF / others: skip text extraction in MVP (keeps deps light)
    print(f"[GDRIVE] Skip unsupported mime for RCCA: {mime} ({name})")
    return ""


def _rcca_mrkdwn_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_rcca_prose(text: str, max_len: int = 2800) -> str:
    """Collapse broken PDF line breaks; trim for Slack."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in t.split("\n")]
    out: list[str] = []
    for ln in lines:
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] and not out[-1].endswith((".", "!", "?", ":")):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    s = "\n".join(x for x in out if x is not None).strip()
    s = re.sub(r"\n{3,}", "\n\n", s)
    if len(s) > max_len:
        s = s[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return s


def _slice_after_label(text: str, label: str, stop_at: list[str]) -> str:
    """Extract content after `label` until the earliest of stop_at labels (case-insensitive)."""
    t = text or ""
    li = t.lower().find(label.lower())
    if li < 0:
        return ""
    rest = t[li + len(label) :].lstrip()
    if rest.startswith(":"):
        rest = rest[1:].lstrip()
    cut = len(rest)
    for stop in stop_at:
        si = rest.lower().find(stop.lower())
        if si >= 0 and si < cut:
            cut = si
    return rest[:cut].strip()


def _parse_rcca_structured_response(raw: str) -> dict[str, str]:
    """Parse SUMMARY / ROOT CAUSE / RESOLUTION blocks from model output."""
    r = (raw or "").strip()
    r = re.sub(r"\*{0,2}(SUMMARY|ROOT CAUSE|RESOLUTION)\*{0,2}\s*:", r"\1:", r, flags=re.I)
    summary = _slice_after_label(r, "SUMMARY", ["ROOT CAUSE", "RESOLUTION"])
    root = _slice_after_label(r, "ROOT CAUSE", ["RESOLUTION"])
    resolution = _slice_after_label(r, "RESOLUTION", [])
    return {
        "incident_summary": _normalize_rcca_prose(summary, 2400),
        "root_cause": _normalize_rcca_prose(root, 1600),
        "resolution": _normalize_rcca_prose(resolution, 2400),
    }


def _try_parse_rcca_json(raw: str) -> Optional[dict[str, str]]:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return {
            "incident_summary": _normalize_rcca_prose(str(obj.get("incident_summary", "")), 2400),
            "root_cause": _normalize_rcca_prose(str(obj.get("root_cause", "")), 1600),
            "resolution": _normalize_rcca_prose(str(obj.get("resolution", "")), 2400),
        }
    except json.JSONDecodeError:
        return None


def _extract_rcca_fields(doc_text: str, ollama_generate_fn) -> dict[str, str]:
    """
    Executive RCCA brief for Slack: line-bounded format first (reliable with small LLMs), then JSON fallback.
    """
    excerpt = doc_text[:12000]
    prompt = (
        "You are a senior SRE. Read the RCCA / post-mortem excerpt below.\n"
        "Produce a concise *executive* brief for Slack. Rules:\n"
        "- Do NOT dump raw form fields (avoid lists like Title:, Date:, Owner:, Severity: copied from the template).\n"
        "- Synthesize into clear prose.\n"
        "- SUMMARY = what failed, who was affected, and approximate duration.\n"
        "- ROOT CAUSE = the underlying technical or process cause (or state if the document is unclear).\n"
        "- RESOLUTION = concrete fixes, verification, and one prevention takeaway.\n\n"
        "Output EXACTLY in this shape (three headers, each on its own line, followed by blank line then paragraphs):\n\n"
        "SUMMARY:\n"
        "<2-4 short sentences>\n\n"
        "ROOT CAUSE:\n"
        "<1-3 sentences>\n\n"
        "RESOLUTION:\n"
        "<2-4 sentences>\n\n"
        f"--- Document ---\n{excerpt}"
    )
    raw = ollama_generate_fn(prompt, timeout=120.0, max_tokens=900)
    if not raw or not raw.strip():
        return {
            "incident_summary": _normalize_rcca_prose(excerpt[:600], 800),
            "root_cause": "Not extracted — see source RCCA file.",
            "resolution": "Not extracted — see source RCCA file.",
        }

    parsed = _parse_rcca_structured_response(raw)
    if parsed["incident_summary"] and (parsed["root_cause"] or parsed["resolution"]):
        return parsed

    j = _try_parse_rcca_json(raw)
    if j and j.get("incident_summary"):
        return j

    salvage_s = parsed["incident_summary"] or ""
    if not salvage_s.strip():
        salvage_s = excerpt[:1200]
    return {
        "incident_summary": _normalize_rcca_prose(salvage_s, 2000),
        "root_cause": _normalize_rcca_prose(parsed["root_cause"], 1600) or "See source RCCA file.",
        "resolution": _normalize_rcca_prose(parsed["resolution"], 1600) or "See source RCCA file.",
    }


def build_rcca_summary_blocks(match: dict) -> list[dict]:
    """Block Kit layout for a professional RCCA summary post."""
    summ = _rcca_mrkdwn_escape(_normalize_rcca_prose(match.get("incident_summary", ""), 2600))
    root = _rcca_mrkdwn_escape(_normalize_rcca_prose(match.get("root_cause", ""), 2000))
    fix = _rcca_mrkdwn_escape(_normalize_rcca_prose(match.get("resolution", ""), 2600))
    src = _rcca_mrkdwn_escape(str(match.get("source_file_name", "RCCA")).replace("`", "'"))
    score = match.get("similarity_score")
    meta = f"Source: `{src}`" + (f" · match score _{score}_" if score is not None else "")

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*📚 Related past incident*\n_Executive summary from your RCCA library — synthesized for this war room._",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📋 What happened*\n{summ or '_No summary._'}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🔎 Root cause*\n{root or '_Not stated in document._'}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*✅ Fix, verification & prevention*\n{fix or '_Not stated in document._'}"},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": meta}]},
    ]
    return blocks


def search_similar_rcca(
    *,
    title: str,
    message_text: str,
    thread_context: str,
    impact_summary: Any,
    ollama_model: str,
    ollama_host: str,
    ollama_generate_fn,
    prefetch_llm_fields: bool = False,
) -> Optional[dict]:
    """
    Finds the best-matching RCCA document.

    If prefetch_llm_fields is False (default), returns only metadata plus rcca_raw_doc_text — no LLM
    extraction yet (call _extract_rcca_fields after the user confirms).

    If True, also runs _extract_rcca_fields and returns incident_summary, root_cause, resolution.

    Priority: **RCCA_LOCAL_FOLDER** if set and exists; else **Google Drive API** when enabled.
    """
    local_raw = os.getenv("RCCA_LOCAL_FOLDER", "").strip().strip('"').strip("'")
    if local_raw:
        expanded = os.path.expanduser(local_raw)
        if os.path.isdir(expanded):
            from integrations.local_rcca import search_similar_rcca_local

            return search_similar_rcca_local(
                title=title,
                message_text=message_text,
                thread_context=thread_context,
                impact_summary=impact_summary,
                ollama_model=ollama_model,
                ollama_host=ollama_host,
                ollama_generate_fn=ollama_generate_fn,
                root=expanded,
                prefetch_llm_fields=prefetch_llm_fields,
            )
        print(f"[RCCA] RCCA_LOCAL_FOLDER is not a directory: {expanded}")
        return None

    if not _gdrive_enabled():
        return None

    folder_id = os.getenv("GDRIVE_RCCA_FOLDER_ID", "").strip()
    if not folder_id:
        print("[GDRIVE] GDRIVE_RCCA_FOLDER_ID not set.")
        return None

    service = _get_drive_service()
    if not service:
        return None

    threshold_sem, threshold_kw = _rcca_thresholds()
    kw_weight = float(os.getenv("GDRIVE_KEYWORD_WEIGHT", "0.25"))
    embed_model = _rcca_embed_model(ollama_model)

    try:
        files = _list_folder_files(service, folder_id)
    except Exception as e:
        print(f"[GDRIVE] list files failed: {e}")
        return None

    impact_str = ""
    if isinstance(impact_summary, dict):
        impact_str = json.dumps(impact_summary)
    elif isinstance(impact_summary, str):
        impact_str = impact_summary

    query_text = f"{title}\n{message_text}\n{thread_context[:1500]}\n{impact_str}"[:5000]
    query_emb = _ollama_embed(query_text, embed_model, ollama_host)
    if not query_emb:
        print(
            "[RCCA] Query embedding failed — using keyword-only matching. "
            "For semantic similarity, run `ollama pull nomic-embed-text` and set OLLAMA_EMBED_MODEL=nomic-embed-text "
            f"(embed model tried: {embed_model!r})."
        )

    best: tuple[float, str, str, str, str] = (-1.0, "", "", "", "keyword")

    for f in files:
        fid = f["id"]
        name = f.get("name", "unknown")
        mime = f.get("mimeType", "")
        try:
            text = _fetch_file_text(service, fid, mime, name)
        except Exception as ex:
            print(f"[GDRIVE] export {name}: {ex}")
            continue
        if not text.strip():
            continue
        text = text[:20000]
        doc_emb = _ollama_embed(text[:4000], embed_model, ollama_host)
        score, mode = _rcca_candidate_score(query_text, text, query_emb, doc_emb, kw_weight)
        if score > best[0]:
            best = (score, text, name, fid, mode)

    thresh = threshold_sem if best[4] == "semantic" else threshold_kw
    if best[0] < thresh:
        print(f"[GDRIVE] No RCCA above threshold ({best[0]:.3f} < {thresh}, mode={best[4]}).")
        return None

    meta = {
        "source_file_name": best[2],
        "source_file_id": best[3],
        "similarity_score": round(best[0], 3),
    }
    if prefetch_llm_fields:
        fields = _extract_rcca_fields(best[1], ollama_generate_fn)
        return {**fields, **meta}
    return {"rcca_raw_doc_text": best[1], **meta}


def format_rcca_slack_message(match: dict) -> str:
    return (
        "🔍 *Past incident — summary & possible fixes*\n\n"
        f"🧾 *Summary:*\n{match.get('incident_summary', 'N/A')}\n\n"
        f"⚠️ *Root cause:*\n{match.get('root_cause', 'N/A')}\n\n"
        f"✅ *Possible solutions / remediation:*\n{match.get('resolution', 'N/A')}\n\n"
        f"_Source: {match.get('source_file_name', 'RCCA')} (match score {match.get('similarity_score', '?')})_"
    )
