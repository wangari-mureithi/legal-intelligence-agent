"""
test_scraper.py — verify the scraper can find the 6 known missing documents.

Run with:
    python test_scraper.py

Each test:
  1. Scrapes the target URL (following links and extracting PDFs).
  2. Checks whether the expected keywords appear in the extracted content.
  3. Checks whether the Kenya keyword auto-flag triggers.
  4. Reports PASS / FAIL with details.

Does NOT call the LLM or write to the database — pure scraper verification.
"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

import warnings
warnings.filterwarnings("ignore")   # suppress SSL InsecureRequestWarning

from backend.nodes.web_scraper import _scrape_with_follow, KENYA_REGULATORY_KEYWORDS
from backend.nodes.relevance_filter import _keyword_auto_flag

# ── Test cases ────────────────────────────────────────────────────────────────

TEST_CASES = [
    {
        "name": "Copyright Bill",
        "url": "https://copyright.go.ke/media-center/news-updates/invitation-public-comments-proposed-copyright-and-related-rights-bill",
        "expected_keywords": ["copyright", "bill", "related rights"],
        "auto_flag_expected": True,
    },
    {
        "name": "AI Bill",
        "url": "https://www.parliament.go.ke/the-national-assembly/bills",
        "expected_keywords": ["artificial intelligence", "technology"],
        "auto_flag_expected": True,
        "note": "May also be at CA Kenya — check both",
    },
    {
        "name": "Draft VASP Regulations",
        "url": "https://www.cma.or.ke/draft-regulations/",
        "expected_keywords": ["virtual asset", "vasp"],
        "auto_flag_expected": True,
    },
    {
        "name": "CBK DCP Press Release",
        "url": "https://www.centralbank.go.ke/press-releases/",
        "expected_keywords": ["digital credit provider", "dcp"],
        "auto_flag_expected": True,
    },
    {
        "name": "Draft Gambling Control Regulations",
        "url": "https://bclb.go.ke/",
        "expected_keywords": ["gambling", "betting", "regulation"],
        "auto_flag_expected": True,
    },
    {
        "name": "Draft Financial Consumer Protection Framework",
        "url": "https://www.centralbank.go.ke/financial-consumer-protection/",
        "expected_keywords": ["consumer protection", "financial consumer"],
        "auto_flag_expected": True,
    },
]

# ── Runner ────────────────────────────────────────────────────────────────────

def run_tests():
    print("\n" + "=" * 70)
    print("  LEGAL AGENT — SCRAPER VERIFICATION TEST")
    print("  Testing 6 known regulatory documents")
    print("=" * 70 + "\n")

    passed = 0
    failed = 0
    results = []

    for i, tc in enumerate(TEST_CASES, 1):
        name = tc["name"]
        url  = tc["url"]
        note = tc.get("note", "")

        print(f"[{i}/6] {name}")
        print(f"      URL: {url}")
        if note:
            print(f"      Note: {note}")

        try:
            content, pub_date, doc_title = _scrape_with_follow(url)

            if not content.strip():
                status = "FAIL"
                reason = "No content retrieved from URL"
                failed += 1
            else:
                # Check expected keywords
                content_lower = content.lower()
                found_kw = [kw for kw in tc["expected_keywords"] if kw in content_lower]
                missing_kw = [kw for kw in tc["expected_keywords"] if kw not in content_lower]

                # Check auto-flag
                auto_flagged, matched_term = _keyword_auto_flag(content)
                flag_ok = auto_flagged == tc.get("auto_flag_expected", True)

                if found_kw and flag_ok:
                    status = "PASS"
                    reason = (
                        f"Keywords found: {found_kw} | "
                        f"Auto-flag: {auto_flagged} (matched: '{matched_term}')"
                    )
                    passed += 1
                elif found_kw and not flag_ok:
                    status = "PARTIAL"
                    reason = (
                        f"Keywords found but auto-flag={'did not trigger' if not auto_flagged else 'triggered unexpectedly'}. "
                        f"Found: {found_kw}"
                    )
                    failed += 1
                else:
                    status = "FAIL"
                    reason = (
                        f"Missing keywords: {missing_kw} | "
                        f"Auto-flag: {auto_flagged}"
                    )
                    failed += 1

            results.append({
                "name": name,
                "status": status,
                "content_len": len(content),
                "pub_date": pub_date,
                "doc_title": doc_title[:80] if doc_title else "",
                "reason": reason,
            })

        except Exception as exc:
            status = "ERROR"
            reason = str(exc)
            failed += 1
            results.append({
                "name": name,
                "status": status,
                "content_len": 0,
                "pub_date": "",
                "doc_title": "",
                "reason": reason,
            })

        r = results[-1]
        icon = {"PASS": "✅", "FAIL": "❌", "PARTIAL": "⚠️", "ERROR": "🚨"}.get(r["status"], "?")
        print(f"      {icon} {r['status']}")
        print(f"      Content: {r['content_len']:,} chars | Date: {r['pub_date'] or 'not found'}")
        print(f"      Title: {r['doc_title'] or 'not found'}")
        print(f"      {r['reason']}")
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"  RESULTS: {passed} passed / {failed} failed / {len(TEST_CASES)} total")
    print("=" * 70)

    if failed > 0:
        print("\nFAILED tests — next steps:")
        for r in results:
            if r["status"] in ("FAIL", "ERROR", "PARTIAL"):
                print(f"  • {r['name']}: {r['reason'][:120]}")
        print(
            "\nIf a document is not found by URL, use Settings → Process URL in the "
            "Streamlit app to paste the direct link to the document."
        )
    else:
        print("\nAll 6 documents found. The scraper is working correctly.")

    return passed, failed


if __name__ == "__main__":
    run_tests()
