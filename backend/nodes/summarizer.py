"""
summarizer_node — generates a structured legal alert from the raw content.

Applies the firm's tone & style rules:
  - Explanatory and firm, not alarmist
  - No passive voice where avoidable
  - No filler phrases
  - Every alert answers: What changed? Who is affected? What must they do?
  - Stakeholder implications are mandatory

On malformed output: retries once, then flags for manual review.
"""

import json
import logging
import re
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.state import AlertState
from backend.config import get_llm

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior lawyer at a Kenyan law firm with 15 years of experience in regulatory and commercial law. You write legal alerts for clients and internal teams.

Your writing is:
- Direct and authoritative. You do not hedge.
- Substantive. Every sentence carries legal information.
- Precise. You cite specific sections, notice numbers, and gazette references.
- Practical. You always tell the reader what to do, not just what happened.

You have never written the phrases: "it is important to note", "it should be noted", "this is a significant development", "stakeholders should be aware", "in conclusion".

Here are three examples of alerts written to our standard. Match this style exactly:

───────────────────────────────────────────────────────────────────────────────
EXAMPLE 1 — Draft Regulations
───────────────────────────────────────────────────────────────────────────────
LEGAL ALERT — GAMING & GAMBLING | REGULATORY

Date: 1 April 2025
Source: Betting Control and Licensing Board — https://bclb.go.ke
Type: Draft Regulations

Headline: Betting Control and Licensing Board Publishes Draft Gambling Control Regulations 2025 — Licence Fees Rise 5x, Foreign Ownership Cap Cut to 30%

Summary: The Betting Control and Licensing Board published the Draft Gambling Control Regulations, 2025 on 1 April 2025 under Legal Notice No. 45 of 2025, pursuant to Section 62 of the Gambling Control Act, 2023. The Regulations replace the Betting, Lotteries and Gaming Act (Cap. 131) framework entirely. Operators must obtain new category-specific licences by 30 June 2025 or face automatic revocation of existing permits under Regulation 15. Annual licence fees for sports betting operators rise from KES 10 million to KES 50 million under the Third Schedule. Section 34 mandates an industry-wide self-exclusion register, operational across all licensed platforms by 1 January 2026.

Key Provisions:
- Section 11: Five licence categories — sports betting, casino, lottery, skill games, virtual sports
- Section 23: Minimum capital requirement of KES 100 million for sports betting licences
- Third Schedule: Annual licence fees increased 5x across all categories
- Section 34: Industry-wide self-exclusion register operational by 1 January 2026
- Section 47: Advertising restricted to post-9pm broadcast hours; social media ads require BCLB pre-clearance

Stakeholder Implications:
→ Licensed Betting Operators: All existing BCLB permits expire on 30 June 2025. Submit new category-specific licence applications with updated capital evidence by 31 May 2025 to avoid a regulatory gap. Failure to comply triggers automatic revocation under Regulation 15.
→ Foreign Investors in Kenyan Gaming: Section 8 caps foreign ownership at 30%, down from 49%. Structures above 30% must be restructured before the June 2025 commencement date.
→ Media and Advertising Agencies: Pre-clearance requirements under Section 47 apply to all campaign materials from commencement, including existing contracts. Audit all current gambling advertising campaigns immediately.

Recommended Action: File submissions on the draft by 30 April 2025. Licensed operators must begin recapitalisation and restructuring now to meet the 31 May 2025 application window.

───────────────────────────────────────────────────────────────────────────────
EXAMPLE 2 — Court Ruling
───────────────────────────────────────────────────────────────────────────────
LEGAL ALERT — EMPLOYMENT LAW | CORPORATE

Date: 14 February 2025
Source: Kenya Law — https://www.kenyalaw.org
Type: Court Ruling

Headline: ELRC Rules Unilateral Reduction of Commission Structures During Restructuring Is Constructive Dismissal — 18 Months' Compensation Awarded

Summary: In Nairobi ELRC Cause No. 234 of 2024 (Otieno v Safaricom PLC), Justice Ndolo held that unilateral reduction of commission structures during business restructuring constitutes a fundamental breach of contract under Section 10(5) of the Employment Act, 2007, entitling the employee to treat the contract as constructively terminated. The court rejected the defence that board approval of a restructuring resolution provides legal authority to vary individual employment terms without consent. Compensation was awarded at 18 months' salary. The ruling applies to all ongoing restructurings involving changes to performance-linked remuneration.

Key Provisions:
- Section 10(5), Employment Act, 2007: Employer must give written notice and obtain employee consent before varying any contractual term — board resolution is not sufficient authority
- 18-month compensation benchmark established for constructive dismissal where commission is a material contract term
- Non-solicitation clauses remain enforceable for up to 24 months where supported by consideration
- Ruling applies retroactively to all pending employment disputes involving benefit reductions

