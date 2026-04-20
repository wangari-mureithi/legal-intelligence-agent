"""
Backfill scraper — finds article/update links on each source's listing page
and runs them through the full LangGraph pipeline.

Changes from v1:
- Date filtering is done by the LLM relevance filter (not regex), which handles
  inconsistent date formats on Kenyan legal sites far more reliably.
- The scraper collects ALL internal links that look like content pages and
  passes them to the graph. The LLM decides what is relevant and recent.
- Progress is reported step by step so the caller can display it.

Run from terminal:
    python -m backend.backfill --from 2026-01-01
"""

import argparse
import logging
import re
import uuid
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from backend import database as db

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LegalAlertBot/1.0)"
}
_TIMEOUT = 20
_MAX_LINKS_PER_SOURCE = 40
_MAX_CONTENT_CHARS = 15_000

# URL path fragments that suggest a content page (not nav/admin/login)
_SKIP_PATH_FRAGMENTS = [
    "login", "logout", "register", "signup", "contact", "about",
    "sitemap", "privacy", "terms", "cookie", "careers", "tender",
    "vacancy", "jobs", "advertis", "subscribe", "rss", "feed",
    "javascript", "mailto", "tel:", "#",
]


# ── Public entry point ────────────────────────────────────────────────────────

def run_backfill(
    start_date: date,
    sources: list[dict] | None = None,
    progress_callback=None,          # optional fn(source_name, queued, total)
) -> int:
    """
    Scan all enabled sources for content since start_date.
    Returns total number of articles queued for human review.
    """
    from backend.graph import graph
    from backend.config import sources_path
    import json

    if sources is None:
        path = sources_path()
        all_sources = json.loads(path.read_text(encoding="utf-8"))
        sources = [s for s in all_sources if s.get("enabled", True)]

    total_queued = 0
    for i, source in enumerate(sources):
        queued = _backfill_source(graph, source, start_date)
        total_queued += queued
        logger.info("Source %s: %d article(s) queued.", source["name"], queued)
        if progress_callback:
            progress_callback(
                source_name=source["name"],
                source_index=i + 1,
                source_total=len(sources),
                queued=queued,
                total_queued=total_queued,
            )

    logger.info("Backfill complete. Total queued: %d", total_queued)
    return total_queued


# ── Per-source backfill ───────────────────────────────────────────────────────

def _backfill_source(graph, source: dict, start_date: date) -> int:
    base_url = source["url"]
    name = source["name"]
    category = source.get("category", "")

    logger.info("Deep crawling for backfill: %s …", name)

    from backend.deep_crawler import crawl_source
    try:
        documents = crawl_source(seed_url=base_url)
    except Exception as exc:
        logger.error("Deep crawl failed for %s: %s", name, exc)
        return 0

    if not documents:
        logger.warning("No document pages found for %s", name)
        return 0

    logger.info("Found %d document page(s) for %s", len(documents), name)

    queued = 0
    for doc in documents:
        doc_url = doc["url"]
        content = doc["content"]

        # Quick pre-check: page must mention 2025 or 2026
        if not re.search(r"\b202[56]\b", content):
            logger.debug("Skipping %s — no 2025/2026 reference.", doc_url)
            continue

        try:
            thread_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": thread_id}}

            initial_state = {
                "source_url": doc_url,
                "source_name": name,
                "source_category": category,
                "source_base_url": base_url,
                "thread_id": thread_id,
                "raw_content": content,
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

            logger.info("Processing: %s", doc_url)

            for _event in graph.stream(
                initial_state, config=config, stream_mode="values"
            ):
                pass

            state_snapshot = graph.get_state(config)
            if state_snapshot.next:
                # Graph paused at human_review — write to DB immediately
                from backend.scheduler import _save_paused_alert
                _save_paused_alert(thread_id, state_snapshot.values)
                logger.info("Queued for review: %s", doc_url)
                queued += 1
            else:
                logger.info("Not relevant or duplicate: %s", doc_url)

        except Exception as exc:
            logger.error("Error processing %s: %s", doc_url, exc)
            db.log_action(
                alert_id="backfill",
                action="graph_error",
                details={"url": doc_url, "source": name, "error": str(exc)},
            )

    return queued


# ── Link extraction ───────────────────────────────────────────────────────────

def _extract_article_links(base_url: str) -> list[str]:
    """
    Fetch the listing page and return internal links that look like content.
    Prioritises links containing year markers, news keywords, or gazette terms.
    """
    try:
        resp = requests.get(
            base_url, headers=_HEADERS, timeout=_TIMEOUT, verify=False
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to fetch listing page %s: %s", base_url, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    base_domain = urlparse(base_url).netloc

    priority = []   # year/keyword links — processed first
    general = []    # all other internal links
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("javascript", "mailto", "tel:", "#")):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc != base_domain:
            continue
        if full_url in seen or full_url == base_url:
            continue

        path = parsed.path.lower()
        if any(skip in path for skip in _SKIP_PATH_FRAGMENTS):
            continue

        seen.add(full_url)

        is_priority = any(kw in path for kw in [
            "2026", "2025", "news", "gazette", "bill", "act",
            "ruling", "judgment", "circular", "notice", "regulation",
            "article", "update", "alert", "publication", "press",
            "statutory", "legal-notice", "supplement",
        ])

        if is_priority:
            priority.append(full_url)
        else:
            general.append(full_url)

    combined = priority + general
    logger.info(
        "Found %d candidate links on %s (%d priority, %d general)",
        len(combined), base_url, len(priority), len(general),
    )
    return combined


# ── Content fetch ─────────────────────────────────────────────────────────────

def _fetch_content(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text[:_MAX_CONTENT_CHARS] or None
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
    db.init_db()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start_date", default="2026-01-01")
    args = parser.parse_args()

    start = date.fromisoformat(args.start_date)
    logger.info("Backfill from %s", start.isoformat())

    def _progress(source_name, source_index, source_total, queued, total_queued):
        print(f"[{source_index}/{source_total}] {source_name} — "
              f"{queued} queued (total: {total_queued})")

    total = run_backfill(start_date=start, progress_callback=_progress)
    print(f"\nDone. {total} article(s) queued for review.")
