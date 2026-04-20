# Legal Intelligence Agent

An AI-powered regulatory monitoring system for Kenyan law firms.  
Monitors legal sources → drafts structured legal alerts → routes through human approval → dispatches via email.

---

## Architecture

```
Scheduler (APScheduler)
    └── LangGraph Agent
          ├── web_scraper_node      — fetches source page
          ├── relevance_filter_node — LLM: is this a genuine regulatory update?
          ├── summarizer_node       — LLM: generate structured legal alert
          ├── human_review_node     — INTERRUPT: waits for partner approval (Streamlit UI)
          ├── email_dispatcher_node — sends HTML email to team + tagged clients
          └── audit_log_node        — records final outcome

Streamlit UI (4 pages)
    ├── Dashboard     — all alerts, filters, detail view
    ├── Alert Review  — editable draft, approve / reject / re-draft
    ├── Settings      — sources, email list, clients, schedule
    └── Audit Log     — append-only action history
```

---

## Prerequisites

- Python 3.11+
- A [Groq API key](https://console.groq.com) (free tier)
- A Gmail account with [App Password](https://myaccount.google.com/apppasswords) enabled

---

## Setup

### 1. Clone / download

```bash
cd "Autonomous Agent"
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Your Groq API key |
| `SMTP_USERNAME` | Gmail address |
| `SMTP_PASSWORD` | Gmail App Password (16 chars, no spaces) |
| `ALERT_FROM_EMAIL` | Sender address (usually same as SMTP_USERNAME) |
| `INTERNAL_TEAM_EMAILS` | Comma-separated list of team addresses |
| `SCRAPE_INTERVAL_HOURS` | How often to scrape (default: 6) |

### 5. (Optional) Customise clients

Edit `config/clients.json` to add your real client list with their practice areas.

---

## Running the Application

You need **two terminal windows** — one for the scheduler, one for the UI.

### Terminal 1 — Start the Scheduler

```bash
python -m backend.scheduler
```

This scrapes all enabled sources immediately on startup, then repeats every N hours.  
Pending alerts appear in the Streamlit UI automatically.

### Terminal 2 — Start the Streamlit UI

```bash
streamlit run frontend/app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Workflow

1. **Scheduler** fires → scrapes each source → LLM filters for relevance
2. If relevant → LLM drafts a structured legal alert → graph **pauses**
3. **Dashboard** shows the pending alert with a yellow "PENDING" badge
4. Go to **Alert Review** → edit any field → click **Approve & Dispatch**
5. Graph resumes → emails sent to internal team + matched clients
6. **Audit Log** records every step with timestamps and recipient lists

---

## Project Structure

```
├── backend/
│   ├── config.py           — LLM + checkpointer singletons
│   ├── database.py         — SQLite application store
│   ├── graph.py            — LangGraph definition
│   ├── scheduler.py        — APScheduler job runner
│   ├── state.py            — AlertState TypedDict
│   └── nodes/
│       ├── web_scraper.py
│       ├── relevance_filter.py
│       ├── summarizer.py
│       ├── human_review.py
│       └── email_dispatcher.py
├── frontend/
│   ├── app.py              — Streamlit entry point
│   └── pages/
│       ├── dashboard.py
│       ├── alert_review.py
│       ├── settings.py
│       └── audit_log.py
├── config/
│   ├── sources.json        — monitored sources
│   └── clients.json        — client email list
├── data/                   — auto-created; SQLite databases live here
├── .env.example
├── requirements.txt
└── README.md
```

---

## Adding New Sources

In the **Settings → Monitored Sources** tab, click "Add New Source" and enter the URL.  
Alternatively, edit `config/sources.json` directly.

---

## Email Format

Subject: `LEGAL ALERT | [Update Type] | [Headline]`

Body: clean HTML rendering of the full structured alert (practice area, summary, key provisions, stakeholder implications, recommended action).

---

## LLM Model

Uses **llama-3.3-70b-versatile** via Groq API (free tier).  
To change the model, edit `backend/config.py` → `get_llm()`.

---

## Known Limitations

- Some sources (Parliament, Judiciary) may require session cookies or JavaScript rendering. The scraper uses a two-stage approach (WebBaseLoader → requests+BS4); pages that require JS will return limited content.
- The Groq free tier has rate limits. If you see `429` errors, reduce `SCRAPE_INTERVAL_HOURS` or reduce the number of enabled sources.
- Microsoft Teams integration is not yet implemented (planned for next phase).
