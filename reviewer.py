#!/usr/bin/env python3
"""
reviewer.py - Cross-checks displayed entry details against live source pages.

For each entry in directory.html, independently extracts structured facts from
the source URL using Claude Sonnet, then programmatically compares against what
the page currently displays. Outputs discrepancies to review_report.json.

Usage:
    python reviewer.py
    python reviewer.py --dry-run       # Parse directory only, skip API calls
    python reviewer.py --limit 5       # Only check first N entries
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DIRECTORY_FILE = BASE_DIR / "directory.html"
REPORT_FILE = BASE_DIR / "review_report.json"

# Use a different model than the crawler (which uses Haiku) for independent review
REVIEW_MODEL = "claude-sonnet-4-5-20250929"

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fields to compare
FACT_FIELDS = ["stipend", "duration", "location", "deadline", "status", "description"]


def parse_directory() -> list[dict]:
    """
    Parse directory.html and extract displayed info for each entry.
    Returns list of dicts with: name, url, category, description, deadline, deadline_class.
    """
    if not DIRECTORY_FILE.exists():
        logger.error(f"Directory file not found: {DIRECTORY_FILE}")
        sys.exit(1)

    with open(DIRECTORY_FILE, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    entries = []
    current_category = None

    for section in soup.find_all("section"):
        h2 = section.find("h2")
        if h2:
            current_category = h2.get_text(strip=True)

        for opp in section.find_all("div", class_="opp"):
            link = opp.find("a")
            if not link or not link.get("href"):
                continue

            url = link["href"]
            name = link.get_text(strip=True)

            details_div = opp.find("div", class_="details")
            description = details_div.get_text(strip=True) if details_div else ""

            deadline_div = opp.find("div", class_=re.compile(r"deadline"))
            deadline_text = deadline_div.get_text(strip=True) if deadline_div else ""
            deadline_classes = deadline_div.get("class", []) if deadline_div else []
            # Extract status from class (open, closed, upcoming)
            displayed_status = ""
            for cls in deadline_classes:
                if cls in ("open", "closed", "upcoming"):
                    displayed_status = cls
                    break

            entries.append({
                "name": name,
                "url": url,
                "category": current_category,
                "displayed_description": description,
                "displayed_deadline": deadline_text,
                "displayed_status": displayed_status,
            })

    logger.info(f"Parsed {len(entries)} entries from directory.html")
    return entries


def fetch_url(url: str) -> tuple:
    """Fetch URL content. Returns (text_content, error_message)."""
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        # Extract visible text
        soup = BeautifulSoup(response.text, "html.parser")
        for el in soup(["script", "style", "noscript", "meta", "link"]):
            el.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 150:
            return None, "Page appears to require JavaScript"
        return text, None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def extract_facts_from_source(entry: dict, page_text: str, client):
    """
    Use Claude Sonnet to independently extract structured facts from the source page.
    Does NOT show the entry's current displayed info to avoid anchoring bias.
    """
    prompt = f"""You are fact-checking an AI safety opportunities directory. Extract factual details about this specific program/opportunity from the web page content below.

PROGRAM NAME: {entry['name']}
SOURCE URL: {entry['url']}

PAGE CONTENT (first 8000 chars):
{page_text[:8000]}

Extract ONLY what is explicitly stated on the page. If a fact is not mentioned, use null.

