"""
email_dispatcher_node — sends approved legal alerts via Gmail SMTP.

Two sends per approved alert:
  1. Internal team email (INTERNAL_TEAM_EMAILS env var).
  2. Per-client emails where the client's practice_areas intersect with
     the alert's practice_areas_tagged (loaded from config/clients.json).

Email format: clean HTML using the structured legal alert template.
Subject: LEGAL ALERT | [Update Type] | [Headline]

On failure: logs the error, sets dispatch_error in state, marks
dispatched_email=False so the Streamlit UI shows a retry button.
"""

import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from backend.state import AlertState
from backend import database as db

logger = logging.getLogger(__name__)

_CLIENTS_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Node ──────────────────────────────────────────────────────────────────────

def email_dispatcher_node(state: AlertState) -> dict[str, Any]:
    alert_id = state.get("thread_id", "")
    draft = state.get("alert_draft", {})

    if state.get("approval_status") != "approved":
        logger.info("Alert %s not approved — skipping dispatch.", alert_id)
        return {"dispatched_email": False}

    if not draft:
        return {"dispatched_email": False, "dispatch_error": "No alert draft found."}

    # ── Build recipient lists ─────────────────────────────────────────────────
    internal_emails = _get_internal_emails()
    client_emails = _get_client_emails(draft.get("practice_areas_tagged", []))

    subject = _build_subject(draft)
    html_body = _build_html(draft, state.get("source_url", ""))

    errors = []

    # ── Send to internal team ─────────────────────────────────────────────────
    if internal_emails:
        err = _send_email(
            recipients=internal_emails,
            subject=subject,
            html_body=html_body,
            alert_id=alert_id,
            audience="internal",
        )
        if err:
            errors.append(f"Internal: {err}")

    # ── Send to matching clients ──────────────────────────────────────────────
    for client in client_emails:
        err = _send_email(
            recipients=[client["email"]],
            subject=subject,
            html_body=html_body,
            alert_id=alert_id,
            audience=f"client:{client['name']}",
        )
        if err:
            errors.append(f"Client {client['name']}: {err}")

    if errors:
        error_msg = "; ".join(errors)
        db.update_alert(alert_id, dispatch_error=error_msg)
        db.log_action(alert_id, "dispatch_failed", details={"errors": errors})
        logger.error("Dispatch errors for %s: %s", alert_id, error_msg)
        return {"dispatched_email": False, "dispatch_error": error_msg}

    db.update_alert(alert_id, dispatched_email=1, dispatch_error="")
    db.log_action(
        alert_id,
        "dispatch_success",
        details={
            "internal_recipients": internal_emails,
            "client_recipients": [c["email"] for c in client_emails],
            "subject": subject,
            "dispatched_at": datetime.utcnow().isoformat(),
        },
    )
    logger.info("Alert %s dispatched successfully.", alert_id)
    return {"dispatched_email": True, "dispatch_error": ""}


# ── Email build helpers ───────────────────────────────────────────────────────

def _build_subject(draft: dict) -> str:
    update_type = draft.get("update_type", "Update")
    headline = draft.get("headline", "")[:80]
    return f"LEGAL ALERT | {update_type} | {headline}"


