"""
Audit Log page — full, immutable history of every alert and action.

Columns: Timestamp | Alert ID | Source | Action | Actor | Details
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import database as db


def render() -> None:
    st.title("Audit Log")
    st.caption(
        "Append-only record of every alert, review decision, dispatch, and error. "
        "Cannot be edited or deleted."
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.expander("Filters", expanded=False):
        f1, f2, f3 = st.columns(3)
        with f1:
            alert_id_filter = st.text_input(
                "Alert ID (partial match)", placeholder="paste alert ID…"
            )
        with f2:
            action_filter = st.selectbox(
                "Action",
                ["All", "alert_drafted", "alert_approved", "alert_rejected",
                 "alert_redraft", "dispatch_success", "dispatch_failed",
                 "email_sent", "graph_error", "workflow_complete",
                 "redraft_requested"],
            )
        with f3:
            days_back = st.slider("Show last N days", 1, 90, 30)

    logs = db.list_audit_log(
        alert_id=alert_id_filter or None,
        limit=500,
    )

    # Date filter
    cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
    logs = [l for l in logs if l.get("timestamp", "") >= cutoff]

    # Action filter
    if action_filter != "All":
        logs = [l for l in logs if l.get("action") == action_filter]

    # ── Metrics ───────────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Log Entries", len(logs))
    m2.metric("Approved", sum(1 for l in logs if l["action"] == "alert_approved"))
    m3.metric("Dispatched", sum(1 for l in logs if l["action"] == "dispatch_success"))
    m4.metric("Errors", sum(1 for l in logs if "error" in l["action"] or "failed" in l["action"]))

    st.markdown("---")

    if not logs:
        st.info("No audit entries match the current filters.")
        return

    # ── Log table ─────────────────────────────────────────────────────────────
    for entry in logs:
        action = entry.get("action", "")
        icon = _action_icon(action)
        ts = entry.get("timestamp", "")[:19].replace("T", " ")
        actor = entry.get("actor", "system")
        alert_id = entry.get("alert_id", "—")

        details_raw = entry.get("details")
        details: dict = {}
        if isinstance(details_raw, str):
            try:
                details = json.loads(details_raw)
            except Exception:
                details = {"raw": details_raw}
        elif isinstance(details_raw, dict):
            details = details_raw

        with st.container():
            c1, c2, c3, c4 = st.columns([2, 1.5, 1.5, 4])
            c1.caption(f"`{ts} UTC`")
            c2.markdown(f"{icon} **{action}**")
            c3.caption(f"by **{actor}**")

            # Summarise key detail fields
            summary_parts = []
            if details.get("source"):
                summary_parts.append(f"Source: {details['source']}")
            if details.get("headline"):
                summary_parts.append(details["headline"][:70])
            if details.get("subject"):
                summary_parts.append(f"Subject: {details['subject'][:60]}")
            if details.get("recipients"):
                r = details["recipients"]
                if isinstance(r, list):
                    summary_parts.append(f"To: {', '.join(r)}")
            if details.get("errors"):
                errs = details["errors"]
                summary_parts.append(f"Errors: {errs}")
            if details.get("error"):
                summary_parts.append(f"Error: {details['error'][:80]}")
            if details.get("reviewer_notes"):
                summary_parts.append(f'Notes: "{details["reviewer_notes"][:60]}"')

            c4.caption(" · ".join(summary_parts) if summary_parts else "—")

            # Expandable full details
            with st.expander(f"Full details — alert `{alert_id[:8]}…`", expanded=False):
                st.json(details)

        st.divider()


def _action_icon(action: str) -> str:
    icons = {
        "alert_drafted": "📝",
        "alert_approved": "✅",
        "alert_rejected": "❌",
        "alert_redraft": "🔄",
        "redraft_requested": "🔄",
        "dispatch_success": "📧",
        "dispatch_failed": "⚠️",
        "email_sent": "📤",
        "graph_error": "🚨",
        "workflow_complete": "🏁",
    }
    return icons.get(action, "📋")
