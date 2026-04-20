"""
web_scraper_node — deep content fetcher with PDF extraction.

For each source URL the scraper:
  1. Fetches the page HTML.
  2. Scans ALL links on the page and scores them against a Kenya-specific
     regulatory keyword list.
  3. Follows high-scoring links (including PDF links) up to 2 hops deep.
  4. Extracts text from HTML pages and PDFs alike.
  5. Extracts publication date and document title from whatever it finds.

PDF extraction uses pdfplumber (falls back to pypdf if needed).
"""

import io
import logging
import re
from datetime import date as _date
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from backend.state import AlertState

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LegalAlertBot/1.0; Kenyan Law Firm Regulatory Monitor)"
    )
}
_TIMEOUT = 25
_MAX_CHARS = 18_000
_MAX_FOLLOWED_LINKS = 15   # per source page

# ── Kenya-specific regulatory keywords ────────────────────────────────────────
# Any link whose anchor text or URL contains one of these is followed first.

KENYA_REGULATORY_KEYWORDS = [
    # Specific missing documents
    "copyright bill", "copyright and related rights",
    "artificial intelligence bill", "ai bill", "ai policy",
    "vasp", "virtual asset service provider", "virtual assets",
    "digital currency", "cbdc", "digital payment",
    "gambling control", "betting control", "gaming regulations",
    "financial consumer protection", "consumer protection framework",
    "dcp", "digital credit provider",
    # General regulatory terms
    "bill", "act", "gazette", "legal notice", "statutory instrument",
    "regulation", "draft regulation", "draft framework",
    "circular", "directive", "guideline", "policy framework",
    "consultation", "public comment", "stakeholder",
    "press release", "media release",
    "judgment", "ruling", "court of appeal", "high court", "supreme court",
    "cbk", "cma", "ca kenya", "epra", "competition authority",
    "parliament", "senate", "national assembly",
    "fintech", "mobile money", "payment system",
    "data protection", "privacy", "cybersecurity",
    "anti-money laundering", "aml", "cft",
    "insurance", "pension", "retirement",
    "employment", "labour", "tax", "vat", "income tax",
    "environment", "climate", "energy", "petroleum",
    "mining", "land", "housing", "health",
]

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Node entry point ──────────────────────────────────────────────────────────

def web_scraper_node(state: AlertState) -> dict[str, Any]:
    url = state["source_url"]
    source_name = state.get("source_name", url)

    # Fast path: content already pre-loaded (backfill/process-URL)
    if state.get("raw_content", "").strip():
        raw = _clean(state["raw_content"])[:_MAX_CHARS]
        pub_date = state.get("publication_date") or _find_any_date(raw[:3000])
        doc_title = state.get("document_title") or _extract_title_from_text(raw)
        if not pub_date:
            pub_date = _date.today().isoformat()
            date_confirmed = False
        else:
            date_confirmed = True
        return {"raw_content": raw, "publication_date": pub_date, "document_title": doc_title, "date_confirmed": date_confirmed}

    logger.info("Scraping: %s", url)
    all_text, pub_date, doc_title = _scrape_with_follow(url)

    if not all_text.strip():
        logger.error("No content retrieved for %s", url)
        return {
            "raw_content": "", "publication_date": "", "document_title": "",
            "is_relevant": False,
            "relevance_reason": "Scraping failed — no content retrieved.",
            "date_confirmed": False,
        }

    if not pub_date:
        pub_date = _date.today().isoformat()
        date_confirmed = False
    else:
        date_confirmed = True
    logger.info(
        "Scraped %d chars from %s | pub_date=%s | title=%s",
        len(all_text), source_name, pub_date or "?", doc_title or "?",
    )
    return {
        "raw_content": all_text[:_MAX_CHARS],
        "publication_date": pub_date,
        "document_title": doc_title,
        "date_confirmed": date_confirmed,
    }


# ── Core scraper ──────────────────────────────────────────────────────────────

