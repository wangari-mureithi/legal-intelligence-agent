"""
APScheduler-based scheduler.

Reads sources from config/sources.json and fires one graph run per enabled
source on the configured interval (default: every 6 hours).

Run this as a standalone process:
    python -m backend.scheduler

Or import start_scheduler() and call it from your main entry point.
"""

import json
import logging
import os
import uuid
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from backend.config import sources_path, data_path
from backend import database as db

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


# ── Core scrape job ───────────────────────────────────────────────────────────

def run_scrape_job() -> None:
    """
    Reads all enabled sources and launches a graph run for each one.
    Each run proceeds until the interrupt() in human_review_node, at which
    point the graph pauses and the alert row is visible in the Streamlit UI.
    """
    # Import graph here (not at module level) to avoid circular imports
    # and to allow the scheduler to start before the graph is compiled.
    from backend.graph import graph

    sources_file = sources_path()
    if not sources_file.exists():
        logger.error("sources.json not found at %s", sources_file)
        return

    sources = json.loads(sources_file.read_text(encoding="utf-8"))
    enabled = [s for s in sources if s.get("enabled", True)]
    logger.info("Starting scrape run: %d sources.", len(enabled))

    for source in enabled:
        _process_source(graph, source)


def _process_source(graph, source: dict) -> None:
    url = source["url"]
    name = source["name"]
    category = source.get("category", "")

    logger.info("Deep crawling source: %s (%s)", name, url)

    # ── Step 1: Deep crawl to find all document pages ─────────────────────────
    try:
        from backend.deep_crawler import crawl_source
        documents = crawl_source(seed_url=url)
    except Exception as exc:
        logger.error("Deep crawl failed for %s: %s", name, exc)
        db.log_action(
            alert_id="scheduler",
            action="crawl_error",
            details={"source": name, "url": url, "error": str(exc)},
        )
        return

    if not documents:
        logger.info("No document pages found for %s", name)
        return

    logger.info("Found %d document page(s) for %s", len(documents), name)

    # ── First-run detection ───────────────────────────────────────────────────
    # On the very first scrape of a source, limit to 5 documents to avoid
    # flooding the review queue with historical content.
    tracking = db.get_source_tracking(url)
    is_first_run = tracking is None
    if is_first_run:
        documents = documents[:5]
        logger.info(
            "First run for %s — limiting to 5 documents to avoid backlog.", name
        )

    max_pub_date_seen = ""
    alerts_queued = 0

    # ── Step 2: Launch one graph run per discovered document ──────────────────
    for doc in documents:
        doc_url = doc["url"]
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial_state = {
            "source_url": doc_url,
            "source_name": name,
            "source_category": category,
            "source_base_url": url,   # base URL from sources.json for tracking
            "thread_id": thread_id,
            # Pre-load content so web_scraper_node skips the network fetch
            "raw_content": doc["content"],
            "document_title": doc.get("title", ""),
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

        try:
            for _event in graph.stream(
                initial_state, config=config, stream_mode="values"
            ):
                pass

            state_snapshot = graph.get_state(config)
            if state_snapshot.next:
                # Graph is paused at human_review — save to DB NOW
                _save_paused_alert(thread_id, state_snapshot.values)
                pub_date = state_snapshot.values.get("publication_date", "")
                if pub_date and pub_date > max_pub_date_seen:
                    max_pub_date_seen = pub_date
                alerts_queued += 1
                logger.info("Queued for review: %s", doc_url)
            else:
                logger.info("Not relevant or duplicate: %s", doc_url)

        except Exception as exc:
            logger.error("Graph error for %s: %s", doc_url, exc)
            db.log_action(
                alert_id=thread_id,
                action="graph_error",
                details={"source": name, "url": doc_url, "error": str(exc)},
            )

    # ── Update source tracking after processing this source ───────────────────
    db.update_source_tracking(
        source_url=url,
        last_seen_date=max_pub_date_seen,
        alerts_delta=alerts_queued,
    )
    logger.info(
        "Source tracking updated for %s: last_seen=%s, alerts=%d",
        name, max_pub_date_seen or "none", alerts_queued,
    )


# ── DB save helper ────────────────────────────────────────────────────────────

def _save_paused_alert(thread_id: str, state: dict) -> None:
    """
    Called after graph.stream() returns with the graph paused at human_review.
    Writes the alert to the application DB so the Streamlit UI can display it.
    """
    alert_draft = state.get("alert_draft", {})
    source_url  = state.get("source_url", "")
    source_name = state.get("source_name", "")

    existing = db.get_alert(thread_id)
    if not existing:
        db.save_alert(
            alert_id=thread_id,
            thread_id=thread_id,
            source_url=source_url,
            source_name=source_name,
            source_category=state.get("source_category", ""),
            document_title=state.get("document_title", ""),
            publication_date=state.get("publication_date", ""),
        )

    db.update_alert(
        thread_id,
        alert_draft=alert_draft,
        raw_content=state.get("raw_content", "")[:500],   # store snippet only
        is_relevant=1,
        approval_status="pending",
        document_title=state.get("document_title", ""),
        publication_date=state.get("publication_date", ""),
        geography_scope=state.get("geography_scope", "kenya"),
        kenya_nexus=state.get("kenya_nexus", ""),
    )

    db.log_action(
        alert_id=thread_id,
        action="alert_drafted",
        actor="system",
        details={
            "source": source_name,
            "url": source_url,
            "headline": alert_draft.get("headline", ""),
            "update_type": alert_draft.get("update_type", ""),
            "publication_date": state.get("publication_date", ""),
        },
    )
    logger.info("Alert %s saved to DB — pending review.", thread_id)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler(interval_hours: int = 6) -> BackgroundScheduler:
    """Start the background scheduler and return it."""
    global _scheduler

    db.init_db()

    _scheduler = BackgroundScheduler(timezone="Africa/Nairobi")
    _scheduler.add_job(
        func=run_scrape_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="scrape_job",
        name="Legal Source Scraper",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    _scheduler.start()
    logger.info("Scheduler started — interval: every %d hour(s).", interval_hours)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def trigger_now() -> None:
    """Run the scrape job immediately (used from the Streamlit Settings page)."""
    logger.info("Manual scrape triggered.")
    run_scrape_job()


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    interval = int(os.getenv("SCRAPE_INTERVAL_HOURS", "6"))
    sched = start_scheduler(interval_hours=interval)

    # Run once immediately on startup
    logger.info("Running initial scrape on startup …")
    run_scrape_job()

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        stop_scheduler()
