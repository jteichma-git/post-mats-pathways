#!/usr/bin/env python3
"""
crawler.py - Fetches resource URLs and uses Claude API to detect status changes.

Reads resources.json, fetches each URL, sends content to Claude Haiku for analysis,
and produces a change report. Updates resources.json with new statuses.
"""

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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RESOURCES_FILE = Path(__file__).parent / "resources.json"
REPORT_FILE = Path(__file__).parent / "change_report.json"

# Request settings
REQUEST_TIMEOUT = 30
PLAYWRIGHT_TIMEOUT = 20000  # ms
REQUEST_DELAY = 2  # seconds between requests to be polite
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# JS-only detection thresholds
MIN_TEXT_LENGTH = 200
JS_REQUIRED_PHRASES = [
    "you need to enable javascript",
    "please enable javascript",
    "javascript is required",
    "this page requires javascript",
    "enable javascript to view",
    "javascript must be enabled",
]

# Ashby API: map domains to org slugs for direct API access
ASHBY_ORGS = {
    "jobs.ashbyhq.com/tilderesearch": "tilderesearch",
    "jobs.ashbyhq.com/virtue-AI": "virtue-AI",
    "jobs.ashbyhq.com/virtue-ai": "virtue-AI",
}

# Check if Playwright is available
_playwright_available = None

def is_playwright_available() -> bool:
    """Check if Playwright and its browsers are installed."""
    global _playwright_available
    if _playwright_available is None:
        try:
            from playwright.sync_api import sync_playwright
            # Quick check that browsers are installed
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            _playwright_available = True
        except Exception:
            _playwright_available = False
    return _playwright_available


