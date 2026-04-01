"""
New Relic NerdGraph NRQL queries for live incident context (logs, errors, infra).
Disabled unless NEW_RELIC_ENABLED=true and API key + account ID set.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

NERDGRAPH_US = "https://api.newrelic.com/graphql"
NERDGRAPH_EU = "https://api.eu.newrelic.com/graphql"


def _nr_enabled() -> bool:
    return os.getenv("NEW_RELIC_ENABLED", "").lower() in ("1", "true", "yes")


def _graphql_url() -> str:
    if os.getenv("NEW_RELIC_REGION", "us").lower() == "eu":
        return NERDGRAPH_EU
    return NERDGRAPH_US


def run_nrql(api_key: str, account_id: int, nrql: str, timeout: float = 45.0) -> list[Any]:
    """Execute NRQL via NerdGraph; returns results list or []."""
    query = """
    query ($accountId: Int!, $nrql: Nrql!) {
      actor {
        account(id: $accountId) {
          nrql(query: $nrql) {
            results
          }
        }
      }
    }
    """
    payload = {
        "query": query,
        "variables": {"accountId": int(account_id), "nrql": nrql},
    }
    try:
        r = httpx.post(
            _graphql_url(),
            headers={"Api-Key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        data = r.json() if r.content else {}
        if r.status_code != 200:
            print(f"[NR] HTTP {r.status_code}: {str(data)[:400]}")
            return []
        errs = data.get("errors")
        if errs:
            print(f"[NR] GraphQL errors: {errs[:2]}")
            # Fallback: try query as string variable (some API versions)
            return _run_nrql_string_fallback(api_key, account_id, nrql, timeout)
        res = (
            data.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results")
        )
        return res if isinstance(res, list) else []
    except Exception as e:
        print(f"[NR] request failed: {e}")
        return []


def _run_nrql_string_fallback(api_key: str, account_id: int, nrql: str, timeout: float) -> list[Any]:
    """Some accounts/schemas accept NRQL as escaped string in document."""
    nrql_esc = json.dumps(nrql)
    doc = f"""
    {{
      actor {{
        account(id: {int(account_id)}) {{
          nrql(query: {nrql_esc}) {{
            results
          }}
        }}
      }}
    }}
    """
    try:
        r = httpx.post(
            _graphql_url(),
            headers={"Api-Key": api_key, "Content-Type": "application/json"},
            json={"query": doc},
            timeout=timeout,
        )
        data = r.json() if r.content else {}
        res = (
            data.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results")
        )
        return res if isinstance(res, list) else []
    except Exception as e:
        print(f"[NR] fallback failed: {e}")
        return []


def _fmt_results(rows: list[Any], max_rows: int = 8) -> str:
    if not rows:
        return "_No rows returned._"
    lines = []
    for row in rows[:max_rows]:
        if isinstance(row, dict):
            lines.append("• " + ", ".join(f"{k}={v}" for k, v in list(row.items())[:6]))
        else:
            lines.append(f"• {row}")
    if len(rows) > max_rows:
        lines.append(f"_…and {len(rows) - max_rows} more_")
    return "\n".join(lines)


def build_incident_nrql_bundle(service_name: str, minutes: int = 30) -> list[tuple[str, str]]:
    """
    Label + NRQL query pairs. Service name is escaped for NRQL string literal.
    """
    svc = (service_name or "production").replace("'", "\\'")[:120]
    m = max(5, min(minutes, 120))
    queries: list[tuple[str, str]] = [
        (
            "Log errors (count by level)",
            f"SELECT count(*) FROM Log WHERE level IN ('error','ERROR','fatal') FACET level SINCE {m} minutes ago LIMIT 10",
        ),
        (
            "Application errors",
            f"SELECT count(*) FROM TransactionError WHERE appName LIKE '%{svc}%' OR host LIKE '%{svc}%' SINCE {m} minutes ago LIMIT 5",
        ),
        (
            "CPU (SystemSample)",
            f"SELECT average(cpuPercent) FROM SystemSample WHERE hostname LIKE '%{svc}%' FACET hostname LIMIT 5 SINCE {m} minutes ago",
        ),
        (
            "Memory (SystemSample)",
            f"SELECT average(memoryUsedPercent) FROM SystemSample WHERE hostname LIKE '%{svc}%' FACET hostname LIMIT 5 SINCE {m} minutes ago",
        ),
    ]
    return queries


def fetch_live_incident_snapshot(service_name: str) -> str:
    """Run NRQL bundle and return markdown section for Slack."""
    if not _nr_enabled():
        return ""

    key = os.getenv("NEW_RELIC_API_KEY", "").strip()
    acc = os.getenv("NEW_RELIC_ACCOUNT_ID", "").strip()
    if not key or not acc:
        print("[NR] NEW_RELIC_API_KEY or NEW_RELIC_ACCOUNT_ID missing.")
        return ""

    try:
        account_id = int(acc)
    except ValueError:
        print("[NR] NEW_RELIC_ACCOUNT_ID must be an integer.")
        return ""

    minutes = int(os.getenv("NR_QUERY_WINDOW_MINUTES", "30"))
    parts: list[str] = ["📊 *Live Incident Data (New Relic)*\n"]

    errors_chunks: list[str] = []
    infra_chunks: list[str] = []
    log_chunks: list[str] = []

    for label, nrql in build_incident_nrql_bundle(service_name, minutes):
        rows = run_nrql(key, account_id, nrql)
        block = f"*{label}*\n{_fmt_results(rows)}"
        low = label.lower()
        if "log" in low:
            log_chunks.append(block)
        elif "error" in low or "application" in low:
            errors_chunks.append(block)
        else:
            infra_chunks.append(block)

    parts.append("🚨 *Errors:*\n" + ("\n\n".join(errors_chunks) if errors_chunks else "_No error query results._"))
    parts.append("\n🖥 *CPU / Memory:*\n" + ("\n\n".join(infra_chunks) if infra_chunks else "_No infra samples (check host/app names)._"))
    parts.append("\n📜 *Logs Summary:*\n" + ("\n\n".join(log_chunks) if log_chunks else "_No log aggregates._"))

    return "\n".join(parts)


def fetch_live_incident_snapshot_safe(service_name: str) -> str:
    try:
        return fetch_live_incident_snapshot(service_name)
    except Exception as e:
        return f"📊 *New Relic*\n_Error fetching data: {e}_"
