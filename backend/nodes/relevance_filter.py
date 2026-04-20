"""
relevance_filter_node — LLM-powered gate with deduplication.

Step 1: Deduplication check against SQLite.
  - If the same source_url OR the same document title + publication date
    already exists in the DB → skip immediately, log reason, return END.

Step 1.5: Hard approve — unambiguous regulatory phrases bypass everything.

Step 1.7: Geography filter — reject content with no Kenya nexus.

Step 2: LLM relevance + publication date validation.
  - Confirm the document was published in 2025 or later.
  - Confirm it is a genuine regulatory update (bill, regulation, ruling, circular).
  - Reject listing/index pages, old content, opinion pieces, admin notices.
"""

import json
import logging
import re
from typing import Any

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage

from backend.state import AlertState
from backend.config import get_llm
from backend import database as db

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior Kenyan legal analyst. Your ONLY job here is to decide whether this content is a genuine regulatory legal instrument — not to check dates.

── WHAT COUNTS AS A REGULATORY UPDATE ────────────────────────────────────────
Answer YES (is_relevant: true) if the content IS or CONTAINS:
- A Bill (any stage: draft, tabled, passed) — e.g. Finance Bill, Gambling Control Bill, AI Bill, Copyright Bill
- A Regulation, Statutory Instrument, or Legal Notice
- A Gazette Notice or Kenya Gazette Supplement
- A Court Ruling, Judgment, or Decree (High Court, Court of Appeal, Supreme Court)
- A Policy Circular, Regulatory Guideline, Framework, or Directive from a government body or regulator
- A Draft regulation, draft framework, or consultation paper from any Kenyan regulator

Answer NO (is_relevant: false) ONLY if the content is clearly:
- A blank or empty page
- A pure navigation/index page with no document content
- A job posting, tender notice, or staff recruitment
- An event invitation or calendar notice
- A generic news article that merely mentions a law without containing the actual instrument

── DATE RULE ──────────────────────────────────────────────────────────────────
Do NOT reject a document because you cannot find its publication date.
- If you find a date, record it.
- If you cannot find a date, set publication_date to null and still assess relevance on content alone.
- A Bill PDF with no visible date is still a Bill. Assess it as relevant.

── OLD CONTENT ────────────────────────────────────────────────────────────────
If the content is clearly from before 2020 with no relevance to current law, set is_relevant: false.
Otherwise, when in doubt, set is_relevant: true — a human partner will make the final call.