def _scrape_with_follow(url: str) -> tuple[str, str, str]:
    """
    Fetch `url`, then follow and extract from all high-value links found.
    Returns (combined_text, publication_date, document_title).
    """
    # Direct PDF URL — extract text immediately without HTML parsing
    if _is_pdf(url):
        logger.info("  Seed URL is a PDF — extracting directly: %s", url)
        pdf_text = _extract_pdf(url)
        if pdf_text:
            pub_date = _find_any_date(pdf_text[:3000])
            title = _extract_title_from_text(pdf_text)
            return _clean(pdf_text)[:_MAX_CHARS], pub_date, title
        logger.warning("  PDF extraction returned no text for %s", url)
        return "", "", ""

    base_domain = urlparse(url).netloc
    html, soup = _fetch_html(url)

    if not html or not soup:
        return "", "", ""

    # Extract text from the seed page itself
    seed_text = _html_to_text(soup)

    # Collect and score all links on the page
    links = _collect_links(soup, url, base_domain)
    scored = _score_links(links)

    collected_texts = [seed_text] if seed_text else []
    best_pub_date = _extract_publication_date(seed_text, soup, url)
    best_title = _extract_document_title(soup, seed_text)

    followed = 0
    for link_url, link_text, score in scored:
        if followed >= _MAX_FOLLOWED_LINKS:
            break

        # Only follow links with at least some keyword relevance
        if score == 0 and not _is_pdf(link_url):
            continue

        logger.debug("  Following [score=%d]: %s", score, link_url)

        if _is_pdf(link_url):
            pdf_text = _extract_pdf(link_url)
            if pdf_text:
                collected_texts.append(f"\n\n--- PDF: {link_url} ---\n{pdf_text}")
                if not best_pub_date:
                    best_pub_date = _find_any_date(pdf_text[:2000])
                if not best_title or best_title == _extract_title_from_text(seed_text):
                    best_title = _extract_title_from_text(pdf_text) or link_text
        else:
            page_html, page_soup = _fetch_html(link_url)
            if page_soup:
                page_text = _html_to_text(page_soup)
                if page_text:
                    collected_texts.append(f"\n\n--- {link_url} ---\n{page_text}")
                    # If this linked page looks more like a document, update date/title
                    page_date = _extract_publication_date(page_text, page_soup, link_url)
                    if page_date and not best_pub_date:
                        best_pub_date = page_date
                    page_title = _extract_document_title(page_soup, page_text)
                    if page_title and score > 5:
                        best_title = page_title

                # Also look for PDFs linked from this subpage
                sub_links = _collect_links(page_soup, link_url, base_domain)
                for sub_url, sub_text, sub_score in _score_links(sub_links):
                    if followed >= _MAX_FOLLOWED_LINKS:
                        break
                    if _is_pdf(sub_url) and sub_score > 0:
                        pdf_text = _extract_pdf(sub_url)
                        if pdf_text:
                            collected_texts.append(
                                f"\n\n--- PDF: {sub_url} ---\n{pdf_text}"
                            )
                            followed += 1

        followed += 1

    combined = "\n\n".join(collected_texts)
    return _clean(combined)[:_MAX_CHARS], best_pub_date, best_title


# ── Link collection & scoring ─────────────────────────────────────────────────

def _collect_links(soup: BeautifulSoup, base_url: str,
                   base_domain: str) -> list[tuple[str, str]]:
    """Return list of (absolute_url, anchor_text) for all usable links."""
    seen = set()
    links = []
    skip_ext = re.compile(
        r"\.(png|jpg|jpeg|gif|svg|ico|css|js|woff|ttf|mp4|avi|zip)$", re.IGNORECASE
    )
    skip_terms = ["login", "logout", "register", "cart", "sitemap",
                  "privacy", "terms", "cookie", "rss", "feed", "mailto:",
                  "javascript:", "tel:", "#", "gallery", "photo"]

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        full_url, _ = urldefrag(urljoin(base_url, href))
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue
        # Allow same-domain links AND PDFs from any domain
        if parsed.netloc != base_domain and not _is_pdf(full_url):
            continue
        if full_url in seen:
            continue
        if any(t in full_url.lower() for t in skip_terms):
            continue
        if skip_ext.search(parsed.path) and not _is_pdf(full_url):
            continue

        anchor = tag.get_text(strip=True)[:200]
        seen.add(full_url)
        links.append((full_url, anchor))

    return links