Stakeholder Implications:
→ Corporates Undergoing Restructuring: Any variation to commission, bonus, or incentive structures requires individual written consent — not board resolution. Audit all restructuring plans currently in progress for compliance with Section 10(5).
→ Listed Companies: Remuneration committees must ensure approved pay changes are implemented through individual contract amendments. Failure to do so creates constructive dismissal liability at 18 months per employee.
→ Financial Services Firms: Banks, insurers, and stockbrokers where commission is a material term are directly exposed. Review all agent and employee commission agreements under any current restructuring.

Recommended Action: Immediately audit all ongoing restructurings involving performance-linked pay changes. Obtain individual written consent for every variation. Do not proceed on board resolution authority alone.

───────────────────────────────────────────────────────────────────────────────
EXAMPLE 3 — CBK Circular
───────────────────────────────────────────────────────────────────────────────
LEGAL ALERT — BANKING & FINANCE | FINTECH

Date: 28 March 2025
Source: Central Bank of Kenya — https://www.centralbank.go.ke
Type: Policy Circular

Headline: CBK Circular No. 4 of 2025 — All Digital Credit Providers Must Integrate With Credit Reference Bureaus by 30 June 2025 or Face Licence Suspension

Summary: The Central Bank of Kenya issued Circular No. 4 of 2025 on 28 March 2025 under Section 31(3) of the Central Bank of Kenya Act, Cap. 491. All licensed Digital Credit Providers must achieve full data-sharing integration with at least two licensed Credit Reference Bureaus by 30 June 2025. Providers failing to integrate face licence suspension under Regulation 22 of the Digital Credit Providers Regulations, 2022 without further notice. Monthly CRB reporting replaces the current quarterly cycle from 1 July 2025. The CBK has confirmed no extensions will be granted.

Key Provisions:
- Paragraph 3: Integration with at least two CRBs mandatory by 30 June 2025
- Paragraph 5: Monthly CRB reporting cycle effective 1 July 2025 — replaces quarterly cycle
- Paragraph 7: CBK will conduct integration audits in Q3 2025; non-compliant DCPs face immediate licence suspension
- Paragraph 9: APIs must conform to the CBK Open API Framework published February 2025

Stakeholder Implications:
→ Licensed Digital Credit Providers: Begin CRB integration scoping and procurement now. Execute CRB vendor contracts by 30 April 2025 to allow adequate testing time before the 30 June deadline.
→ Credit Reference Bureaus: Expect a surge in DCP integration requests. Publish API documentation and integration timelines by 30 April 2025 as required under Paragraph 9.
→ Private Equity and Venture Investors in Kenyan Fintech: Portfolio companies with DCP licences must be assessed for compliance readiness immediately. A licence suspension triggers covenant breaches in most investment agreements and shareholder loans.

Recommended Action: Map current CRB data-sharing arrangements against Circular No. 4 requirements. Execute CRB integration agreements by 30 April 2025. Notify board and investors of the 30 June 2025 deadline.
───────────────────────────────────────────────────────────────────────────────

DATE FIELDS — follow these rules exactly:
- "date" = the date the source document was PUBLISHED or RELEASED. Find it in the byline, gazette date, bill introduction date, or circular header. Do NOT use today's date.
- "summary" = include when the law or regulation COMES INTO FORCE, if stated.
- "recommended_action" = state the compliance deadline. If no deadline is stated: "Monitor for gazette notice confirming commencement date."

REGIONAL/INTERNATIONAL ALERTS:
- If geography_scope is "regional" or "international", focus stakeholder implications on what Kenyan regulators (CBK, CMA) may do in response and what Kenyan businesses must monitor.
- For EAC/COMESA alerts, focus on cross-border compliance implications for Kenyan businesses.

OUTPUT FORMAT — JSON object only, no markdown, no extra text:
{
  "practice_area": "e.g. Banking & Finance | Corporate",
  "date": "YYYY-MM-DD",
  "source": "Source Name — https://source-url",
  "update_type": "New Bill | Regulation | Statutory Instrument | Court Ruling | Policy Circular | Draft Regulations | Draft Framework",
  "headline": "One sentence. Active voice. States the legal change and its immediate effect. Must end with a full stop.",
  "summary": "4-6 sentences. Include commencement date if stated. No filler phrases. Must end with a full stop.",
  "key_provisions": [
    "Section X: Description — cite the section number",
    "Section Y: Description — cite the section number"
  ],
  "stakeholder_implications": [
    {"type": "Specific Stakeholder Type", "implication": "Specific, actionable implication. Must end with a full stop."},
    {"type": "Specific Stakeholder Type", "implication": "Specific, actionable implication. Must end with a full stop."}
  ],
  "recommended_action": "State the specific action and deadline. If unknown: Monitor for gazette notice confirming commencement date.",
  "practice_areas_tagged": ["Banking & Finance", "Corporate"]
}

