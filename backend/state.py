"""
AlertState — the single shared state object threaded through every LangGraph node.
"""

from typing import TypedDict, Optional


class AlertState(TypedDict):
    # ── Ingestion ──────────────────────────────────────────────────────────────
    raw_content: str           # Full scraped text from the source page
    source_url: str            # URL that was scraped
    source_name: str           # Human-readable source label (e.g. "Kenya Law")
    source_category: str       # Category tag from sources.json
    source_base_url: str    # Base URL of the source from sources.json (for source tracking)

    # ── Publication date extraction (done in web_scraper_node) ────────────────
    publication_date: str      # Date the document was published/gazetted (YYYY-MM-DD)
                               # This is NOT today's date and NOT the commencement date.
                               # It is the date the gazette notice, bill, judgment, or
                               # circular was officially released.
    document_title: str        # Title of the specific document (e.g. "Finance Bill 2026")

    # ── Relevance ─────────────────────────────────────────────────────────────
    is_relevant: bool          # True → pass to summariser; False → END
    relevance_reason: str      # LLM explanation for the relevance decision
    is_duplicate: bool         # True if already alerted on — skip without processing

    # ── Geography & Date metadata ──────────────────────────────────────────────
    geography_scope: str    # "kenya" | "regional" | "international"
    kenya_nexus: str        # explanation of why relevant if not a Kenyan source
    date_confirmed: bool    # True if pub date found in document, False if retrieval date used

    # ── Summarisation ─────────────────────────────────────────────────────────
    alert_draft: dict          # Structured alert (see ALERT_DRAFT_SCHEMA below)

    # ── Human Review ──────────────────────────────────────────────────────────
    approval_status: str       # "pending" | "approved" | "rejected" | "redraft"
    reviewer_notes: str        # Free-text comments from the reviewing partner

    # ── Dispatch ──────────────────────────────────────────────────────────────
    dispatched_email: bool     # True once emails have been sent
    dispatch_error: str        # Non-empty if email send failed

    # ── Metadata ──────────────────────────────────────────────────────────────
    thread_id: str             # LangGraph thread identifier (== our DB row ID)
    retry_count: int           # Summarisation retries so far (max 1)


# ── ALERT_DRAFT_SCHEMA ────────────────────────────────────────────────────────
# Keys expected inside alert_draft:
#   practice_area   : str   — e.g. "Corporate | Tax"
#   date            : str   — publication date of the SOURCE DOCUMENT (not today)
#   source          : str   — "Name — URL"
#   update_type     : str   — "New Bill" | "Regulation" | "Statutory Instrument" |
#                             "Court Ruling" | "Policy Circular"
#   headline        : str   — one-sentence active-voice headline
#   summary         : str   — 3-5 sentence paragraph; must include commencement date
#   key_provisions  : list[str]
#   stakeholder_implications : list[dict]  — [{"type": str, "implication": str}]
#   recommended_action : str — must include compliance deadline or commencement date
#   practice_areas_tagged : list[str]
#   date_confirmed  : bool  — True if pub date found in doc; False if retrieval date used
#   regional_relevance : bool — True if content is from outside Kenya but relevant
