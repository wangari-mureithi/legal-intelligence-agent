"""
Alert Review page — human-in-the-loop approval workflow.

Flow:
  1. Lists all pending alerts.
  2. Reviewer selects one and sees the full editable draft.
  3. Reviewer can edit any field, add notes, then:
       - Approve  → resumes the LangGraph thread → email dispatch triggered
       - Reject   → resumes the graph with rejection → workflow ends
       - Re-draft → resumes with redraft signal → (optional: re-run summarizer)
  4. Shows dispatch confirmation or error after approval.
"""

import json
import sys
from pathlib import Path

import streamlit as st
from langgraph.types import Command

# Ensure project root on path
_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import database as db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_graph():
    """Import graph lazily to avoid slow startup on every page load."""
    from backend.graph import graph
    return graph


def _resume_graph(thread_id: str, decision: dict) -> bool:
    """
    Resume the paused LangGraph thread with the reviewer's decision.
    Returns True on success, False on error.
    """
    try:
        graph = _get_graph()
        config = {"configurable": {"thread_id": thread_id}}
        graph.invoke(Command(resume=decision), config=config)
        return True
    except Exception as exc:
        st.error(f"Failed to resume graph: {exc}")
        return False


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    st.title("Alert Review")
    st.caption("Review and approve AI-drafted legal alerts before distribution.")

    # ── Alert selection ───────────────────────────────────────────────────────
    pending_alerts = db.list_alerts(status="pending")

    if not pending_alerts:
        st.success("No alerts pending review.")

        # Show recently reviewed
        st.markdown("### Recently Reviewed")
        recent = db.list_alerts(limit=10)
        non_pending = [a for a in recent if a["approval_status"] != "pending"]
        if non_pending:
            for a in non_pending[:5]:
                draft = a.get("alert_draft") or {}
                status_icon = {"approved": "🟢", "rejected": "🔴"}.get(
                    a["approval_status"], "⚪"
                )
                hl = draft.get("headline", "").strip()
                hl_display = hl[:100] + "…" if len(hl) > 100 else hl
                st.caption(
                    f"{status_icon} {a['source_name']} — "
                    f"{hl_display} "
                    f"({a['created_at'][:10]})"
                )
        return

    # Pre-select from dashboard navigation
    preselected = st.session_state.get("selected_alert_id")
    def _alert_label(a: dict) -> str:
        hl = (a.get("alert_draft") or {}).get("headline", "No headline").strip()
        hl_short = hl[:90] + "…" if len(hl) > 90 else hl
        return f"{a['source_name']} — {hl_short} ({a['created_at'][:10]})"

    alert_options = {_alert_label(a): a["id"] for a in pending_alerts}
    option_labels = list(alert_options.keys())

    # Find index of preselected alert
    default_idx = 0
    if preselected:
        for i, aid in enumerate(alert_options.values()):
            if aid == preselected:
                default_idx = i
                break

    selected_label = st.selectbox(
        f"Select a pending alert ({len(pending_alerts)} awaiting review)",
        options=option_labels,
        index=default_idx,
    )
    selected_id = alert_options[selected_label]
    alert = db.get_alert(selected_id)

    if not alert:
        st.error("Alert not found.")
        return

    # Clear pre-selection after rendering
    if preselected == selected_id:
        st.session_state.pop("selected_alert_id", None)

    # ── Auto-scroll anchor ────────────────────────────────────────────────────
    st.markdown('<div id="alert-content"></div>', unsafe_allow_html=True)
    import streamlit.components.v1 as components
    components.html(
        """<script>
            (function() {
                try {
                    var el = window.parent.document.getElementById('alert-content');
                    if (el) { el.scrollIntoView({behavior: 'smooth', block: 'start'}); }
                } catch(e) {}
            })();
        </script>""",
        height=0,
    )

    draft = alert.get("alert_draft") or {}
    thread_id = alert.get("thread_id", alert["id"])

    st.markdown("---")

    # ── Alert metadata ────────────────────────────────────────────────────────
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Source", alert.get("source_name", "—"))
    mc2.metric("Update Type", draft.get("update_type", "—"))
    mc3.metric("Category", alert.get("source_category", "—"))

    mc4, mc5, mc6 = st.columns(3)
    pub_date = alert.get("publication_date") or draft.get("date", "—")
    date_conf = draft.get("date_confirmed", True)
    date_display = pub_date + (" ✓" if date_conf else " ⚠️ unconfirmed")
    mc4.metric("Publication Date", date_display)
    mc5.metric("Geography", alert.get("geography_scope", "kenya").title())
    regional = draft.get("regional_relevance", False)
    mc6.metric("Regional Alert", "Yes" if regional else "No")

    st.markdown("---")

    # ── Editable draft form ───────────────────────────────────────────────────
    st.subheader("Edit Alert Draft")
    st.caption(
        "All fields are editable. Changes made here will be used for dispatch."
    )

    with st.form(key=f"review_form_{selected_id}"):
        col_l, col_r = st.columns([2, 1])

        with col_l:
            practice_area = st.text_input(
                "Practice Area", value=draft.get("practice_area", "")
            )
            update_type = st.selectbox(
                "Update Type",
                options=[
                    "New Bill", "Regulation", "Statutory Instrument",
                    "Court Ruling", "Policy Circular",
                ],
                index=_update_type_index(draft.get("update_type", "New Bill")),
            )
            headline = st.text_area(
                "Headline (one sentence, active voice)",
                value=draft.get("headline", ""),
                height=80,
            )
            summary = st.text_area(
                "Summary (3–5 sentences)",
                value=draft.get("summary", ""),
                height=140,
            )

        with col_r:
            alert_date = st.text_input(
                "Date (YYYY-MM-DD)", value=draft.get("date", "")
            )
            source_ref = st.text_input(
                "Source Reference", value=draft.get("source", "")
            )
            recommended_action = st.text_area(
                "Recommended Action",
                value=draft.get("recommended_action", ""),
                height=100,
            )

        # Key provisions
        st.markdown("**Key Provisions** (one per line)")
        provisions_text = st.text_area(
            "Key Provisions",
            value="\n".join(draft.get("key_provisions", [])),
            height=120,
            label_visibility="collapsed",
        )

        # Stakeholder implications
        st.markdown("**Stakeholder Implications** (format: `Type: Implication`)")
        existing_impl = "\n".join(
            f"{i.get('type','')}: {i.get('implication','')}"
            for i in draft.get("stakeholder_implications", [])
        )
        implications_text = st.text_area(
            "Stakeholder Implications",
            value=existing_impl,
            height=120,
            label_visibility="collapsed",
        )

        # Practice areas tagged
        st.markdown("**Practice Areas Tagged** (comma-separated)")
        tags_text = st.text_input(
            "Practice Areas Tagged",
            value=", ".join(draft.get("practice_areas_tagged", [])),
            label_visibility="collapsed",
        )

        # Reviewer notes
        st.markdown("**Reviewer Notes**")
        reviewer_notes = st.text_area(
            "Reviewer Notes",
            placeholder="Optional notes (will be logged but not sent to clients).",
            height=80,
            label_visibility="collapsed",
        )

        reviewer_name = st.text_input(
            "Your name / email (for audit log)",
            placeholder="partner@firm.com",
        )

        # ── Action buttons ────────────────────────────────────────────────────
        st.markdown("---")
        btn_col1, btn_col2, btn_col3, _ = st.columns([1, 1, 1, 3])
        approve_btn = btn_col1.form_submit_button(
            "Approve & Dispatch", type="primary", use_container_width=True
        )
        reject_btn  = btn_col2.form_submit_button(
            "Reject", use_container_width=True
        )
        redraft_btn = btn_col3.form_submit_button(
            "Re-draft", use_container_width=True
        )

    # ── Process form submission ───────────────────────────────────────────────
    if approve_btn or reject_btn or redraft_btn:
        updated_draft = _build_updated_draft(
            draft,
            practice_area=practice_area,
            update_type=update_type,
            headline=headline,
            summary=summary,
            alert_date=alert_date,
            source_ref=source_ref,
            recommended_action=recommended_action,
            provisions_text=provisions_text,
            implications_text=implications_text,
            tags_text=tags_text,
        )

        if approve_btn:
            decision = {
                "approval_status": "approved",
                "reviewer_notes": reviewer_notes,
                "updated_draft": updated_draft,
                "reviewed_by": reviewer_name or "partner",
            }
            with st.spinner("Resuming workflow and dispatching email…"):
                success = _resume_graph(thread_id, decision)
            if success:
                st.success("Alert approved. Email dispatch initiated.")
                db.update_alert(
                    selected_id,
                    approval_status="approved",
                    reviewer_notes=reviewer_notes,
                    alert_draft=updated_draft,
                )
                st.balloons()
            else:
                st.error("Graph resume failed — check logs.")

        elif reject_btn:
            decision = {
                "approval_status": "rejected",
                "reviewer_notes": reviewer_notes,
                "updated_draft": updated_draft,
                "reviewed_by": reviewer_name or "partner",
            }
            with st.spinner("Submitting rejection…"):
                success = _resume_graph(thread_id, decision)
            if success:
                st.warning("Alert rejected and workflow ended.")
                db.update_alert(
                    selected_id,
                    approval_status="rejected",
                    reviewer_notes=reviewer_notes,
                )

        elif redraft_btn:
            decision = {
                "approval_status": "redraft",
                "reviewer_notes": reviewer_notes,
                "updated_draft": updated_draft,
                "reviewed_by": reviewer_name or "partner",
            }
            # For redraft, update DB and note — reviewer can edit manually above
            db.update_alert(
                selected_id,
                reviewer_notes=f"[Re-draft requested] {reviewer_notes}",
                alert_draft=updated_draft,
            )
            db.log_action(
                selected_id, "redraft_requested",
                actor=reviewer_name or "partner",
                details={"notes": reviewer_notes},
            )
            st.info(
                "Re-draft noted. Edit the fields above and click Approve & Dispatch "
                "when the revised alert is ready."
            )

    # ── Dispatch status ───────────────────────────────────────────────────────
    # Refresh from DB to show latest dispatch state
    refreshed = db.get_alert(selected_id)
    if refreshed:
        if refreshed.get("dispatched_email"):
            st.success("Email dispatched successfully.")
        if refreshed.get("dispatch_error"):
            err = refreshed["dispatch_error"]
            st.error(f"Dispatch error: {err}")
            if st.button("Retry Dispatch"):
                _retry_dispatch(selected_id, thread_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _update_type_index(value: str) -> int:
    options = ["New Bill", "Regulation", "Statutory Instrument",
               "Court Ruling", "Policy Circular"]
    try:
        return options.index(value)
    except ValueError:
        return 0


def _parse_implications(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            parts = line.split(":", 1)
            items.append({"type": parts[0].strip(), "implication": parts[1].strip()})
        else:
            items.append({"type": "General", "implication": line})
    return items or [{"type": "All Clients", "implication": "See summary above."}]


def _build_updated_draft(original: dict, **fields) -> dict:
    updated = dict(original)
    updated["practice_area"]   = fields["practice_area"]
    updated["update_type"]     = fields["update_type"]
    updated["headline"]        = fields["headline"]
    updated["summary"]         = fields["summary"]
    updated["date"]            = fields["alert_date"]
    updated["source"]          = fields["source_ref"]
    updated["recommended_action"] = fields["recommended_action"]
    updated["key_provisions"]  = [
        p.strip() for p in fields["provisions_text"].splitlines() if p.strip()
    ]
    updated["stakeholder_implications"] = _parse_implications(fields["implications_text"])
    updated["practice_areas_tagged"] = [
        t.strip() for t in fields["tags_text"].split(",") if t.strip()
    ]
    return updated


def _retry_dispatch(alert_id: str, thread_id: str) -> None:
    """Directly re-invoke only the email dispatcher for a failed alert."""
    try:
        from backend.nodes.email_dispatcher import email_dispatcher_node
        alert = db.get_alert(alert_id)
        if not alert:
            st.error("Alert not found.")
            return
        state = {**alert, "thread_id": thread_id}
        result = email_dispatcher_node(state)
        if result.get("dispatched_email"):
            db.update_alert(alert_id, dispatched_email=1, dispatch_error="")
            st.success("Retry successful — email dispatched.")
        else:
            st.error(f"Retry failed: {result.get('dispatch_error','')}")
    except Exception as exc:
        st.error(f"Retry error: {exc}")
