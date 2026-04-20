"""
deep_crawler.py — multi-level site crawler for legal/regulatory sources.

Strategy:
  - BFS crawl from a seed URL up to MAX_DEPTH levels.
  - At each level, extract internal links and score them.
  - High-score links (bills, acts, gazette, ruling, circular, etc.) are
    prioritised and followed first.
  - Only pages that look like actual documents/articles (not listing pages)
    are returned as candidates for LLM processing.
  - Visited URLs are tracked per-crawl to prevent loops.
  - Robots.txt is respected where possible.
  - Rate-limited with a small delay between requests.

Typical depth needed per source:
  Parliament  → /bills → /bills/2026 → /bills/2026/finance-bill-2026   (depth 3)
  Kenya Law   → /legislation → /acts → /acts/finance-act-2026          (depth 3)
  CBK         → / → /press-releases → /press-releases/2026/march       (depth 2)
  CMA         → / → /publications → specific circular                   (depth 2)
"""

import logging
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
MAX_DEPTH = 3           # levels below the seed URL
MAX_PAGES = 80          # hard cap per source to avoid runaway crawls
REQUEST_DELAY = 0.8     # seconds between requests (be polite)
TIMEOUT = 20
MAX_CONTENT_CHARS = 15_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LegalAlertBot/1.0; "
        "Kenyan Law Firm Regulatory Monitor)"
    )
}

# ── Scoring keywords ──────────────────────────────────────────────────────────
# Higher score = higher priority in the crawl queue

_HIGH_VALUE_PATH_TERMS = [
    "bill", "act", "gazette", "regulation", "statutory", "instrument",
    "legal-notice", "supplement", "ruling", "judgment", "circular",
    "press-release", "press_release", "publication", "notice",
    "policy", "directive", "guideline", "consultation", "draft",
    "2026", "2025",
]

_CONTENT_PAGE_SIGNALS = [
    # URL path signals
    r"/\d{4}/",           # year in path
    r"bill[-_]",
    r"act[-_]",
    r"gazette[-_]?no",
    r"ruling[-_]",
    r"judgment",
    r"circular[-_]?no",
    r"legal[-_]notice",
    r"press[-_]release",
    r"\d{4}-\d{2}-\d{2}",  # date in URL
]

_SKIP_PATH_TERMS = [
    "login", "logout", "register", "signup", "contact", "about-us",
    "sitemap", "privacy", "terms", "cookie", "careers", "tender",
    "vacancy", "jobs", "advertis", "subscribe", "rss", "feed",
    "gallery", "media-gallery", "photo", "video", "event", "calendar",
    "search", "tag", "category", "author", "page=", "print=",
    "download=", "mailto:", "javascript:", "tel:",
]


# ── Public entry point ────────────────────────────────────────────────────────

def crawl_source(seed_url: str, max_depth: int = MAX_DEPTH,
                 max_pages: int = MAX_PAGES) -> list[dict]:
    """
    Crawl a source starting from seed_url.
    Returns a list of dicts: {"url": str, "content": str, "depth": int}
    for pages that look like actual documents (not listing pages).
    """
    base_domain = urlparse(seed_url).netloc
    robots = _load_robots(seed_url)

    visited: set[str] = set()
    results: list[dict] = []

    # Queue entries: (url, depth)
    queue: deque[tuple[str, int]] = deque()
    queue.append((seed_url, 0))
    visited.add(_normalise(seed_url))

    pages_fetched = 0

    while queue and pages_fetched < max_pages:
        url, depth = queue.popleft()

        if not _allowed_by_robots(robots, url):
            logger.debug("Robots.txt disallows: %s", url)
            continue

        logger.info("Crawling [depth=%d] %s", depth, url)
        html, soup = _fetch(url)
        if not html or not soup:
            continue

        pages_fetched += 1
        time.sleep(REQUEST_DELAY)

        # Decide if this page is a document or a listing page
        page_type = _classify_page(url, soup, html)

        if page_type == "document":
            content = _extract_text(soup)
            if content:
                results.append({
                    "url": url,
                    "content": content,
                    "depth": depth,
                    "title": _extract_title(soup),
                })
                logger.info("  → Document found: %s", url)

        # Always follow links if we haven't hit max depth
        if depth < max_depth:
            links = _extract_links(soup, url, base_domain)
            scored = _score_and_sort(links, depth)

            for link_url, _score in scored:
                norm = _normalise(link_url)
                if norm not in visited:
                    visited.add(norm)
                    queue.append((link_url, depth + 1))

    logger.info(
        "Crawl complete for %s: %d pages fetched, %d documents found.",
        seed_url, pages_fetched, len(results),
    )
    return results