Respond with a JSON object only — no markdown, no extra text:
{
  "is_relevant": true or false,
  "reason": "One sentence. State what the document IS, not why you rejected it.",
  "publication_date": "YYYY-MM-DD or null",
  "document_title": "Full title of the legal instrument, or best guess",
  "update_type": "New Bill | Regulation | Statutory Instrument | Court Ruling | Policy Circular | Draft Framework | Not Applicable"
}"""


# ── Hard approve phrases — checked before everything else ─────────────────────
# These unambiguous regulatory phrases trigger an immediate approve without LLM.

_HARD_APPROVE_PHRASES = [
    "draft regulations",
    "draft guidelines",
    "draft framework",
    "draft policy",
    "draft bill",
    "gazette notice",
    "legal notice",
    "statutory instrument",
    "consultation paper",
    "exposure draft",
    "public participation",
    "cabinet secretary",
    "county regulations",
]


def _hard_approve_check(title: str, content_preview: str) -> tuple[bool, str]:
    """
    Check title and first 500 chars of content against hard-approve phrases.
    Returns (True, matched_phrase) if found, else (False, "").
    """
    combined = (title + " " + content_preview[:500]).lower()
    for phrase in _HARD_APPROVE_PHRASES:
        if phrase in combined:
            return True, phrase
    return False, ""


# ── Kenya domain & body indicators ────────────────────────────────────────────
_KENYA_DOMAINS = [".go.ke", ".co.ke", ".ac.ke", ".or.ke", "kenyalaw.org", "parliament.go.ke"]
_KENYA_NEWS_SITES = ["businessdailyafrica.com", "standardmedia.co.ke", "nation.africa", "mondaq.com/kenya"]
_KENYA_BODY_TERMS = [
    "kenya", "kenyan", "nairobi", "national assembly", "senate of kenya",
    "court of appeal of kenya", "high court of kenya", "supreme court of kenya",
    "cabinet secretary", "principal secretary",
    "capital markets authority", "central bank of kenya", "communications authority",
    "competition authority of kenya", "energy and petroleum regulatory",
    "data protection commissioner", "national treasury", "kenya revenue authority",
    "betting control and licensing", "bclb", "kepi", "icta",
    "county government", "county assembly",
]

# ── Regional / international exception triggers ────────────────────────────────
_EAC_COMESA_TERMS = [
    "east african community", "eac regulation", "eac directive", "eac framework",
    "comesa", "african union directive", "au regulation",
    "imf directive", "world bank policy",
]
_GLOBAL_FINTECH_TERMS = [
    "crypto regulation", "cryptocurrency regulation", "virtual asset service",
    "vasp licensing", "vasp regulation", "digital asset regulation",
    "stablecoin regulation", "defi regulation", "cbdc launch", "cbdc framework",
    "bitcoin regulation", "crypto ban", "crypto exchange regulation",
    "sec vs", "ftx", "binance regulation",
]
_EA_COUNTRY_TERMS = ["tanzania", "uganda", "rwanda", "ethiopia", "south sudan", "burundi"]
_EA_FINANCE_TERMS = [
    "mobile money", "payment system regulation", "banking regulation",
    "capital markets regulation", "tax treaty", "mobile banking regulation",
    "fintech regulation", "digital payment regulation",
]


def _geography_filter(
    source_url: str,
    source_name: str,
    raw_content: str,
    document_title: str,
) -> tuple[bool, str, str]:
    """
    Determine whether the content has a Kenya nexus.

    Returns:
        (is_relevant, geography_scope, kenya_nexus)
        geography_scope: "kenya" | "regional" | "international"
        kenya_nexus: empty string for Kenya sources; explanation for others
    """
    url_l = source_url.lower()
    name_l = source_name.lower()
    combined = (document_title + " " + raw_content[:2500]).lower()

    # ── 1. Clearly a Kenyan source ─────────────────────────────────────────
    if any(d in url_l for d in _KENYA_DOMAINS):
        return True, "kenya", ""
    if any(s in url_l for s in _KENYA_NEWS_SITES):
        return True, "kenya", ""
    if any(t in name_l for t in ["kenya", "kenyan", "nairobi"]):
        return True, "kenya", ""

    # ── 2. Content contains strong Kenya regulatory signals ────────────────
    kenya_hits = sum(1 for t in _KENYA_BODY_TERMS if t in combined)
    if kenya_hits >= 2:
        return True, "kenya", ""

    # Single Kenya mention + regulatory context
    if "kenya" in combined:
        reg_ctx = ["regulation", "bill", "act", "circular", "gazette", "legislation",
                   "court", "tribunal", "compliance", "statutory"]
        if any(t in combined for t in reg_ctx):
            return True, "kenya", ""

    # ── 3. EAC / COMESA / AU — regional exception ─────────────────────────
    if any(t in combined for t in _EAC_COMESA_TERMS):
        return True, "regional", (
            "EAC/COMESA/African Union regulation applicable to Kenya as a member state."
        )

    # ── 4. Global fintech / crypto — international exception ──────────────
    if any(t in combined for t in _GLOBAL_FINTECH_TERMS):
        return True, "international", (
            "Major global digital asset or fintech development with potential impact "
            "on Kenyan financial regulation (CBK, CMA, or Parliament)."
        )

    # ── 5. East African finance/banking — regional exception ──────────────
    if any(c in combined for c in _EA_COUNTRY_TERMS):
        if any(t in combined for t in _EA_FINANCE_TERMS):
            return True, "regional", (
                "East African financial services development with cross-border "
                "relevance to Kenya."
            )

    # ── 6. Not relevant to Kenya ──────────────────────────────────────────
    return False, "international", ""


# ── Kenya-specific auto-flag keywords ────────────────────────────────────────
# If any of these appear in the content, the document bypasses the LLM gate
# and is sent directly to the summariser. The LLM still validates the date
# and drafts the alert — this just skips the binary relevance check.

_AUTO_FLAG_TERMS = [
    # The 6 specific missing documents
    "copyright bill", "copyright and related rights bill",
    "artificial intelligence bill", "ai bill",
    "virtual asset service provider", "vasp regulations",
    "digital credit provider", "dcp framework", "dcp press release",
    "gambling control regulations", "betting control regulations",
    "financial consumer protection framework",
    # Broad Kenya regulatory auto-flags
    "legal notice no.", "gazette notice no.", "kenya gazette supplement",
    "statutory instrument", "bill, 202", "act, 202",
    "first reading", "second reading", "third reading",
    "national assembly bills", "senate bills",
    "draft regulations", "draft framework", "draft policy",
    "public participation", "invitation to comment", "public comments",
    "cabinet secretary", "principal secretary",
    "capital markets authority", "central bank of kenya",
    "communications authority of kenya",
    "competition authority of kenya",
    "energy and petroleum regulatory",
    "betting control and licensing",
    "data protection commissioner",
    "national intelligence service",
]


def _keyword_auto_flag(text: str) -> tuple[bool, str]:
    """
    Check content against Kenya-specific keyword list.
    Returns (flagged, matched_term).
    Fast — no LLM call needed.
    """
    lower = text.lower()
    for term in _AUTO_FLAG_TERMS:
        if term in lower:
            return True, term
    return False, ""


def _recency_filter(
    source_url: str,
    source_base_url: str,
    publication_date: str,
    date_confirmed: bool,
    max_age_days: int = 30,
) -> tuple[bool, str]:
    """
    Check whether a document is recent enough to process.

    Returns (should_proceed, reason).
    - should_proceed = True  → continue pipeline
    - should_proceed = False → discard — document is old news

    Rules:
    - date_confirmed=True and pub_date > 30 days ago → discard
    - date_confirmed=True and pub_date <= last_seen_date for this source → discard (already seen)
    - date_confirmed=False → always proceed (flag as unconfirmed)
    """
    from datetime import date, timedelta

    if not date_confirmed or not publication_date:
        # Cannot confirm date — proceed but do not block
        return True, "date_unconfirmed"

    today = date.today()
    cutoff = today - timedelta(days=max_age_days)

    try:
        pub_date = date.fromisoformat(publication_date)
    except ValueError:
        # Unparseable date string — don't block on it
        return True, "date_unparseable"

    # Rule 1: too old
    if pub_date < cutoff:
        return False, (
            f"Document dated {publication_date} is older than {max_age_days} days "
            f"(cutoff: {cutoff.isoformat()}). Not a new update."
        )

    # Rule 2: already seen for this source
    if source_base_url:
        tracking = db.get_source_tracking(source_base_url)
        if tracking and tracking.get("last_seen_date"):
            try:
                last_seen = date.fromisoformat(tracking["last_seen_date"])
                if pub_date <= last_seen:
                    return False, (
                        f"Document dated {publication_date} already seen for this source "
                        f"(last seen: {tracking['last_seen_date']})."
                    )
            except ValueError:
                pass  # Bad last_seen_date in DB — don't block

    return True, ""


def relevance_filter_node(state: AlertState) -> dict[str, Any]:
    raw = state.get("raw_content", "")
    source_url = state.get("source_url", "")
    source_name = state.get("source_name", "Unknown")

    # ── Step 1: Deduplication ─────────────────────────────────────────────────
    scraped_pub_date = state.get("publication_date", "")
    scraped_title = state.get("document_title", "")

    is_dup, dup_reason = db.check_duplicate(
        source_url=source_url,
        document_title=scraped_title,
        publication_date=scraped_pub_date,
    )
    if is_dup:
        logger.info("Duplicate — skipping %s: %s", source_url, dup_reason)
        db.log_action(
            alert_id="dedup",
            action="duplicate_skipped",
            details={"url": source_url, "reason": dup_reason},
        )
        return {
            "is_relevant": False,
            "is_duplicate": True,
            "relevance_reason": f"Duplicate: {dup_reason}",
            "geography_scope": "kenya",
            "kenya_nexus": "",
        }

    if not raw.strip():
        return {
            "is_relevant": False,
            "is_duplicate": False,
            "relevance_reason": "No content to evaluate.",
        }

    # ── Step 1.3: Recency filter ──────────────────────────────────────────────
    date_confirmed = state.get("date_confirmed", False)
    source_base_url = state.get("source_base_url", "")
    proceed, recency_reason = _recency_filter(
        source_url=source_url,
        source_base_url=source_base_url,
        publication_date=scraped_pub_date,
        date_confirmed=date_confirmed,
    )
    if not proceed:
        logger.info("Recency filter rejected %s: %s", source_url, recency_reason)
        db.log_action(
            alert_id="recency",
            action="recency_filtered",
            details={"url": source_url, "reason": recency_reason},
        )
        return {
            "is_relevant": False,
            "is_duplicate": False,
            "relevance_reason": f"Recency filter: {recency_reason}",
            "geography_scope": "kenya",
            "kenya_nexus": "",
        }

    # ── Step 1.5: Hard approve — unambiguous regulatory phrases ──────────────
    hard_flagged, hard_term = _hard_approve_check(scraped_title, raw)
    if hard_flagged:
        logger.info("HARD-APPROVED '%s' in %s", hard_term, source_name)
        return {
            "is_relevant": True,
            "is_duplicate": False,
            "relevance_reason": f"Hard-approved: unambiguous regulatory phrase '{hard_term}'.",
            "publication_date": scraped_pub_date,
            "document_title": scraped_title,
            "geography_scope": "kenya",
            "kenya_nexus": "",
        }

    # ── Step 1.7: Geography filter ────────────────────────────────────────────
    geo_relevant, geo_scope, geo_nexus = _geography_filter(
        source_url=source_url,
        source_name=source_name,
        raw_content=raw,
        document_title=scraped_title,
    )
    if not geo_relevant:
        reason = "Outside geographical scope — no Kenya nexus found."
        logger.info("Geography filter rejected %s: %s", source_url, reason)
        db.log_action(
            alert_id="geo_filter",
            action="geography_filtered",
            details={"url": source_url, "reason": reason},
        )
        return {
            "is_relevant": False,
            "is_duplicate": False,
            "relevance_reason": reason,
            "geography_scope": "international",
            "kenya_nexus": "",
        }

    # ── Step 2: Kenya keyword auto-flag (no LLM needed) ──────────────────────
    flagged, matched_term = _keyword_auto_flag(raw)
    if flagged:
        logger.info(
            "AUTO-FLAGGED '%s' in %s — skipping LLM gate, sending to summariser.",
            matched_term, source_name,
        )
        return {
            "is_relevant": True,
            "is_duplicate": False,
            "relevance_reason": f"Auto-flagged: keyword match '{matched_term}'.",
            "publication_date": scraped_pub_date,
            "document_title": scraped_title,
            "geography_scope": geo_scope,
            "kenya_nexus": geo_nexus,
        }

    # ── Step 3: LLM relevance + date validation ───────────────────────────────
    llm = get_llm()
    source_context = (
        f"Source: {source_name} ({source_url})\n"
        f"Category: {state.get('source_category', 'General')}\n"
        f"Publication date extracted by scraper: {scraped_pub_date or 'not found'}\n"
        f"Document title extracted by scraper: {scraped_title or 'not found'}\n\n"
    )

    user_msg = (
        f"{source_context}"
        f"--- SCRAPED CONTENT (first 8000 chars) ---\n{raw[:8000]}\n--- END ---\n\n"
        "Evaluate this content. Respond with the JSON object only."
    )

    try:
        response = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ])
        result = _parse_json_response(response.content)

        is_relevant = bool(result.get("is_relevant", False))
        llm_pub_date = result.get("publication_date") or scraped_pub_date or ""
        llm_title = result.get("document_title") or scraped_title or ""

        logger.info(
            "Relevance=%s | %s | pub_date=%s | reason=%s",
            is_relevant, source_name, llm_pub_date, result.get("reason", ""),
        )

        return {
            "is_relevant": is_relevant,
            "is_duplicate": False,
            "relevance_reason": result.get("reason", ""),
            # Enrich state with LLM-validated date and title
            "publication_date": llm_pub_date,
            "document_title": llm_title,
            "geography_scope": geo_scope,
            "kenya_nexus": geo_nexus,
        }

    except Exception as exc:
        logger.error("Relevance filter failed for %s: %s", source_url, exc)
        return {
            "is_relevant": False,
            "is_duplicate": False,
            "relevance_reason": f"Filter error: {exc}",
            "geography_scope": geo_scope,
            "kenya_nexus": geo_nexus,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise
