"""
human_review_node — pauses the LangGraph execution and waits for partner approval.

DB writes are deliberately NOT done here. They are done by the scheduler/backfill
AFTER detecting the graph is paused, which avoids any risk of LangGraph's
interrupt() mechanism interfering with SQLite commits.

This node only:
  1. Calls interrupt() to pause the graph.
  2. On resume, returns the reviewer's decision so the graph can continue.
"""

import logging
from typing import Any

from langgraph.types import interrupt

from backend.state import AlertState
from backend import database as db

logger = logging.getLogger(__name__)


def human_review_node(state: AlertState) -> dict[str, Any]:
    thread_id = state.get("thread_id", "")
    alert_draft = state.get("alert_draft", {})

    logger.info("Alert %s awaiting human review.", thread_id)

    # ── Pause graph — Streamlit resumes with Command(resume={...}) ─────────────
    decision: dict = interrupt(
        {
            "thread_id": thread_id,
            "alert_draft": alert_draft,
            "source_url": state.get("source_url", ""),
            "source_name": state.get("source_name", ""),
            "message": "Alert pending partner review.",
        }
    )

    # ── Process the reviewer's decision ───────────────────────────────────────
    approval_status = decision.get("approval_status", "rejected")
    reviewer_notes = decision.get("reviewer_notes", "")
    updated_draft = decision.get("updated_draft", alert_draft)
    reviewed_by = decision.get("reviewed_by", "partner")

    from datetime import datetime
    reviewed_at = datetime.utcnow().isoformat()

    db.update_alert(
        thread_id,
        approval_status=approval_status,
        reviewer_notes=reviewer_notes,
        alert_draft=updated_draft,
        reviewed_at=reviewed_at,
        reviewed_by=reviewed_by,
    )

    db.log_action(
        alert_id=thread_id,
        action=f"alert_{approval_status}",
        actor=reviewed_by,
        details={
            "reviewer_notes": reviewer_notes,
            "approval_status": approval_status,
        },
    )

    logger.info("Alert %s: %s by %s.", thread_id, approval_status, reviewed_by)

    return {
        "approval_status": approval_status,
        "reviewer_notes": reviewer_notes,
        "alert_draft": updated_draft,
    }
