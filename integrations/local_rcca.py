"""
RCCA similarity search over a local folder (e.g. macOS Google Drive sync path).
No Google Cloud credentials required — reads .txt / .md files from disk.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from integrations.gdrive_rcca import (
    _extract_rcca_fields,
    _ollama_embed,
    _rcca_candidate_score,
    _rcca_embed_model,
    _rcca_thresholds,
)


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".rst", ".csv"}
PDF_EXTENSIONS = {".pdf"}
MAX_FILE_BYTES = 2_000_000
MAX_DEPTH = 4
MAX_FILES = 50


def _read_pdf_text(full_path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("[RCCA local] Install pypdf for PDF support: pip install pypdf")
        return ""
    try:
        reader = PdfReader(full_path)
        parts: list[str] = []
        for page in reader.pages[:40]:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return "\n".join(parts)
    except Exception as e:
        print(f"[RCCA local] PDF read failed {full_path}: {e}")
        return ""


def get_local_rcca_root() -> str:
    raw = os.getenv("RCCA_LOCAL_FOLDER", "").strip().strip('"').strip("'")
    return os.path.expanduser(raw) if raw else ""


def local_rcca_configured() -> bool:
    root = get_local_rcca_root()
    return bool(root) and os.path.isdir(root)


def scan_local_rcca_files(root: str) -> list[tuple[str, str]]:
    """Return list of (relative_path, text_content)."""
    found: list[tuple[str, str]] = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return found

    for dirpath, _dirnames, filenames in os.walk(root):
        depth = dirpath[len(root) :].count(os.sep)
        if depth > MAX_DEPTH:
            continue
        for name in filenames:
            if len(found) >= MAX_FILES:
                return found
            low = name.lower()
            ext = os.path.splitext(low)[1]
            if ext not in TEXT_EXTENSIONS and ext not in PDF_EXTENSIONS:
                if ext == ".gdoc":
                    continue  # Google Doc shortcut only; use .pdf or export .txt in this folder
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                if st.st_size > MAX_FILE_BYTES:
                    print(f"[RCCA local] Skip large file: {name}")
                    continue
                if ext in PDF_EXTENSIONS:
                    text = _read_pdf_text(full)
                else:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
            except OSError as e:
                print(f"[RCCA local] Cannot read {full}: {e}")
                continue
            rel = os.path.relpath(full, root)
            found.append((rel, text))
    return found


def search_similar_rcca_local(
    *,
    title: str,
    message_text: str,
    thread_context: str,
    impact_summary: Any,
    ollama_model: str,
    ollama_host: str,
    ollama_generate_fn,
    root: Optional[str] = None,
    prefetch_llm_fields: bool = False,
) -> Optional[dict]:
    root = root or get_local_rcca_root()
    if not root or not os.path.isdir(root):
        print(f"[RCCA local] Folder missing or not a directory: {root!r}")
        return None

    files = scan_local_rcca_files(root)
    if not files:
        print(f"[RCCA local] No readable text files (.txt, .md, …) under {root}")
        return None

    threshold_sem, threshold_kw = _rcca_thresholds()
    kw_weight = float(os.getenv("GDRIVE_KEYWORD_WEIGHT", "0.25"))
    embed_model = _rcca_embed_model(ollama_model)

    impact_str = ""
    if isinstance(impact_summary, dict):
        impact_str = json.dumps(impact_summary)
    elif isinstance(impact_summary, str):
        impact_str = impact_summary

    query_text = f"{title}\n{message_text}\n{thread_context[:1500]}\n{impact_str}"[:5000]
    query_emb = _ollama_embed(query_text, embed_model, ollama_host)
    if not query_emb:
        print(
            "[RCCA local] Query embedding failed — using keyword-only matching. "
            "For semantic similarity: `ollama pull nomic-embed-text` and set OLLAMA_EMBED_MODEL=nomic-embed-text "
            f"(tried: {embed_model!r})."
        )

    best: tuple[float, str, str, str] = (-1.0, "", "", "keyword")

    for rel_path, text in files:
        if not text.strip():
            continue
        text = text[:20000]
        doc_emb = _ollama_embed(text[:4000], embed_model, ollama_host)
        score, mode = _rcca_candidate_score(query_text, text, query_emb, doc_emb, kw_weight)
        if score > best[0]:
            best = (score, text, rel_path, mode)

    thresh = threshold_sem if best[3] == "semantic" else threshold_kw
    if best[0] < thresh:
        print(f"[RCCA local] No match above threshold ({best[0]:.3f} < {thresh}, mode={best[3]}).")
        return None

    meta = {
        "source_file_name": best[2],
        "source_file_id": f"local:{best[2]}",
        "similarity_score": round(best[0], 3),
    }
    if prefetch_llm_fields:
        fields = _extract_rcca_fields(best[1], ollama_generate_fn)
        return {**fields, **meta}
    return {"rcca_raw_doc_text": best[1], **meta}


def check_local_rcca_connectivity() -> dict:
    """For /health — no secrets."""
    root = get_local_rcca_root()
    if not root:
        return {"mode": "local", "configured": False, "detail": "RCCA_LOCAL_FOLDER not set"}
    if not os.path.exists(root):
        return {"mode": "local", "configured": True, "path": root, "exists": False, "detail": "path does not exist"}
    if not os.path.isdir(root):
        return {"mode": "local", "configured": True, "path": root, "exists": True, "is_dir": False}
    files = scan_local_rcca_files(root)
    names = [f[0] for f in files[:8]]
    usable = sum(1 for _rel, t in files if t.strip())
    return {
        "mode": "local",
        "configured": True,
        "path": root,
        "exists": True,
        "is_dir": True,
        "scanned_files": len(files),
        "readable_text_files": usable,
        "sample_files": names,
    }