def _build_html(draft: dict, source_url: str) -> str:
    provisions_html = "".join(
        f"<li>{_esc(p)}</li>" for p in draft.get("key_provisions", [])
    )
    implications_html = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:bold;vertical-align:top;white-space:nowrap'>"
        f"→ {_esc(i.get('type',''))}</td>"
        f"<td style='padding:6px 12px'>{_esc(i.get('implication',''))}</td></tr>"
        for i in draft.get("stakeholder_implications", [])
    )
    tags = " | ".join(draft.get("practice_areas_tagged", []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<style>
  body {{font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1a1a;max-width:700px;margin:0 auto;padding:24px;}}
  h1 {{font-size:18px;color:#1a1a2e;border-bottom:2px solid #c41e3a;padding-bottom:8px;}}
  h2 {{font-size:13px;color:#c41e3a;text-transform:uppercase;letter-spacing:1px;margin-top:24px;margin-bottom:6px;}}
  .meta {{color:#555;font-size:12px;margin-bottom:16px;}}
  .headline {{font-size:16px;font-weight:bold;margin:12px 0;line-height:1.4;}}
  ul {{margin:0;padding-left:20px;}}
  li {{margin-bottom:6px;}}
  table {{border-collapse:collapse;width:100%;}}
  td {{border:none;vertical-align:top;}}
  .recommended {{background:#f5f5f0;border-left:4px solid #c41e3a;padding:12px 16px;margin-top:12px;}}
  .footer {{margin-top:32px;font-size:11px;color:#888;border-top:1px solid #ddd;padding-top:12px;}}
  .tag-row {{margin-top:16px;font-size:12px;color:#444;}}
</style>
</head>
<body>
<h1>LEGAL ALERT — {_esc(draft.get('practice_area',''))}</h1>
<div class="meta">
  <strong>Date:</strong> {_esc(draft.get('date',''))}&nbsp;&nbsp;
  <strong>Source:</strong> <a href="{_esc(source_url)}">{_esc(draft.get('source',''))}</a>&nbsp;&nbsp;
  <strong>Update Type:</strong> {_esc(draft.get('update_type',''))}
</div>

<div class="headline">{_esc(draft.get('headline',''))}</div>

<h2>Summary</h2>
<p>{_esc(draft.get('summary',''))}</p>

<h2>Key Provisions</h2>
<ul>{provisions_html}</ul>

<h2>Stakeholder Implications</h2>
<table>{implications_html}</table>

<h2>Recommended Action</h2>
<div class="recommended">{_esc(draft.get('recommended_action',''))}</div>

<div class="tag-row"><strong>PRACTICE AREAS TAGGED:</strong> {_esc(tags)}</div>

<div class="footer">
  This alert is prepared by the Legal Intelligence System and reviewed by a qualified partner
  before distribution. It is not legal advice. For advice specific to your circumstances,
  please contact your designated partner.
</div>
</body>
</html>"""


def _esc(text: str) -> str:
    """Minimal HTML escaping."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── SMTP send ─────────────────────────────────────────────────────────────────

def _send_email(recipients: list[str], subject: str, html_body: str,
                alert_id: str, audience: str) -> str:
    """Returns an error string on failure, empty string on success."""
    # Re-read .env on every call so credential updates never require a restart.
    from dotenv import load_dotenv
    from pathlib import Path as _Path
    load_dotenv(_Path(__file__).parent.parent.parent / ".env", override=True)

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("ALERT_FROM_EMAIL", username)

    if not username or not password:
        msg = "SMTP credentials not configured."
        logger.error(msg)
        return msg

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Legal Intelligence System <{from_email}>"
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(username, password)
            server.sendmail(from_email, recipients, msg.as_string())

        logger.info("Email sent to %s (%s)", recipients, audience)
        db.log_action(
            alert_id,
            "email_sent",
            details={"recipients": recipients, "audience": audience, "subject": subject},
        )
        return ""
    except Exception as exc:
        logger.error("SMTP error (%s): %s", audience, exc)
        return str(exc)


# ── Recipient helpers ─────────────────────────────────────────────────────────

def _get_internal_emails() -> list[str]:
    raw = os.getenv("INTERNAL_TEAM_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _get_client_emails(practice_areas: list[str]) -> list[dict]:
    """Return clients whose practice_areas intersect with the alert's tags."""
    if not _CLIENTS_PATH.exists():
        return []
    try:
        clients = json.loads(_CLIENTS_PATH.read_text(encoding="utf-8"))
        pa_set = {p.lower() for p in practice_areas}
        matched = []
        for c in clients:
            if not c.get("active", True):
                continue
            client_pa = {p.lower() for p in c.get("practice_areas", [])}
            if pa_set & client_pa:
                matched.append({"name": c["name"], "email": c["email"]})
        return matched
    except Exception as exc:
        logger.error("Failed to load clients.json: %s", exc)
        return []
