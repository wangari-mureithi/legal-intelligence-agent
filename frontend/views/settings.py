"""
Settings page — manage monitored sources, email lists, and scrape schedule.
"""

import json
import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import database as db
from backend.config import sources_path, clients_path

_SOURCES_PATH = sources_path()
_CLIENTS_PATH = clients_path()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    st.title("Settings")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Monitored Sources",
        "Email Distribution",
        "Client List",
        "Scheduler",
        "Backfill",
        "Process URL",
    ])

    with tab1:
        _render_sources()
    with tab2:
        _render_email_settings()
    with tab3:
        _render_clients()
    with tab4:
        _render_scheduler()
    with tab5:
        _render_backfill()
    with tab6:
        _render_process_url()


# ── Tab: Monitored Sources ────────────────────────────────────────────────────

def _render_sources() -> None:
    st.subheader("Monitored Legal Sources")
    st.caption(
        "Add, remove, or toggle the sources the agent monitors. "
        "Changes take effect on the next scrape run."
    )

    sources = _load_json(_SOURCES_PATH)

    # Add new source
    with st.expander("Add New Source"):
        with st.form("add_source"):
            name     = st.text_input("Source Name", placeholder="e.g. KEBS Kenya")
            url      = st.text_input("URL", placeholder="https://www.kebs.org")
            category = st.text_input("Category", placeholder="Standards")
            add_btn  = st.form_submit_button("Add Source")

        if add_btn:
            if name and url:
                sources.append({"name": name, "url": url,
                                 "category": category, "enabled": True})
                _save_json(_SOURCES_PATH, sources)
                st.success(f"Source '{name}' added.")
                st.rerun()
            else:
                st.warning("Name and URL are required.")

    st.markdown("---")

    if not sources:
        st.info("No sources configured.")
        return

    # Display and toggle/remove existing sources
    for i, src in enumerate(sources):
        c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
        c1.markdown(f"**{src['name']}**  \n`{src['url']}`")
        c2.caption(src.get("category", "—"))

        enabled = c3.checkbox(
            "Enabled", value=src.get("enabled", True), key=f"src_enabled_{i}"
        )
        if enabled != src.get("enabled", True):
            sources[i]["enabled"] = enabled
            _save_json(_SOURCES_PATH, sources)
            st.rerun()

        if c4.button("Remove", key=f"src_remove_{i}"):
            sources.pop(i)
            _save_json(_SOURCES_PATH, sources)
            st.rerun()

        st.divider()


# ── Tab: Email Distribution ───────────────────────────────────────────────────

def _render_email_settings() -> None:
    st.subheader("Internal Team Email Distribution")
    st.caption(
        "These addresses receive every approved alert. "
        "Stored in your .env file as INTERNAL_TEAM_EMAILS."
    )

    import os
    current = os.getenv("INTERNAL_TEAM_EMAILS", "")

    with st.form("email_settings"):
        emails = st.text_area(
            "Internal Team Emails (one per line or comma-separated)",
            value=current.replace(",", "\n"),
            height=150,
            help="e.g. partner@firm.co.ke\nassociate@firm.co.ke",
        )
        save_btn = st.form_submit_button("Save", type="primary")

    if save_btn:
        cleaned = ",".join(
            e.strip()
            for e in emails.replace("\n", ",").split(",")
            if e.strip()
        )
        db.set_setting("internal_team_emails", cleaned)
        st.success(
            "Saved to settings database. "
            "Update INTERNAL_TEAM_EMAILS in your .env file to make this permanent."
        )
        st.code(f"INTERNAL_TEAM_EMAILS={cleaned}")

    # Show stored override
    stored = db.get_setting("internal_team_emails")
    if stored:
        st.info(f"Runtime override (from Settings DB): `{stored}`")


# ── Tab: Client List ──────────────────────────────────────────────────────────