Always include at least 3 stakeholder types where the content supports it. Be specific — not generic. Reference section numbers wherever the source material supports it."""


def summarizer_node(state: AlertState) -> dict[str, Any]:
    raw = state.get("raw_content", "")
    retry_count = state.get("retry_count", 0)

    if not raw.strip():
        return {
            "alert_draft": _error_draft("No content available for summarisation."),
            "approval_status": "pending",
        }

    llm = get_llm()
    today = date.today().isoformat()
    source_line = (
        f"{state.get('source_name', 'Unknown Source')} — {state.get('source_url', '')}"
    )
    geo_scope = state.get("geography_scope", "kenya")
    kenya_nexus = state.get("kenya_nexus", "")
    date_confirmed = state.get("date_confirmed", True)

    user_msg = (
        f"Today's date: {today}\n"
        f"Source: {source_line}\n"
        f"Category: {state.get('source_category', 'General')}\n"
        f"Geography scope: {geo_scope}\n"
        f"Kenya nexus: {kenya_nexus or 'Kenyan source'}\n"
        f"Date confirmed: {'Yes' if date_confirmed else 'No — use retrieval date as placeholder'}\n\n"
        f"--- SCRAPED CONTENT ---\n{raw[:10000]}\n--- END ---\n\n"
        "Generate the structured legal alert JSON now."
    )

    for attempt in range(2):
        try:
            response = llm.invoke([
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ])
            draft = _parse_and_validate(response.content)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                logger.warning("Malformed JSON on attempt 1 — retrying. Error: %s", exc)
                user_msg += (
                    "\n\nWARNING: Your previous response was not valid JSON. "
                    "Output ONLY the JSON object, nothing else. Do not truncate any field."
                )
                continue
            logger.error("Summariser failed after retry for %s: %s",
                         state.get("source_url"), exc)
            return {
                "alert_draft": _error_draft(
                    f"Auto-generation failed after retry. Manual drafting required. Error: {exc}"
                ),
                "approval_status": "pending",
                "retry_count": retry_count + 2,
            }
        except Exception as exc:
            logger.error("Unexpected summariser error for %s: %s",
                         state.get("source_url"), exc)
            return {
                "alert_draft": _error_draft(f"LLM error: {exc}"),
                "approval_status": "pending",
            }

        # ── Completeness validation ───────────────────────────────────────────
        # Catch LLM output that was cut off mid-sentence before any post-processing.
        failures = _validate_completeness(draft)
        if failures:
            if attempt == 0:
                logger.warning(
                    "Draft appears incomplete (attempt 1) — retrying. Issues: %s", failures
                )
                user_msg += (
                    "\n\nCRITICAL: Your previous response was cut off or incomplete. "
                    "Return the COMPLETE, UNTRUNCATED alert JSON. "
                    "Do not stop mid-sentence under any circumstance. "
                    "Every field must be a fully complete thought or sentence. "
                    f"Specific issues to fix: {'; '.join(failures)}"
                )
                continue  # force retry
            else:
                # Second attempt also incomplete — mark for manual review but don't discard
                logger.error(
                    "Draft still incomplete after retry for %s — flagging for review. Issues: %s",
                    state.get("source_url"), failures,
                )
                draft["headline"] = "⚠️ [INCOMPLETE — MANUAL REVIEW REQUIRED] " + draft.get("headline", "")
                draft["summary"] = (
                    "⚠️ This alert was generated incompletely and requires manual completion. "
                    f"Issues detected: {'; '.join(failures)}\n\n"
                    + draft.get("summary", "")
                )

        # ── Date-unconfirmed warning ──────────────────────────────────────────
        if not state.get("date_confirmed", True):
            retrieval_date = state.get("publication_date", "")
            warning = (
                f"⚠️ Publication date unconfirmed — retrieved on {retrieval_date}. "
                "Please verify the actual publication date before dispatch."
            )
            draft["summary"] = warning + "\n\n" + draft.get("summary", "")
            if "monitor" not in draft.get("recommended_action", "").lower():
                draft["recommended_action"] = (
                    draft.get("recommended_action", "")
                    + " [Note: Publication date unconfirmed — verify before relying on compliance deadlines.]"
                )
            draft["date_confirmed"] = False
        else:
            draft["date_confirmed"] = True

        # ── Regional / international alert banner ─────────────────────────────
        geo_scope = state.get("geography_scope", "kenya")
        kenya_nexus = state.get("kenya_nexus", "")
        if geo_scope in ("regional", "international") and kenya_nexus:
            region_label = "REGIONAL ALERT" if geo_scope == "regional" else "INTERNATIONAL ALERT"
            origin = state.get("source_name", "an international source")
            regional_note = (
                f"🌍 {region_label} — This update originates from {origin} "
                f"but is relevant to Kenya because: {kenya_nexus}"
            )
            draft["summary"] = regional_note + "\n\n" + draft.get("summary", "")
            draft["regional_relevance"] = True
        else:
            draft["regional_relevance"] = False

        logger.info("Alert drafted for %s", state.get("source_name"))
        return {
            "alert_draft": draft,
            "approval_status": "pending",
            "retry_count": retry_count + attempt,
        }

    # Should not reach here
    return {"alert_draft": _error_draft("Unknown summariser error."), "approval_status": "pending"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_completeness(draft: dict) -> list[str]:
    """
    Return a list of failure reasons if the draft appears truncated or incomplete.
    An empty list means the draft passed all checks.

    Checks:
    - headline: ≥15 chars; last char is not a bare alphanumeric (mid-word cut)
    - summary: ≥2 sentence-ending punctuation marks (periods, !, ?)
    - stakeholder_implications: ≥2 entries, each implication ≥15 chars
    - recommended_action: ≥20 chars
    """
    failures = []

    headline = (draft.get("headline") or "").strip()
    summary = (draft.get("summary") or "").strip()
    recommended = (draft.get("recommended_action") or "").strip()
    implications = draft.get("stakeholder_implications") or []

    # Headline
    if len(headline) < 15:
        failures.append(f"Headline too short ({len(headline)} chars — minimum 15)")
    elif headline and headline[-1].isalnum():
        # Ends on a bare letter/digit — likely cut mid-word or mid-sentence
        # Exception: short acronyms or ALL-CAPS endings are sometimes valid
        words = headline.split()
        last_word = words[-1] if words else ""
        if len(last_word) > 2 and not last_word.isupper():
            failures.append(
                f"Headline appears cut off mid-word (ends with: '…{last_word}')"
            )

    # Summary — require at least 2 sentence-ending punctuation marks
    sentence_endings = sum(1 for ch in summary if ch in ".!?")
    if sentence_endings < 2:
        failures.append(
            f"Summary too short or incomplete ({sentence_endings} sentence(s) detected — need ≥2)"
        )

    # Stakeholder implications
    if len(implications) < 2:
        failures.append(
            f"Too few stakeholder implications ({len(implications)} found — need ≥2)"
        )
    else:
        for impl in implications:
            implication_text = (impl.get("implication") or "").strip()
            if len(implication_text) < 15:
                failures.append(
                    f"Stakeholder implication for '{impl.get('type', '?')}' appears "
                    f"truncated ({len(implication_text)} chars)"
                )
                break

    # Recommended action
    if len(recommended) < 20:
        failures.append(
            f"Recommended action too short ({len(recommended)} chars — minimum 20)"
        )

    return failures


_REQUIRED_KEYS = {
    "practice_area", "date", "source", "update_type",
    "headline", "summary", "key_provisions",
    "stakeholder_implications", "recommended_action", "practice_areas_tagged",
}


def _parse_and_validate(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    # Try direct parse first
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in LLM response.")
        data = json.loads(match.group())

    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"Missing required keys: {missing}")

    # Ensure list fields are lists
    for key in ("key_provisions", "stakeholder_implications", "practice_areas_tagged"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"Field '{key}' must be a list.")

    # Ensure stakeholder_implications have required sub-keys
    for item in data["stakeholder_implications"]:
        if not isinstance(item, dict) or "type" not in item or "implication" not in item:
            raise ValueError("stakeholder_implications items must have 'type' and 'implication'.")

    return data


def _error_draft(message: str) -> dict:
    """Fallback draft when generation fails — allows manual editing in the UI."""
    return {
        "practice_area": "Review Required",
        "date": date.today().isoformat(),
        "source": "",
        "update_type": "Review Required",
        "headline": f"[AUTO-GENERATION FAILED] {message}",
        "summary": message,
        "key_provisions": ["Manual review required"],
        "stakeholder_implications": [
            {"type": "All Clients", "implication": "Please review and complete manually."}
        ],
        "recommended_action": "Complete this alert manually before dispatch.",
        "practice_areas_tagged": [],
    }