# ── Page classification ───────────────────────────────────────────────────────

def _classify_page(url: str, soup: BeautifulSoup, html: str) -> str:
    """
    Returns "document" or "listing".

    A document page has:
      - Substantial body text (>400 chars after stripping boilerplate)
      - Date-like strings in the content
      - Legal terminology in the heading or body
      - OR a URL that pattern-matches a document
    """
    path = urlparse(url).path.lower()

    # URL is an obvious document
    for pattern in _CONTENT_PAGE_SIGNALS:
        if re.search(pattern, path):
            text = _extract_text(soup)
            if len(text) > 300:
                return "document"

    # Check text length and legal signal density
    text = _extract_text(soup)
    if len(text) < 300:
        return "listing"

    legal_terms = [
        "whereas", "hereinafter", "pursuant", "gazette", "commencement",
        "enacted", "repeal", "amend", "regulation", "statutory",
        "parliament", "gazetted", "tabled", "judgment", "ruling",
        "circular", "directive", "section", "clause", "subsection",
    ]
    term_hits = sum(1 for t in legal_terms if t in text.lower())

    # Has a date and legal language = likely a document
    has_date = bool(re.search(r"\b(20[12]\d)\b", text))
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True).lower() if h1 else ""
    h1_legal = any(t in h1_text for t in legal_terms + ["bill", "act", "notice"])

    if term_hits >= 3 and has_date:
        return "document"
    if h1_legal and len(text) > 500:
        return "document"

    return "listing"


# ── Link extraction & scoring ─────────────────────────────────────────────────

def _extract_links(soup: BeautifulSoup, base_url: str,
                   base_domain: str) -> list[str]:
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        full_url, _ = urldefrag(urljoin(base_url, href))
        parsed = urlparse(full_url)

        if parsed.netloc != base_domain:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        path_lower = parsed.path.lower() + parsed.query.lower()
        if any(skip in path_lower for skip in _SKIP_PATH_TERMS):
            continue
        # Skip non-HTML resources
        if re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip|png|jpg|gif|svg|css|js)$",
                     parsed.path, re.IGNORECASE):
            # PDFs are handled separately; skip for now
            continue
        links.append(full_url)

    return list(dict.fromkeys(links))  # deduplicate preserving order


def _score_and_sort(links: list[str], current_depth: int) -> list[tuple[str, int]]:
    """Return links sorted by descending relevance score."""
    scored = []
    for url in links:
        path = urlparse(url).path.lower()
        score = 0
        for term in _HIGH_VALUE_PATH_TERMS:
            if term in path:
                score += 10
        # Prefer shorter paths at shallow depths (more likely to be section pages)
        path_depth = path.count("/")
        if current_depth == 0:
            score += max(0, 5 - path_depth)
        scored.append((url, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _fetch(url: str) -> tuple[str | None, BeautifulSoup | None]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type:
            return None, None
        soup = BeautifulSoup(resp.text, "lxml")
        return resp.text, soup
    except Exception as exc:
        logger.debug("Fetch error %s: %s", url, exc)
        return None, None


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "footer",
                     "header", "aside", "form", "button"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:MAX_CONTENT_CHARS]


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:200]
    title = soup.find("title")
    if title:
        t = re.sub(r"\s*[\|\-–—]\s*\w.*$", "", title.get_text(strip=True))
        return t.strip()[:200]
    return ""


# ── Robots.txt ────────────────────────────────────────────────────────────────

def _load_robots(seed_url: str) -> RobotFileParser | None:
    parsed = urlparse(seed_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp
    except Exception:
        return None


def _allowed_by_robots(rp: RobotFileParser | None, url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise(url: str) -> str:
    """Normalise URL for deduplication (strip trailing slash, lowercase scheme/host)."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
