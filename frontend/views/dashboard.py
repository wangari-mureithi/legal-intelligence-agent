"""
Dashboard page — overview of all alerts with filtering and detail drill-down.
"""

from datetime import datetime, timedelta
import streamlit as st
from backend import database as db


def render() -> None:
    st.title("Dashboard")
    st.caption("All regulatory alerts tracked by the system.")

    # ── Summary metrics ───────────────────────────────────────────────────────
    all_alerts = db.list_alerts(limit=1000)
    pending   = [a for a in all_alerts if a["approval_status"] == "pending"]
    approved  = [a for a in all_alerts if a["approval_status"] == "approved"]
    rejected  = [a for a in all_alerts if a["approval_status"] == "rejected"]
    dispatched = [a for a in all_alerts if a.get("dispatched_email")]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Alerts", len(all_alerts))
    col2.metric("Pending Review", len(pending), delta=f"+{len(pending)}" if pending else None,
                delta_color="inverse" if pending else "off")
    col3.metric("Approved & Sent", len(dispatched))
    col4.metric("Rejected", len(rejected))

    st.markdown("---")

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.expander("Filters", expanded=True):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            status_filter = st.selectbox(
                "Status",
                ["All", "pending", "approved", "rejected"],
                index=0,
            )
        with fc2:
            date_from = st.date_input(
                "From",
                value=datetime.utcnow().date() - timedelta(days=30),
            )
        with fc3:
            date_to = st.date_input("To", value=datetime.utcnow().date())

        practice_filter = st.text_input(
            "Practice Area (keyword)",
            placeholder="e.g. Corporate, Tax, Banking",
        )

    # ── Query ─────────────────────────────────────────────────────────────────
    filtered = db.list_alerts(
        status=status_filter if status_filter != "All" else None,
        date_from=str(date_from),
        date_to=str(date_to),
        practice_area=practice_filter or None,
    )

    if not filtered:
        st.info("No alerts match the current filters.")
        return

    # ── Alert table ───────────────────────────────────────────────────────────
    st.markdown(f"**{len(filtered)} alert(s) found**")

    # Build display rows
    rows = []
    for a in filtered:
        draft = a.get("alert_draft") or {}
        full_headline = (draft.get("headline") or "No headline").strip()
        # Show up to 100 chars in the table; full text is in the detail view
        display_headline = (
            full_headline[:100] + "…" if len(full_headline) > 100 else full_headline
        )
        rows.append({
            "_id": a["id"],
            "Date": a["created_at"][:10] if a.get("created_at") else "—",
            "Source": a.get("source_name", "—"),
            "Update Type": draft.get("update_type", "—"),
            "Headline": display_headline,
            "_full_headline": full_headline,   # kept for the detail panel
            "Practice Areas": draft.get("practice_area", "—"),
            "Status": a.get("approval_status", "pending"),
        })

    # Select which alert to view
    for i, row in enumerate(rows):
        with st.container():
            c1, c2, c3, c4, c5 = st.columns([1.2, 2.5, 5.5, 2, 1.2])
            c1.caption(row["Date"])
            c2.caption(row["Source"])
            c3.markdown(f"**{row['Headline']}**")
            c4.caption(row["Practice Areas"])

            badge_class = {
                "pending": "badge-pending",
                "approved": "badge-approved",
                "rejected": "badge-rejected",
            }.get(row["Status"], "badge-pending")
            c5.markdown(
                f'<span class="{badge_class}">{row["Status"].upper()}</span>',
                unsafe_allow_html=True,
            )

            if st.button("Open", key=f"open_{row['_id']}"):
                st.session_state["selected_alert_id"] = row["_id"]
                st.session_state["nav_page"] = "Alert Review"
                st.rerun()

        st.divider()

    # ── Detail panel (if row selected on this page) ───────────────────────────
    selected_id = st.session_state.get("selected_alert_id")
    if selected_id:
        alert = db.get_alert(selected_id)
        if alert:
            st.markdown("---")
            _render_alert_detail(alert)


# ── Alert detail view ─────────────────────────────────────────────────────────

def _render_alert_detail(alert: dict) -> None:
    draft = alert.get("alert_draft") or {}
    status = alert.get("approval_status", "pending")

    badge = {
        "pending": "🟡 PENDING",
        "approved": "🟢 APPROVED",
        "rejected": "🔴 REJECTED",
    }.get(status, status.upper())

    st.markdown(f"### Alert Detail — {badge}")
    st.caption(
        f"Source: {alert.get('source_name')} | "
        f"Created: {alert.get('created_at', '')[:19].replace('T', ' ')} UTC"
    )

    if not draft:
        st.warning("No structured draft available for this alert.")
        return

    _render_formatted_alert(draft)

    if alert.get("reviewer_notes"):
        st.markdown("**Reviewer Notes:**")
        st.info(alert["reviewer_notes"])

    if alert.get("dispatched_email"):
        st.success("Email dispatched successfully.")
    if alert.get("dispatch_error"):
        st.error(f"Dispatch error: {alert['dispatch_error']}")


def _render_formatted_alert(draft: dict) -> None:
    """Render a structured alert draft with no text truncation."""
    st.markdown(
        f"<div class='section-label'>LEGAL ALERT — {draft.get('practice_area','')}</div>"
        f"<div><strong>Date:</strong> {draft.get('date','')} &nbsp; "
        f"<strong>Source:</strong> {draft.get('source','')} &nbsp; "
        f"<strong>Type:</strong> {draft.get('update_type','')}</div>",
        unsafe_allow_html=True,
    )
    # Full headline — never truncated
    st.markdown(f"#### {draft.get('headline', '')}")

    st.markdown('<div class="section-label">Summary</div>', unsafe_allow_html=True)
    # st.write renders full text without any character limit
    st.write(draft.get("summary", ""))

    st.markdown('<div class="section-label">Key Provisions</div>', unsafe_allow_html=True)
    for p in draft.get("key_provisions", []):
        st.markdown(f"- {p}")  # full text — no truncation

    st.markdown('<div class="section-label">Stakeholder Implications</div>', unsafe_allow_html=True)
    for impl in draft.get("stakeholder_implications", []):
        # Full type and implication text
        st.markdown(f"**→ {impl.get('type', '')}:** {impl.get('implication', '')}")

    st.markdown('<div class="section-label">Recommended Action</div>', unsafe_allow_html=True)
    st.info(draft.get("recommended_action", ""))  # st.info renders full text

    tags = " | ".join(draft.get("practice_areas_tagged", []))
    st.caption(f"PRACTICE AREAS TAGGED: {tags}")