def _render_clients() -> None:
    st.subheader("Client Distribution List")
    st.caption(
        "Clients are matched to alerts by practice area. "
        "An alert tagged 'Corporate' will be sent to all clients with 'Corporate' in their practice areas."
    )

    clients = _load_json(_CLIENTS_PATH)

    # Add new client
    with st.expander("Add New Client"):
        with st.form("add_client"):
            import uuid
            c_name = st.text_input("Client Name")
            c_email = st.text_input("Email Address")
            c_contact = st.text_input("Contact Person")
            c_pa = st.multiselect(
                "Practice Areas",
                options=["Corporate", "Tax", "Employment", "Banking & Finance",
                         "Capital Markets", "Telecoms & Tech", "Energy",
                         "Competition", "Court Rulings", "General"],
            )
            add_btn = st.form_submit_button("Add Client")

        if add_btn:
            if c_name and c_email:
                clients.append({
                    "id": f"client_{uuid.uuid4().hex[:6]}",
                    "name": c_name,
                    "email": c_email,
                    "practice_areas": c_pa,
                    "contact_person": c_contact,
                    "active": True,
                })
                _save_json(_CLIENTS_PATH, clients)
                st.success(f"Client '{c_name}' added.")
                st.rerun()
            else:
                st.warning("Name and email are required.")

    st.markdown("---")

    if not clients:
        st.info("No clients configured.")
        return

    for i, client in enumerate(clients):
        c1, c2, c3, c4 = st.columns([2.5, 2, 2.5, 1])
        c1.markdown(f"**{client['name']}**  \n`{client['email']}`")
        c2.caption(client.get("contact_person", "—"))
        c3.caption(", ".join(client.get("practice_areas", [])) or "All Areas")

        active = c4.checkbox(
            "Active", value=client.get("active", True), key=f"client_active_{i}"
        )
        if active != client.get("active", True):
            clients[i]["active"] = active
            _save_json(_CLIENTS_PATH, clients)
            st.rerun()

        st.divider()


# ── Tab: Scheduler ────────────────────────────────────────────────────────────

def _render_scheduler() -> None:
    st.subheader("Scraping Schedule")
    st.caption(
        "The agent scrapes all enabled sources on this interval. "
        "Changes require a scheduler restart."
    )

    current_interval = int(db.get_setting("scrape_interval_hours", "6"))

    with st.form("scheduler_settings"):
        interval = st.slider(
            "Scrape Interval (hours)",
            min_value=1,
            max_value=24,
            value=current_interval,
            step=1,
        )
        save_btn = st.form_submit_button("Save Interval", type="primary")

    if save_btn:
        db.set_setting("scrape_interval_hours", str(interval))
        st.success(
            f"Interval saved: every {interval} hour(s). "
            "Restart the scheduler process to apply."
        )

    st.markdown("---")
    st.subheader("Manual Trigger")
    st.caption("Run a scrape right now without waiting for the next scheduled run.")

    if st.button("Run Scrape Now", type="primary"):
        with st.spinner("Triggering scrape job…"):
            try:
                from backend.scheduler import trigger_now
                trigger_now()
                st.success(
                    "Scrape job triggered. "
                    "New pending alerts will appear in the Dashboard shortly."
                )
            except Exception as exc:
                st.error(f"Trigger failed: {exc}")


# ── Tab: Backfill ─────────────────────────────────────────────────────────────

def _render_backfill() -> None:
    st.subheader("Backfill Historical Alerts")
    st.caption(
        "Scan all enabled sources for regulatory updates published since a "
        "chosen start date. Each article that mentions 2025 or 2026 is passed "
        "to the LLM relevance filter and, if relevant, queued for your review."
    )

    from datetime import date as date_type

    st.info(
        "This follows article links on each source page and checks for recent "
        "content. It may take several minutes. Start with one or two sources "
        "to test before running all 13."
    )

    with st.form("backfill_form"):
        start_date = st.date_input(
            "Scan from date",
            value=date_type(2026, 1, 1),
            min_value=date_type(2025, 1, 1),
            max_value=date_type.today(),
        )

        sources_file = _load_json(_SOURCES_PATH)
        enabled_sources = [s for s in sources_file if s.get("enabled", True)]
        source_names = [s["name"] for s in enabled_sources]

        selected_sources = st.multiselect(
            "Limit to specific sources (leave empty = scan all)",
            options=source_names,
            default=[],
            help="Recommended: start with a few sources to test before scanning all.",
        )

        run_btn = st.form_submit_button("Start Backfill", type="primary")

    if run_btn:
        sources_to_scan = (
            [s for s in enabled_sources if s["name"] in selected_sources]
            if selected_sources
            else enabled_sources
        )

        if not sources_to_scan:
            st.warning("No sources selected.")
            return

        from backend.backfill import _backfill_source, _extract_article_links
        from backend.graph import graph
        from backend.database import init_db
        init_db()

        progress_bar = st.progress(0, text="Starting…")
        log_area = st.empty()
        log_lines = []
        total_queued = 0

        for i, source in enumerate(sources_to_scan):
            pct = i / len(sources_to_scan)
            progress_bar.progress(pct, text=f"Scanning {source['name']}…")
            log_lines.append(f"**{source['name']}** — fetching article links…")
            log_area.markdown("\n\n".join(log_lines[-10:]))

            try:
                # Show link count before processing
                links = _extract_article_links(source["url"])
                log_lines.append(
                    f"&nbsp;&nbsp;↳ Found {len(links)} candidate link(s). "
                    f"Checking for 2025/2026 content…"
                )
                log_area.markdown("\n\n".join(log_lines[-10:]))

                queued = _backfill_source(graph, source, start_date)
                total_queued += queued
                log_lines.append(
                    f"&nbsp;&nbsp;✅ Done — **{queued}** article(s) queued."
                )
            except Exception as exc:
                log_lines.append(f"&nbsp;&nbsp;⚠️ Error: {exc}")

            log_area.markdown("\n\n".join(log_lines[-10:]))

        progress_bar.progress(1.0, text="Backfill complete.")
        if total_queued:
            st.success(
                f"Backfill finished — **{total_queued} article(s)** queued. "
                "Go to **Alert Review** to approve them."
            )
        else:
            st.warning(
                "Backfill finished but no new articles were queued. "
                "Possible reasons:\n"
                "- The sources' article links don't mention 2025 or 2026 yet\n"
                "- The LLM relevance filter rejected all articles as non-regulatory\n"
                "- The sites blocked the scraper (try different sources)\n\n"
                "Try running the terminal command for more detail:\n"
                "`python -m backend.backfill --from 2026-01-01`"
            )


