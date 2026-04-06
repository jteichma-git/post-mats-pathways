#!/usr/bin/env python3
"""
Add approved scanner results to the site (resources.json + index.html + directory.html).

Reads suggested_additions.json, lets you pick which entries to add via
index numbers, then inserts them into all three files.

Usage:
    python add_entries.py                    # Interactive: review and pick entries
    python add_entries.py --ids 1,3,5        # Add entries 1, 3, and 5 directly
    python add_entries.py --all              # Add all entries (score >= 3)
    python add_entries.py --dry-run --all    # Preview without writing files
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_PATH = os.path.join(SCRIPT_DIR, "resources.json")
INDEX_PATH = os.path.join(SCRIPT_DIR, "index.html")
DIRECTORY_PATH = os.path.join(SCRIPT_DIR, "directory.html")
SUGGESTIONS_PATH = os.path.join(SCRIPT_DIR, "suggested_additions.json")

# Maps scanner categories to section markers in the HTML files.
# index.html uses <!-- N. Section Name --> comments before each card.
# directory.html uses <section> tags with id attributes.
CATEGORY_CONFIG = {
    "career-resources": {"section_id": "career-resources"},
    "community":        {"section_id": "community"},
    "fellowships":      {"section_id": "fellowships"},
    "grants":           {"section_id": "grants"},
    "startups":         {"section_id": "startups"},
    "jobs":             {"section_id": "jobs"},
    "phd":              {"section_id": "phd"},
    "policy-internships": {"section_id": "policy-internships"},
    "tech-internships": {"section_id": "tech-internships"},
}


def load_suggestions() -> List[Dict]:
    with open(SUGGESTIONS_PATH) as f:
        data = json.load(f)
    return data.get("suggestions", [])


def load_resources() -> List[Dict]:
    with open(RESOURCES_PATH) as f:
        return json.load(f)


def display_suggestions(suggestions: List[Dict]) -> None:
    """Print numbered list of suggestions for review."""
    category_labels = {
        "fellowships": "Fellowships",
        "grants": "Grants & Funding",
        "tech-internships": "Tech Internships",
        "policy-internships": "Policy Internships",
        "jobs": "Jobs",
        "community": "Community",
        "phd": "PhD & Academic",
        "startups": "Startups",
        "career-resources": "Career Resources",
    }

    print("\n{:<4} {:<5} {:<20} {:<50} {}".format(
        "#", "Score", "Category", "Name", "Status"))
    print("-" * 120)

    for i, s in enumerate(suggestions, 1):
        score = s.get("relevance_score", "?")
        cat = category_labels.get(s.get("category", ""), s.get("category", "?"))
        name = s.get("name", "Unknown")[:48]
        status = s.get("status", "unknown")
        print("{:<4} {:<5} {:<20} {:<50} {}".format(i, score, cat, name, status))

    print()


def pick_entries(suggestions: List[Dict]) -> List[Dict]:
    """Interactive selection of entries to add."""
    display_suggestions(suggestions)
    print("Enter entry numbers to add (comma-separated), 'all' for score>=3, or 'q' to quit:")
    choice = input("> ").strip()

    if choice.lower() == "q":
        sys.exit(0)
    if choice.lower() == "all":
        return [s for s in suggestions if s.get("relevance_score", 0) >= 3]

    try:
        ids = [int(x.strip()) for x in choice.split(",") if x.strip()]
        return [suggestions[i - 1] for i in ids if 1 <= i <= len(suggestions)]
    except (ValueError, IndexError):
        print("Invalid input.")
        sys.exit(1)


def make_resource_entry(entry: Dict) -> Dict:
    """Convert a scanner suggestion into a resources.json entry."""
    return {
        "name": entry.get("name", "Unknown"),
        "url": entry.get("url", ""),
        "category": entry.get("category", "unknown"),
        "current_status": entry.get("status", "unknown"),
        "current_deadline": entry.get("deadline"),
        "current_details": entry.get("description", ""),
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "js_only": False,
    }


def add_to_resources(entries: List[Dict]) -> None:
    """Add entries to resources.json in their correct category positions."""
    resources = load_resources()
    existing_urls = {r["url"].split("?")[0].rstrip("/").lower() for r in resources}

    added = 0
    for entry in entries:
        resource = make_resource_entry(entry)
        norm_url = resource["url"].split("?")[0].rstrip("/").lower()
        if norm_url in existing_urls:
            print("  SKIP (already in resources.json): {}".format(resource["name"]))
            continue

        # Insert alphabetically within the category
        category = resource["category"]
        insert_idx = len(resources)
        for i, r in enumerate(resources):
            if r["category"] == category and r["name"].lower() > resource["name"].lower():
                insert_idx = i
                break
            # If we've passed all entries of this category, insert at end of category
            if i > 0 and resources[i - 1].get("category") == category and r["category"] != category:
                insert_idx = i
                break

        resources.insert(insert_idx, resource)
        existing_urls.add(norm_url)
        added += 1

    with open(RESOURCES_PATH, "w") as f:
        json.dump(resources, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("  resources.json: added {} entries".format(added))


def _deadline_class(status: Optional[str]) -> str:
    """Map status to CSS class."""
    return {
        "open": "open",
        "closed": "closed",
        "upcoming": "upcoming",
        "expression_of_interest": "upcoming",
    }.get(status or "", "")


def add_to_index_html(entries: List[Dict]) -> None:
    """Add entries to index.html card-back sections."""
    with open(INDEX_PATH, "r") as f:
        html = f.read()

    added = 0
    for entry in entries:
        name = entry.get("name", "Unknown")
        url = entry.get("url", "")
        desc = entry.get("description", "")
        status = entry.get("status", "")
        deadline = entry.get("deadline")
        category = entry.get("category", "")

        if url.split("?")[0].rstrip("/") in html:
            print("  SKIP index.html (URL already present): {}".format(name))
            continue

        section_id = CATEGORY_CONFIG.get(category, {}).get("section_id")
        if not section_id:
            print("  SKIP index.html (unknown category '{}'): {}".format(category, name))
            continue

        # Build the HTML block
        lines = ['<div class="opp">']
        lines.append('<a class="opp-name" href="{}" onclick="event.stopPropagation()" target="_blank">{}</a>'.format(url, name))
        if desc:
            lines.append('<div class="opp-detail">{}</div>'.format(desc))
        if deadline:
            css = _deadline_class(status)
            lines.append('<div class="opp-deadline {}">{}</div>'.format(css, deadline))
        lines.append('</div>')
        block = "\n".join(lines)

        # Find the end of this section's card-back and insert before the closing </div>s
        # Pattern: the section ends with </div>\n</div>\n</div>\n<!-- next section or end -->
        # We look for the section comment, then find the last </div>\n</div>\n</div> before next section
        section_pattern = r'(<!-- \d+\.\s+[^>]*?' + re.escape(section_id).replace(r'\-', r'[^>]*?') + r'.*?)'

        # Simpler approach: find the section's card-back by looking for the back-title with the section name
        # Then find the last </div>\n</div> before the next <!-- section
        # Actually, let's find the last <div class="opp"> block in this section and insert after it

        # Find all opp blocks in this category section by locating section boundaries
        # Sections in index.html are delimited by <!-- N. ... --> comments
        section_comments = list(re.finditer(r'<!-- \d+\..*?-->', html))
        section_start = None
        section_end = None
        for i, m in enumerate(section_comments):
            if section_id.replace("-", " ") in m.group().lower().replace("&amp;", "&"):
                section_start = m.start()
                section_end = section_comments[i + 1].start() if i + 1 < len(section_comments) else len(html)
                break

        if section_start is None:
            # Try matching by category keywords
            for i, m in enumerate(section_comments):
                comment_lower = m.group().lower()
                keywords = section_id.replace("-", " ").split()
                if all(kw in comment_lower for kw in keywords):
                    section_start = m.start()
                    section_end = section_comments[i + 1].start() if i + 1 < len(section_comments) else len(html)
                    break

        if section_start is None:
            print("  SKIP index.html (couldn't find section): {}".format(name))
            continue

        section_html = html[section_start:section_end]

        # Find the last </div>\n</div>\n</div>\n</div> which closes the card
        # Insert our block before the closing sequence
        # The card-back ends with repeated </div> tags before the next section
        last_opp = section_html.rfind('<div class="opp">')
        if last_opp == -1:
            print("  SKIP index.html (no opp blocks found in section): {}".format(name))
            continue

        # Find the end of the last opp block (next </div> after the last opp's content)
        # Count div nesting from last_opp
        pos = last_opp
        depth = 0
        opp_end = None
        while pos < len(section_html):
            if section_html[pos:pos+5] == '<div ':
                depth += 1
            elif section_html[pos:pos+4] == '<div>':
                depth += 1
            elif section_html[pos:pos+6] == '</div>':
                depth -= 1
                if depth == 0:
                    opp_end = pos + 6
                    break
            pos += 1

        if opp_end is None:
            print("  SKIP index.html (couldn't parse section): {}".format(name))
            continue

        insert_pos = section_start + opp_end
        html = html[:insert_pos] + "\n" + block + html[insert_pos:]
        added += 1

    with open(INDEX_PATH, "w") as f:
        f.write(html)

    print("  index.html: added {} entries".format(added))


def add_to_directory_html(entries: List[Dict]) -> None:
    """Add entries to directory.html sections."""
    with open(DIRECTORY_PATH, "r") as f:
        html = f.read()

    added = 0
    for entry in entries:
        name = entry.get("name", "Unknown")
        url = entry.get("url", "")
        desc = entry.get("description", "")
        status = entry.get("status", "")
        deadline = entry.get("deadline")
        category = entry.get("category", "")

        if url.split("?")[0].rstrip("/") in html:
            print("  SKIP directory.html (URL already present): {}".format(name))
            continue

        section_id = CATEGORY_CONFIG.get(category, {}).get("section_id")
        if not section_id:
            print("  SKIP directory.html (unknown category '{}'): {}".format(category, name))
            continue

        # Build the HTML block
        lines = ['<div class="opp">']
        lines.append('<h3><a href="{}">{}</a></h3>'.format(url, name))
        if desc:
            lines.append('<div class="details">{}</div>'.format(desc))
        if deadline:
            css = _deadline_class(status)
            lines.append('<div class="deadline {}">{}</div>'.format(css, deadline))
        lines.append('</div>')
        block = "\n".join(lines)

        # Find the section by id and insert before </section>
        section_tag = 'id="{}"'.format(section_id)
        section_start = html.find(section_tag)
        if section_start == -1:
            print("  SKIP directory.html (section '{}' not found): {}".format(section_id, name))
            continue

        # Find </section> after section_start
        section_close = html.find("</section>", section_start)
        if section_close == -1:
            print("  SKIP directory.html (no closing </section>): {}".format(name))
            continue

        html = html[:section_close] + block + "\n" + html[section_close:]
        added += 1

    with open(DIRECTORY_PATH, "w") as f:
        f.write(html)

    print("  directory.html: added {} entries".format(added))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add approved scanner results to the site")
    parser.add_argument("--ids", help="Comma-separated entry numbers to add")
    parser.add_argument("--all", action="store_true",
                        help="Add all entries with relevance score >= 3")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be added without modifying files")
    args = parser.parse_args()

    if not os.path.exists(SUGGESTIONS_PATH):
        print("No suggested_additions.json found. Run scanner.py first.")
        sys.exit(1)

    suggestions = load_suggestions()
    if not suggestions:
        print("No suggestions to add.")
        sys.exit(0)

    # Select entries
    if args.all:
        entries = [s for s in suggestions if s.get("relevance_score", 0) >= 3]
        if not entries:
            print("No entries with relevance score >= 3.")
            sys.exit(0)
    elif args.ids:
        ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
        entries = [suggestions[i - 1] for i in ids if 1 <= i <= len(suggestions)]
    else:
        entries = pick_entries(suggestions)

    if not entries:
        print("No entries selected.")
        sys.exit(0)

    print("\nAdding {} entries:".format(len(entries)))
    for e in entries:
        print("  - {} [{}] ({})".format(
            e.get("name", "?"), e.get("category", "?"), e.get("status", "?")))

    if args.dry_run:
        print("\n[DRY RUN] No files modified.")
        return

    print()
    add_to_resources(entries)
    add_to_index_html(entries)
    add_to_directory_html(entries)
    print("\nDone! Review changes with: git diff")


if __name__ == "__main__":
    main()