Respond with ONLY valid JSON (no markdown, no code fences):
{{
  "stipend": "exact stipend/funding amount as stated, or null",
  "duration": "program duration as stated, or null",
  "location": "location/format (remote, in-person city, hybrid), or null",
  "deadline": "application deadline as stated, or null",
  "status": "open, closed, upcoming, or unknown based on page content",
  "description": "one-sentence summary of what this program offers, based on the page"
}}"""

    try:
        message = client.messages.create(
            model=REVIEW_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            return json.loads(json_match.group())
        logger.warning(f"Could not parse JSON for {entry['name']}")
        return None
    except Exception as e:
        logger.error(f"Claude API error for {entry['name']}: {e}")
        return None


def compare_facts(entry: dict, source_facts: dict) -> list[dict]:
    """
    Programmatically compare displayed info against source-extracted facts.
    Returns list of discrepancies.
    """
    discrepancies = []

    # Compare stipend: check if source mentions a stipend that differs from displayed
    source_stipend = source_facts.get("stipend")
    if source_stipend:
        displayed = entry["displayed_description"]
        # Extract dollar/pound amounts from both
        displayed_amounts = set(re.findall(r'[\$£€]\s?[\d,]+(?:K)?(?:\s?[–-]\s?[\$£€]?\s?[\d,]+(?:K)?)?', displayed, re.IGNORECASE))
        source_amounts = set(re.findall(r'[\$£€]\s?[\d,]+(?:K)?(?:\s?[–-]\s?[\$£€]?\s?[\d,]+(?:K)?)?', source_stipend, re.IGNORECASE))
        if source_amounts and not source_amounts.issubset(displayed_amounts):
            discrepancies.append({
                "field": "stipend",
                "displayed": sorted(displayed_amounts) or "(not shown)",
                "source_says": source_stipend,
                "severity": "high",
            })

    # Compare deadline
    source_deadline = source_facts.get("deadline")
    if source_deadline and entry["displayed_deadline"]:
        # Normalize for comparison (lowercase, strip whitespace)
        d_norm = entry["displayed_deadline"].lower().strip()
        s_norm = source_deadline.lower().strip()
        if d_norm != s_norm and s_norm not in d_norm and d_norm not in s_norm:
            discrepancies.append({
                "field": "deadline",
                "displayed": entry["displayed_deadline"],
                "source_says": source_deadline,
                "severity": "high",
            })
    elif source_deadline and not entry["displayed_deadline"]:
        discrepancies.append({
            "field": "deadline",
            "displayed": "(none shown)",
            "source_says": source_deadline,
            "severity": "medium",
        })

    # Compare status
    source_status = source_facts.get("status", "unknown")
    if (entry["displayed_status"] and source_status != "unknown"
            and entry["displayed_status"] != source_status):
        discrepancies.append({
            "field": "status",
            "displayed": entry["displayed_status"],
            "source_says": source_status,
            "severity": "high",
        })

    # Compare location
    source_location = source_facts.get("location")
    if source_location:
        displayed = entry["displayed_description"].lower()
        loc_lower = source_location.lower()
        # Check if key location terms appear
        loc_terms = [t.strip() for t in re.split(r'[,/;]', loc_lower) if len(t.strip()) > 2]
        missing_terms = [t for t in loc_terms if t not in displayed]
        if missing_terms and loc_lower not in displayed:
            discrepancies.append({
                "field": "location",
                "displayed": "(check description)",
                "source_says": source_location,
                "severity": "low",
            })

    # Compare duration
    source_duration = source_facts.get("duration")
    if source_duration:
        displayed = entry["displayed_description"].lower()
        dur_lower = source_duration.lower()
        if dur_lower not in displayed and not any(
            term in displayed for term in dur_lower.split()
            if len(term) > 3 and term not in ("with", "from", "the", "and", "for")
        ):
            discrepancies.append({
                "field": "duration",
                "displayed": "(check description)",
                "source_says": source_duration,
                "severity": "low",
            })

    return discrepancies


def run_reviewer(dry_run: bool = False, limit: int = None) -> list[dict]:
    """Main reviewer logic. Returns the review report."""
    entries = parse_directory()

    if limit:
        entries = entries[:limit]
        logger.info(f"Limited to first {limit} entries")

    if dry_run:
        logger.info("[DRY RUN] Parsed entries:")
        for e in entries:
            logger.info(f"  - {e['name']} ({e['url'][:60]}...)")
        return []

    # Initialize Anthropic client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    report = []
    checked = 0
    skipped = 0

    for i, entry in enumerate(entries):
        logger.info(f"[{i+1}/{len(entries)}] Checking: {entry['name']}")

        # Fetch source page
        page_text, error = fetch_url(entry["url"])
        if error:
            logger.warning(f"  Skipped (fetch error): {error}")
            report.append({
                "name": entry["name"],
                "url": entry["url"],
                "category": entry["category"],
                "status": "fetch_error",
                "error": error,
                "discrepancies": [],
            })
            skipped += 1
            time.sleep(REQUEST_DELAY)
            continue

        # Extract facts from source (independent of displayed info)
        source_facts = extract_facts_from_source(entry, page_text, client)
        if not source_facts:
            logger.warning(f"  Skipped (extraction failed)")
            report.append({
                "name": entry["name"],
                "url": entry["url"],
                "category": entry["category"],
                "status": "extraction_error",
                "discrepancies": [],
            })
            skipped += 1
            time.sleep(REQUEST_DELAY)
            continue

        # Compare
        discrepancies = compare_facts(entry, source_facts)
        status = "ok" if not discrepancies else "flagged"

        if discrepancies:
            logger.warning(f"  FLAGGED — {len(discrepancies)} discrepancy(ies):")
            for d in discrepancies:
                logger.warning(f"    [{d['severity']}] {d['field']}: displayed={d['displayed']} vs source={d['source_says']}")
        else:
            logger.info(f"  OK")

        report.append({
            "name": entry["name"],
            "url": entry["url"],
            "category": entry["category"],
            "status": status,
            "source_facts": source_facts,
            "discrepancies": discrepancies,
        })
        checked += 1
        time.sleep(REQUEST_DELAY)

    # Save report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"\nReview complete: {checked} checked, {skipped} skipped")

    flagged = [r for r in report if r["status"] == "flagged"]
    if flagged:
        logger.warning(f"{len(flagged)} entries flagged with discrepancies — see {REPORT_FILE}")
    else:
        logger.info("No discrepancies found.")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cross-check directory entries against live source pages"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse directory only, skip fetching and API calls")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only check first N entries")
    args = parser.parse_args()

    run_reviewer(dry_run=args.dry_run, limit=args.limit)