# ── Tab: Process Specific URL ─────────────────────────────────────────────────

def _render_process_url() -> None:
    st.subheader("Process a Specific URL")
    st.caption(
        "Paste the direct link to any document — a Bill, Gazette notice, "
        "court ruling, regulatory circular — and the agent will scrape it, "
        "check relevance, draft a legal alert, and queue it for your review."
    )

    st.markdown(
        "**Use this when the scraper misses a known document**, for example:\n"
        "- A specific Bill on the Parliament website\n"
        "- A CBK or CMA consultation paper\n"
        "- A court judgment from Kenya Law\n"
        "- Any external article or gazette notice"
    )

    with st.form("process_url_form"):
        url = st.text_input(
            "URL",
            placeholder="https://www.parliament.go.ke/bills/...",
        )

        col1, col2 = st.columns(2)
        with col1:
            source_name = st.text_input(
                "Source Name",
                placeholder="e.g. Parliament of Kenya",
            )
        with col2:
            source_category = st.selectbox(
                "Category",
                options=[
                    "Bills", "Gazette", "Legislation", "Tax & Finance",
                    "Capital Markets", "Banking", "Telecoms & Tech",
                    "Competition", "Energy", "Court Rulings",
                    "IP & Copyright", "News", "Legal Commentary", "Other",
                ],
            )

        submit = st.form_submit_button("Process URL", type="primary")

    if submit:
        if not url.strip():
            st.warning("Please enter a URL.")
            return

        import uuid
        from backend.graph import graph
        from backend.database import init_db
        from backend.scheduler import _save_paused_alert

        init_db()

        with st.spinner(f"Scraping and processing {url} …"):
            try:
                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}

                # Pass empty raw_content so web_scraper_node fetches and
                # extracts the URL properly (including PDF extraction).
                initial_state = {
                    "source_url": url,
                    "source_name": source_name or url,
                    "source_category": source_category,
                    "thread_id": thread_id,
                    "raw_content": "",          # let web_scraper_node handle fetch
                    "document_title": "",
                    "publication_date": "",
                    "is_relevant": False,
                    "is_duplicate": False,
                    "relevance_reason": "",
                    "alert_draft": {},
                    "approval_status": "pending",
                    "reviewer_notes": "",
                    "dispatched_email": False,
                    "dispatch_error": "",
                    "retry_count": 0,
                }

                for _event in graph.stream(
                    initial_state, config=config, stream_mode="values"
                ):
                    pass

                state_snapshot = graph.get_state(config)
                if state_snapshot.next:
                    # Graph paused at human_review — write to DB so it
                    # appears in Alert Review.
                    _save_paused_alert(thread_id, state_snapshot.values)
                    st.success(
                        "Done. The alert has been drafted and is waiting for "
                        "your review in **Alert Review**."
                    )
                else:
                    final_state = state_snapshot.values
                    reason = final_state.get("relevance_reason", "No reason provided.")
                    is_dup = final_state.get("is_duplicate", False)
                    if is_dup:
                        st.info(
                            f"This document has already been processed.\n\n"
                            f"**Reason:** {reason}"
                        )
                    else:
                        st.warning(
                            f"The LLM determined this URL is **not a regulatory update** "
                            f"and did not queue it.\n\n**Reason:** {reason}\n\n"
                            "If this is incorrect, try submitting again — "
                            "the auto-flag keyword list may need updating."
                        )

            except Exception as exc:
                st.error(f"Processing error: {exc}")