def load_resources() -> list[dict]:
    """Load resources from resources.json."""
    if not RESOURCES_FILE.exists():
        logger.error(f"Resources file not found: {RESOURCES_FILE}")
        sys.exit(1)
    with open(RESOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_resources(resources: list[dict]) -> None:
    """Save resources to resources.json."""
    with open(RESOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(resources, f, indent=2, ensure_ascii=False)
    logger.info(f"Updated {RESOURCES_FILE}")


def save_report(report: list[dict]) -> None:
    """Save the change report to change_report.json."""
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"Change report saved to {REPORT_FILE}")


def fetch_url(url):
    """
    Fetch URL content. Returns (html_content, status_code, error_message).
    """
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        return response.text, response.status_code, None
    except requests.exceptions.Timeout:
        return None, None, "Request timed out"
    except requests.exceptions.ConnectionError:
        return None, None, "Connection error"
    except requests.exceptions.TooManyRedirects:
        return None, None, "Too many redirects"
    except requests.exceptions.RequestException as e:
        return None, None, f"Request error: {str(e)}"


def fetch_with_playwright(url):
    """
    Fetch URL using a headless browser (Playwright).
    Renders JavaScript and returns (html_content, status_code, error_message).
    Used as fallback for JS-only pages and sites that block simple requests.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None, "Playwright not installed"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            # Use domcontentloaded (faster) then wait briefly for JS to render
            response = page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            # Give JS frameworks time to hydrate
            page.wait_for_timeout(3000)
            status_code = response.status if response else None
            html = page.content()
            browser.close()

            if status_code and status_code >= 400:
                return None, status_code, f"HTTP {status_code}"
            return html, status_code, None
    except Exception as e:
        return None, None, f"Playwright error: {str(e)}"


def fetch_ashby_api(url):
    """
    Fetch job listings from Ashby's public API for known org slugs.
    Returns (text_content, 200, error_message) — text is pre-extracted, not HTML.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path_key = f"{parsed.netloc}{parsed.path}".rstrip("/")

    org_slug = None
    for pattern, slug in ASHBY_ORGS.items():
        if pattern in path_key:
            org_slug = slug
            break

    if not org_slug:
        return None, None, "Not an Ashby URL"

    api_url = "https://api.ashbyhq.com/posting-api/job-board/" + org_slug
    try:
        resp = requests.get(api_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None, resp.status_code, f"Ashby API HTTP {resp.status_code}"
        data = resp.json()
    except Exception as e:
        return None, None, f"Ashby API error: {str(e)}"

    # Convert job listings to readable text
    jobs = data.get("jobs", [])
    if not jobs:
        text = f"{org_slug} job board: No open positions listed."
        return text, 200, None

    lines = [f"{org_slug} job board — {len(jobs)} open position(s):\n"]
    for job in jobs:
        title = job.get("title", "Untitled")
        location = job.get("location", "Unknown location")
        team = job.get("department", "")
        employment = job.get("employmentType", "")
        published = job.get("publishedAt", "")
        parts = [f"- {title}"]
        if location:
            parts.append(f"  Location: {location}")
        if team:
            parts.append(f"  Team: {team}")
        if employment:
            parts.append(f"  Type: {employment}")
        if published:
            parts.append(f"  Posted: {published}")
        lines.append("\n".join(parts))

    text = "\n\n".join(lines)
    return text, 200, None


def is_ashby_url(url: str) -> bool:
    """Check if a URL is a known Ashby job board."""
    return any(pattern in url for pattern in ASHBY_ORGS)


def extract_text_from_html(html: str) -> str:
    """Extract visible text from HTML, stripping tags."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script and style elements
    for element in soup(["script", "style", "noscript", "meta", "link"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_js_only(html: str) -> bool:
    """
    Detect if a page is JS-only (renders content via JavaScript).
    Returns True if the page appears to require JavaScript.
    """
    if not html:
        return False

    text = extract_text_from_html(html)
    lower_text = text.lower()

    # Check for JS-required phrases
    for phrase in JS_REQUIRED_PHRASES:
        if phrase in lower_text:
            return True

    # Check if body has very little text content
    if len(text) < MIN_TEXT_LENGTH:
        return True

    return False


def analyze_with_claude(resource, page_text, client):
    """
    Send page content to Claude Sonnet for status analysis.
    Returns parsed JSON response or None on failure.
    """
    prompt = f"""Analyze this web page content for an AI safety opportunity/resource tracker.

RESOURCE INFO ON FILE:
- Name: {resource['name']}
- URL: {resource['url']}
- Category: {resource['category']}
- Current stored status: {resource['current_status']}
- Current stored deadline: {resource.get('current_deadline', 'None')}
- Current stored details: {resource.get('current_details', 'None')}

PAGE CONTENT (first 8000 chars):
{page_text[:8000]}

Based on the page content, determine:
1. Is this program/opportunity currently accepting applications?
2. What are the current deadlines?
3. What is the current stipend/funding amount?
4. Has anything changed from what we have on file?

Respond with ONLY valid JSON (no markdown, no code fences) in this exact format:
{{
  "status": "open" or "closed" or "upcoming" or "expression_of_interest" or "unknown",
  "deadline": "deadline string or null if none found",
  "stipend": "stipend/funding info string or null if not mentioned",
  "key_changes": "description of what's different from stored info, or 'No changes detected' if same"
}}

Rules for status:
- "open" = actively accepting applications now
- "closed" = applications are closed, past deadline
- "upcoming" = will open soon, or has a future deadline but not yet accepting
- "expression_of_interest" = accepting expressions of interest but not formal applications
- "unknown" = cannot determine from page content (e.g., resource pages, directories, career boards)
"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()

        # Try to extract JSON from response (handle occasional markdown wrapping)
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            return json.loads(json_match.group())
        else:
            logger.warning(f"Could not parse JSON from Claude response for {resource['name']}")
            return None
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode error for {resource['name']}: {e}")
        return None
    except Exception as e:
        logger.error(f"Claude API error for {resource['name']}: {e}")
        return None


def run_crawler(dry_run: bool = False) -> list[dict]:
    """
    Main crawler logic. Returns the change report.
    """
    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Set it with: export ANTHROPIC_API_KEY='your-key-here'"
        )
        sys.exit(1)

    # Initialize Anthropic client
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    resources = load_resources()
    change_report = []
    now = datetime.now(timezone.utc).isoformat()

    total = len(resources)
    logger.info(f"Starting crawl of {total} resources...")

    for i, resource in enumerate(resources, 1):
        name = resource["name"]
        url = resource["url"]
        logger.info(f"[{i}/{total}] Processing: {name}")
        logger.info(f"  URL: {url}")

        # Skip Slack URLs (require authentication)
        if "slack.com" in url:
            logger.info(f"  Skipping Slack URL (requires auth)")
            resource["last_checked"] = now
            continue

        # --- Fetch pipeline: try Ashby API, then requests, then Playwright ---
        page_text = None
        fetch_method = None
        final_error = None

        # 1) Ashby API shortcut — returns pre-extracted text
        if is_ashby_url(url):
            logger.info(f"  Trying Ashby API...")
            text, status_code, error = fetch_ashby_api(url)
            if text and not error:
                page_text = text
                fetch_method = "ashby_api"
                logger.info(f"  Fetched via Ashby API ({len(page_text)} chars)")

        # 2) Standard requests fetch
        if page_text is None:
            html, status_code, error = fetch_url(url)
            needs_fallback = False

            if error:
                logger.warning(f"  Fetch error: {error}")
                needs_fallback = True
                final_error = error
            elif status_code and status_code >= 400:
                logger.warning(f"  HTTP {status_code}")
                needs_fallback = True
                final_error = f"HTTP {status_code}"
            else:
                logger.info(f"  Fetched OK (HTTP {status_code})")
                is_js = detect_js_only(html)
                if is_js:
                    if not resource.get("js_only"):
                        logger.info(f"  Detected as JS-only page")
                        resource["js_only"] = True
                    needs_fallback = True
                    final_error = "JS-only page"
                else:
                    page_text = extract_text_from_html(html)
                    fetch_method = "requests"

            # 3) Playwright fallback for 403s, connection errors, and JS-only pages
            if needs_fallback and is_playwright_available():
                logger.info(f"  Retrying with Playwright...")
                html, status_code, pw_error = fetch_with_playwright(url)
                if html and not pw_error:
                    text = extract_text_from_html(html)
                    if len(text) >= MIN_TEXT_LENGTH:
                        page_text = text
                        fetch_method = "playwright"
                        resource["js_only"] = False  # Playwright can handle it now
                        logger.info(f"  Fetched via Playwright ({len(page_text)} chars)")
                    else:
                        logger.warning(f"  Playwright returned too little text ({len(text)} chars)")
                else:
                    logger.warning(f"  Playwright fallback failed: {pw_error}")

        # If all fetch methods failed, record the error and move on
        if page_text is None:
            error_action = "fetch_error"
            if final_error and final_error.startswith("HTTP"):
                error_action = "http_error"
            elif final_error == "JS-only page":
                error_action = "js_only"
            change_report.append({
                "name": name,
                "url": url,
                "action": error_action,
                "error": final_error,
                "old_status": resource["current_status"],
                "new_status": None,
                "old_deadline": resource.get("current_deadline"),
                "new_deadline": None,
                "key_changes": f"Could not fetch: {final_error}",
            })
            resource["last_checked"] = now
            time.sleep(REQUEST_DELAY)
            continue

        if len(page_text) < 50:
            logger.warning(f"  Very little text content extracted ({len(page_text)} chars)")

        # Analyze with Claude
        logger.info(f"  Sending to Claude for analysis...")
        analysis = analyze_with_claude(resource, page_text, client)

        if analysis is None:
            logger.warning(f"  Claude analysis failed")
            change_report.append({
                "name": name,
                "url": url,
                "action": "analysis_error",
                "error": "Claude analysis returned no result",
                "old_status": resource["current_status"],
                "new_status": None,
                "old_deadline": resource.get("current_deadline"),
                "new_deadline": None,
                "key_changes": "Analysis failed",
            })
            resource["last_checked"] = now
            time.sleep(REQUEST_DELAY)
            continue

        # Compare and detect changes
        old_status = resource["current_status"]
        new_status = analysis.get("status", "unknown")
        old_deadline = resource.get("current_deadline")
        new_deadline = analysis.get("deadline")
        new_stipend = analysis.get("stipend")
        key_changes = analysis.get("key_changes", "")

        has_changes = False
        if new_status != old_status and new_status != "unknown":
            has_changes = True
        if new_deadline and new_deadline != old_deadline and new_deadline != "null":
            has_changes = True

        if has_changes:
            logger.info(f"  CHANGES DETECTED:")
            if new_status != old_status:
                logger.info(f"    Status: {old_status} -> {new_status}")
            if new_deadline and new_deadline != old_deadline:
                logger.info(f"    Deadline: {old_deadline} -> {new_deadline}")
            logger.info(f"    Details: {key_changes}")

        report_entry = {
            "name": name,
            "url": url,
            "action": "changed" if has_changes else "unchanged",
            "error": None,
            "old_status": old_status,
            "new_status": new_status,
            "old_deadline": old_deadline,
            "new_deadline": new_deadline,
            "new_stipend": new_stipend,
            "key_changes": key_changes,
        }
        change_report.append(report_entry)

        # Update resource if not dry run
        if not dry_run and has_changes:
            if new_status != "unknown":
                resource["current_status"] = new_status
            if new_deadline and new_deadline != "null":
                resource["current_deadline"] = new_deadline
            if new_stipend and new_stipend != "null":
                # Store stipend info in details if it's new info
                pass  # Keep existing details, stipend tracked in report

        resource["last_checked"] = now

        # Rate limiting
        time.sleep(REQUEST_DELAY)

    # Save updated resources and report
    if not dry_run:
        save_resources(resources)
    save_report(change_report)

    # Print summary
    changes = [r for r in change_report if r["action"] == "changed"]
    errors = [r for r in change_report if r["action"] in ("fetch_error", "http_error", "analysis_error")]
    js_only = [r for r in change_report if r["action"] == "js_only"]

    logger.info(f"\n{'='*60}")
    logger.info(f"CRAWL COMPLETE")
    logger.info(f"  Total resources: {total}")
    logger.info(f"  Changes detected: {len(changes)}")
    logger.info(f"  Errors: {len(errors)}")
    logger.info(f"  JS-only (manual review): {len(js_only)}")
    logger.info(f"  Unchanged: {total - len(changes) - len(errors) - len(js_only)}")
    logger.info(f"{'='*60}")

    if changes:
        logger.info("\nCHANGES:")
        for c in changes:
            logger.info(f"  {c['name']}:")
            logger.info(f"    Status: {c['old_status']} -> {c['new_status']}")
            if c.get("new_deadline"):
                logger.info(f"    Deadline: {c['old_deadline']} -> {c['new_deadline']}")
            logger.info(f"    Details: {c['key_changes']}")

    if errors:
        logger.info("\nERRORS:")
        for e in errors:
            logger.info(f"  {e['name']}: {e.get('error', 'Unknown error')}")

    if js_only:
        logger.info("\nJS-ONLY (needs manual review):")
        for j in js_only:
            logger.info(f"  {j['name']}: {j['url']}")

    return change_report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crawl AI safety resources and detect changes")
    parser.add_argument("--dry-run", action="store_true", help="Don't update resources.json")
    args = parser.parse_args()

    run_crawler(dry_run=args.dry_run)