def _score_links(links: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    """Score and sort links by keyword relevance. Returns (url, anchor, score)."""
    scored = []
    for url, anchor in links:
        text = (url + " " + anchor).lower()
        score = sum(10 for kw in KENYA_REGULATORY_KEYWORDS if kw in text)
        # Extra points for PDFs — they are usually the actual documents
        if _is_pdf(url):
            score += 15
        scored.append((url, anchor, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


# ── PDF extraction ────────────────────────────────────────────────────────────

def _extract_pdf(url: str) -> str:
    """Download a PDF and extract its text. Returns empty string on failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, verify=False)
        resp.raise_for_status()

        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages[:20]]
                text = "\n\n".join(pages)
                logger.info("  PDF extracted: %d chars from %s", len(text), url)
                return _clean(text)[:_MAX_CHARS]
        except ImportError:
            pass

        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(resp.content))
            pages = [reader.pages[i].extract_text() or "" for i in range(min(20, len(reader.pages)))]
            text = "\n\n".join(pages)
            logger.info("  PDF extracted (pypdf): %d chars from %s", len(text), url)
            return _clean(text)[:_MAX_CHARS]
        except ImportError:
            logger.warning("No PDF library available. Install pdfplumber: pip install pdfplumber")
            return ""

    except Exception as exc:
        logger.debug("PDF extraction failed for %s: %s", url, exc)
        return ""


# ── HTML fetch & parse ────────────────────────────────────────────────────────

def _fetch_html(url: str) -> tuple[str | None, BeautifulSoup | None]:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, verify=False)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and not _is_pdf(url):
            return None, None
        soup = BeautifulSoup(resp.text, "lxml")
        return resp.text, soup
    except Exception as exc:
        logger.debug("Fetch error %s: %s", url, exc)
        return None, None


def _html_to_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


# ── Publication date extraction ───────────────────────────────────────────────

def _extract_publication_date(text: str, soup: BeautifulSoup, url: str) -> str:
    """Source-aware date extractor. Returns YYYY-MM-DD or ''."""
    # HTML meta tags
    if soup:
        for meta in soup.find_all("meta", attrs={"property": True}):
            if "published_time" in meta.get("property", ""):
                d = _parse_iso(meta.get("content", ""))
                if d:
                    return d
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            d = _parse_iso(time_tag["datetime"])
            if d:
                return d

    # Source-specific patterns
    patterns = []

    if "gazette" in url.lower() or "kenyalaw" in url:
        patterns += [
            r"NAIROBI[,\s]+(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+(\d{4})",
            r"Gazette\s+Notice[^\n]*?(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+(\d{4})",
        ]
    if "parliament" in url:
        patterns += [
            r"(?:First Reading|Date Tabled|Introduced)[:\s]+(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+(\d{4})",
        ]
    # General patterns
    patterns += [
        r"(?:dated|published|issued|released|gazetted)[:\s]+(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+(\d{4})",
        r"(?:dated|published|issued|released|gazetted)[:\s]+(20\d\d-\d{2}-\d{2})",
    ]

    for pat in patterns:
        m = re.search(pat, text[:5000], re.IGNORECASE)
        if m:
            groups = m.groups()
            if len(groups) == 3:
                try:
                    month = _MONTH_MAP.get(groups[1].lower())
                    if month:
                        return f"{groups[2]}-{month:02d}-{int(groups[0]):02d}"
                except Exception:
                    pass
            elif len(groups) == 1:
                d = _parse_iso(groups[0])
                if d:
                    return d

    return _find_any_date(text[:3000])


def _find_any_date(text: str) -> str:
    """Find any 2025/2026 date in text. Returns YYYY-MM-DD or ''."""
    m = re.search(r"\b(202[5-9])-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|"
        r"Jun|Jul|Aug|Sep|Oct|Nov|Dec)[,\s]+(202[5-9])\b",
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTH_MAP.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{1,2})[,\s]+(202[5-9])\b",
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
    return ""


def _parse_iso(s: str) -> str:
    m = re.search(r"(202[0-9]-\d{2}-\d{2})", s or "")
    return m.group(1) if m else ""


# ── Title extraction ──────────────────────────────────────────────────────────

def _extract_document_title(soup: BeautifulSoup, text: str) -> str:
    if soup:
        h1 = soup.find("h1")
        if h1 and len(h1.get_text(strip=True)) > 5:
            return h1.get_text(strip=True)[:200]
        title_tag = soup.find("title")
        if title_tag:
            t = re.sub(
                r"\s*[\|\-–—]\s*(Kenya|Parliament|CMA|CBK|EPRA|CA|Judiciary|BCLB|KEPI).*$",
                "", title_tag.get_text(strip=True), flags=re.IGNORECASE,
            ).strip()
            if len(t) > 5:
                return t[:200]
    return _extract_title_from_text(text)


def _extract_title_from_text(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 10:
            return line[:200]
    return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_pdf(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
