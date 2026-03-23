#!/usr/bin/env python3
"""
updater.py - Patches index.html and directory.html with updated resource info.

Reads the change report from crawler.py and updates deadline text, status classes,
and details in both HTML files. Matches entries by their href URL.
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
REPORT_FILE = BASE_DIR / "change_report.json"
INDEX_FILE = BASE_DIR / "index.html"
DIRECTORY_FILE = BASE_DIR / "directory.html"


def load_report() -> list[dict]:
    """Load the change report."""
    if not REPORT_FILE.exists():
        logger.error(f"Change report not found: {REPORT_FILE}")
        logger.error("Run crawler.py first to generate the report.")
        sys.exit(1)
    with open(REPORT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_changes(report: list[dict]) -> list[dict]:
    """Filter report to only entries with actual changes."""
    return [r for r in report if r.get("action") == "changed"]


def extract_deadline_date(deadline_text: str):
    """
    Try to extract a concrete date from deadline text.
    Returns a datetime if found, None otherwise.
    Handles formats like:
      - "March 22, 2026"
      - "23:59 PT Sunday 22nd March"
      - "May 17, 2026 11:59 PM"
      - "March 30, 2026 at 23:59 GMT"
      - "April 22, 2026 11:59:59 PM PT"
      - "January 7, 2026"
    """
    if not deadline_text:
        return None

    # Skip entries that are clearly not concrete deadlines
    skip_phrases = [
        "rolling", "continuous", "unknown", "tbd", "not yet",
        "not announced", "check ", "year-round", "updated ",
        "recurring", "multiple cohorts",
    ]
    lower = deadline_text.lower()
    if any(phrase in lower for phrase in skip_phrases):
        return None

    # Remove ordinal suffixes (1st, 2nd, 3rd, 22nd, etc.)
    cleaned = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', deadline_text)

    # Common date formats to try
    date_patterns = [
        # "March 22, 2026" or "March 22 2026"
        r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})',
        # "22 March 2026"
        r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
        # "March 30 2026" (without comma)
        r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s+\d{4})',
    ]

    date_formats = [
        "%B %d, %Y",   # March 22, 2026
        "%B %d %Y",    # March 22 2026
        "%d %B %Y",    # 22 March 2026
    ]

    for pattern in date_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            date_str = match.group(1)
            for fmt in date_formats:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue

    # Try to find month + day without year — assume current year
    month_day_pattern = r'(?:23:59|11:59).*?((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2})\b'
    match = re.search(month_day_pattern, cleaned, re.IGNORECASE)
    if not match:
        # Also try "day month" order: "22 March", "30 March"
        month_day_pattern2 = r'(\d{1,2})\s+((?:January|February|March|April|May|June|July|August|September|October|November|December))\b'
        match = re.search(month_day_pattern2, cleaned, re.IGNORECASE)
        if match:
            date_str = f"{match.group(2)} {match.group(1)} {datetime.now().year}"
            try:
                return datetime.strptime(date_str, "%B %d %Y")
            except ValueError:
                pass
    else:
        date_str = f"{match.group(1)} {datetime.now().year}"
        try:
            return datetime.strptime(date_str, "%B %d %Y")
        except ValueError:
            pass

    return None


def check_deadline_passed(status: str, deadline_text: str) -> str:
    """
    If a deadline date is in the past and the status is not already 'closed',
    override the status to 'closed'.
    """
    if status == "closed":
        return status

    deadline_date = extract_deadline_date(deadline_text)
    if deadline_date and deadline_date.date() < datetime.now().date():
        logger.info(f"    Deadline '{deadline_text}' is in the past — overriding status to 'closed'")
        return "closed"

    return status


def status_to_class(status: str) -> str:
    """Map status string to CSS class name."""
    mapping = {
        "open": "open",
        "closed": "closed",
        "upcoming": "upcoming",
        "expression_of_interest": "upcoming",
        "unknown": "",
    }
    return mapping.get(status, "")


def format_deadline_text(change):
    """
    Format the deadline text for display.
    Returns None if no meaningful deadline to display.
    """
    new_deadline = change.get("new_deadline")
    if not new_deadline or new_deadline == "null":
        return None
    return new_deadline


def update_directory_html(changes: list[dict], dry_run: bool = False) -> int:
    """
    Update directory.html with changed statuses and deadlines.
    Uses targeted regex replacement to avoid BeautifulSoup re-serialization
    which can mangle closing div tags.
    Returns count of updates made.
    """
    if not DIRECTORY_FILE.exists():
        logger.warning(f"Directory file not found: {DIRECTORY_FILE}")
        return 0

    content = DIRECTORY_FILE.read_text(encoding="utf-8")
    new_content, updates = _apply_changes_via_regex(
        content, changes, "directory.html", deadline_class_prefix="deadline"
    )

    if not dry_run and updates > 0:
        DIRECTORY_FILE.write_text(new_content, encoding="utf-8")
        logger.info(f"  Wrote {updates} updates to {DIRECTORY_FILE}")

    return updates


def _apply_changes_via_regex(content: str, changes: list[dict], file_label: str,
                              deadline_class_prefix: str = "opp-deadline") -> tuple[str, int]:
    """
    Apply deadline/status changes to HTML content using targeted regex
    replacements instead of re-serializing the whole DOM with BeautifulSoup.

    This avoids BeautifulSoup's html.parser mangling closing tags on write-back.
    We still use BeautifulSoup to *find* which URLs exist and what their current
    deadline text is, but all mutations happen via string replacement on the
    original content.

    Returns (updated_content, update_count).
    """
    updates = 0

    for change in changes:
        url = change["url"]
        new_status = change.get("new_status")
        new_deadline_text = format_deadline_text(change)

        if not new_status or new_status == "unknown":
            continue

        # Check if the deadline date has passed — override status to closed
        if new_deadline_text:
            new_status = check_deadline_passed(new_status, new_deadline_text)

        new_css_class = status_to_class(new_status)

        # Escape URL for use in regex
        escaped_url = re.escape(url)

        # Find the opp-deadline div that follows an <a> with this href.
        # Pattern: ...href="<url>"... then the next opp-deadline div
        deadline_pattern = re.compile(
            rf'(href="{escaped_url}"[^>]*>.*?)'
            rf'(<div\s+class="({deadline_class_prefix})\s*[^"]*">)(.*?)(</div>)',
            re.DOTALL,
        )

        match = deadline_pattern.search(content)
        if match and new_deadline_text:
            old_div = match.group(2) + match.group(4) + match.group(5)
            new_class_attr = f'{deadline_class_prefix} {new_css_class}'.strip()
            new_div = f'<div class="{new_class_attr}">{new_deadline_text}</div>'
            content = content.replace(old_div, new_div, 1)
            logger.info(
                f"  [{file_label}] Updated deadline for '{change['name']}': "
                f"'{match.group(4)}' -> '{new_deadline_text}'"
            )
            updates += 1
        elif match and not new_deadline_text:
            # Update just the CSS class
            old_div_tag = match.group(2)
            new_class_attr = f'{deadline_class_prefix} {new_css_class}'.strip()
            new_div_tag = f'<div class="{new_class_attr}">'
            if old_div_tag != new_div_tag:
                content = content.replace(old_div_tag, new_div_tag, 1)
                logger.info(
                    f"  [{file_label}] Updated status class for '{change['name']}'"
                )
                updates += 1

    return content, updates


def update_index_html(changes: list[dict], dry_run: bool = False) -> int:
    """
    Update index.html with changed statuses and deadlines.
    Uses targeted regex replacement to avoid BeautifulSoup re-serialization
    which can mangle closing div tags.
    Returns count of updates made.
    """
    if not INDEX_FILE.exists():
        logger.warning(f"Index file not found: {INDEX_FILE}")
        return 0

    content = INDEX_FILE.read_text(encoding="utf-8")
    new_content, updates = _apply_changes_via_regex(
        content, changes, "index.html", deadline_class_prefix="opp-deadline"
    )

    if not dry_run and updates > 0:
        INDEX_FILE.write_text(new_content, encoding="utf-8")
        logger.info(f"  Wrote {updates} updates to {INDEX_FILE}")

    return updates


def run_updater(dry_run: bool = False) -> int:
    """
    Main updater logic. Returns total number of updates made.
    """
    report = load_report()
    changes = get_changes(report)

    if not changes:
        logger.info("No changes to apply. HTML files are up to date.")
        return 0

    logger.info(f"Found {len(changes)} changes to apply:")
    for c in changes:
        logger.info(f"  - {c['name']}: {c['old_status']} -> {c['new_status']}")
        if c.get("new_deadline"):
            logger.info(f"    Deadline: {c.get('old_deadline')} -> {c.get('new_deadline')}")

    if dry_run:
        logger.info("\n[DRY RUN] Would update the following files:")
        logger.info(f"  - {INDEX_FILE}")
        logger.info(f"  - {DIRECTORY_FILE}")
        logger.info("No files were modified.")
        return len(changes)

    logger.info(f"\nUpdating {DIRECTORY_FILE}...")
    dir_updates = update_directory_html(changes, dry_run=dry_run)

    logger.info(f"\nUpdating {INDEX_FILE}...")
    idx_updates = update_index_html(changes, dry_run=dry_run)

    # Close any deadlines that have passed (catches entries not in the change report)
    logger.info(f"\nChecking for past deadlines in {DIRECTORY_FILE}...")
    dir_content = DIRECTORY_FILE.read_text(encoding="utf-8")
    dir_content, dir_closed = close_past_deadlines(dir_content, "directory.html", "deadline")
    if dir_closed > 0:
        DIRECTORY_FILE.write_text(dir_content, encoding="utf-8")
        dir_updates += dir_closed

    logger.info(f"\nChecking for past deadlines in {INDEX_FILE}...")
    idx_content = INDEX_FILE.read_text(encoding="utf-8")
    idx_content, idx_closed = close_past_deadlines(idx_content, "index.html", "opp-deadline")
    if idx_closed > 0:
        INDEX_FILE.write_text(idx_content, encoding="utf-8")
        idx_updates += idx_closed

    # Update the "last updated" date on both pages
    update_last_updated_date()

    total = dir_updates + idx_updates
    logger.info(f"\nTotal updates applied: {total} ({dir_updates} in directory.html, {idx_updates} in index.html)")
    return total


def close_past_deadlines(content: str, file_label: str,
                         deadline_class_prefix: str = "opp-deadline") -> tuple:
    """
    Scan HTML for any deadline divs with dates in the past that are still
    marked as 'open' or 'upcoming', and flip them to 'closed'.
    This catches entries not in the change report.
    Returns (updated_content, update_count).
    """
    updates = 0
    # Match deadline divs with open/upcoming class
    pattern = re.compile(
        rf'<div\s+class="({deadline_class_prefix})\s+(open|upcoming)">(.*?)</div>',
        re.DOTALL,
    )

    for match in pattern.finditer(content):
        css_class = match.group(2)
        deadline_text = match.group(3).strip()
        deadline_date = extract_deadline_date(deadline_text)

        if deadline_date and deadline_date.date() < datetime.now().date():
            old_div = match.group(0)
            new_div = f'<div class="{deadline_class_prefix} closed">{deadline_text}</div>'
            content = content.replace(old_div, new_div, 1)
            logger.info(
                f"  [{file_label}] Closed past deadline: '{deadline_text}' "
                f"(was {css_class})"
            )
            updates += 1

    return content, updates


def update_last_updated_date():
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    pattern = re.compile(r'(id="last-updated">Last updated: )(.*?)(</)')

    for filepath in [INDEX_FILE, DIRECTORY_FILE]:
        content = filepath.read_text(encoding="utf-8")
        new_content = pattern.sub(rf'\g<1>{today}\3', content)
        if new_content != content:
            filepath.write_text(new_content, encoding="utf-8")
            logger.info(f"  Updated 'Last updated' date to {today} in {filepath.name}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Update HTML files with crawled resource changes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    run_updater(dry_run=args.dry_run)
