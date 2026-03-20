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
from pathlib import Path

from bs4 import BeautifulSoup

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


def format_deadline_text(change: dict) -> str | None:
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
    Matches entries by their href URL within <h3><a href="..."> tags.
    Returns count of updates made.
    """
    if not DIRECTORY_FILE.exists():
        logger.warning(f"Directory file not found: {DIRECTORY_FILE}")
        return 0

    with open(DIRECTORY_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")
    updates = 0

    for change in changes:
        url = change["url"]
        new_status = change.get("new_status")
        new_deadline_text = format_deadline_text(change)

        if not new_status or new_status == "unknown":
            continue

        # Find all links matching this URL in h3 tags
        links = soup.find_all("a", href=url)
        for link in links:
            # Verify this is inside an opp div (h3 > a pattern in directory.html)
            parent_h3 = link.find_parent("h3")
            if not parent_h3:
                continue
            opp_div = parent_h3.find_parent("div", class_="opp")
            if not opp_div:
                continue

            # Update or create deadline div
            deadline_div = opp_div.find("div", class_="deadline")

            if new_deadline_text:
                new_class = status_to_class(new_status)
                if deadline_div:
                    # Update existing deadline
                    old_text = deadline_div.get_text()
                    old_classes = deadline_div.get("class", [])
                    deadline_div.string = new_deadline_text
                    deadline_div["class"] = ["deadline"]
                    if new_class:
                        deadline_div["class"].append(new_class)
                    logger.info(
                        f"  [directory.html] Updated deadline for '{change['name']}': "
                        f"'{old_text}' -> '{new_deadline_text}' (class: {old_classes} -> {deadline_div['class']})"
                    )
                else:
                    # Create new deadline div
                    new_div = soup.new_tag("div")
                    new_div["class"] = ["deadline"]
                    if new_class:
                        new_div["class"].append(new_class)
                    new_div.string = new_deadline_text
                    opp_div.append(new_div)
                    logger.info(
                        f"  [directory.html] Added deadline for '{change['name']}': '{new_deadline_text}'"
                    )
                updates += 1
            elif deadline_div:
                # Update just the class if status changed but no new deadline text
                new_class = status_to_class(new_status)
                old_classes = deadline_div.get("class", [])
                deadline_div["class"] = ["deadline"]
                if new_class:
                    deadline_div["class"].append(new_class)
                if deadline_div["class"] != old_classes:
                    logger.info(
                        f"  [directory.html] Updated status class for '{change['name']}': "
                        f"{old_classes} -> {deadline_div['class']}"
                    )
                    updates += 1

    if not dry_run and updates > 0:
        # Write back preserving original formatting as much as possible
        output = str(soup)
        with open(DIRECTORY_FILE, "w", encoding="utf-8") as f:
            f.write(output)
        logger.info(f"  Wrote {updates} updates to {DIRECTORY_FILE}")

    return updates


def update_index_html(changes: list[dict], dry_run: bool = False) -> int:
    """
    Update index.html with changed statuses and deadlines.
    Matches entries by their href URL within opp-name links.
    Returns count of updates made.
    """
    if not INDEX_FILE.exists():
        logger.warning(f"Index file not found: {INDEX_FILE}")
        return 0

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")
    updates = 0

    for change in changes:
        url = change["url"]
        new_status = change.get("new_status")
        new_deadline_text = format_deadline_text(change)

        if not new_status or new_status == "unknown":
            continue

        # Find all links matching this URL (opp-name class in index.html)
        links = soup.find_all("a", href=url)
        for link in links:
            # Check if this is an opp-name link or h3 > a link
            opp_div = link.find_parent("div", class_="opp")
            if not opp_div:
                continue

            # Find the deadline div (opp-deadline class in index.html)
            deadline_div = opp_div.find("div", class_=re.compile(r"opp-deadline"))

            if new_deadline_text:
                new_class = status_to_class(new_status)
                if deadline_div:
                    old_text = deadline_div.get_text()
                    old_classes = deadline_div.get("class", [])
                    deadline_div.string = new_deadline_text
                    deadline_div["class"] = ["opp-deadline"]
                    if new_class:
                        deadline_div["class"].append(new_class)
                    logger.info(
                        f"  [index.html] Updated deadline for '{change['name']}': "
                        f"'{old_text}' -> '{new_deadline_text}' (class: {old_classes} -> {deadline_div['class']})"
                    )
                else:
                    new_div = soup.new_tag("div")
                    new_div["class"] = ["opp-deadline"]
                    if new_class:
                        new_div["class"].append(new_class)
                    new_div.string = new_deadline_text
                    opp_div.append(new_div)
                    logger.info(
                        f"  [index.html] Added deadline for '{change['name']}': '{new_deadline_text}'"
                    )
                updates += 1
            elif deadline_div:
                new_class = status_to_class(new_status)
                old_classes = deadline_div.get("class", [])
                deadline_div["class"] = ["opp-deadline"]
                if new_class:
                    deadline_div["class"].append(new_class)
                if deadline_div["class"] != old_classes:
                    logger.info(
                        f"  [index.html] Updated status class for '{change['name']}': "
                        f"{old_classes} -> {deadline_div['class']}"
                    )
                    updates += 1

    if not dry_run and updates > 0:
        output = str(soup)
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            f.write(output)
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

    total = dir_updates + idx_updates
    logger.info(f"\nTotal updates applied: {total} ({dir_updates} in directory.html, {idx_updates} in index.html)")
    return total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Update HTML files with crawled resource changes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    run_updater(dry_run=args.dry_run)
