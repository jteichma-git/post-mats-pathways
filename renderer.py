#!/usr/bin/env python3
"""
renderer.py — Renders index.html and directory.html from resources.json.

Replaces every <div class="opp">...</div> card with freshly-generated HTML
derived from resources.json. Cards are matched by the URL in their <a href>.
Also regenerates the "What's New" block (from change_report.json) and the
"Last updated" stamp (from max(last_content_change) across resources).

Drift between resources.json and the HTML is structurally impossible because
we always render JSON -> HTML, never the reverse.
"""

import bisect
import json
import logging
import re
import sys
from datetime import datetime, timezone
from html import escape as html_escape


def attr_escape(text):
    """Escape for use inside double-quoted HTML attribute values."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def text_escape(text):
    """Minimal escape for inline text — leaves apostrophes and inline HTML markup alone."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
RESOURCES_FILE = BASE_DIR / "resources.json"
REPORT_FILE = BASE_DIR / "change_report.json"
INDEX_FILE = BASE_DIR / "index.html"
DIRECTORY_FILE = BASE_DIR / "directory.html"


# --- Deadline parsing (used to derive a "past-deadline" closed override) ---

_MONTH = (
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
)

_MONTH_ABBREVS = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September",
    "Oct": "October", "Nov": "November", "Dec": "December",
}


def _expand_month(text: str) -> str:
    for abbr, full in _MONTH_ABBREVS.items():
        text = re.sub(rf'\b{abbr}\.?\b', full, text)
    return text


def parse_deadline_date(deadline_text):
    """
    Try to extract a concrete datetime from a deadline string.
    Returns None for rolling/TBD/unknown/etc.

    Thin wrapper over parse_deadline() for callers that only want the date.
    """
    parsed = parse_deadline(deadline_text)
    return parsed[0] if parsed else None


def parse_deadline(deadline_text):
    """
    Try to extract a concrete datetime from a deadline string.
    Returns (datetime, year_assumed) or None for rolling/TBD/unknown/etc.

    `year_assumed` is True when the string carried no year and we filled in
    the current one — those entries expire on a guess, so run_renderer lists
    them for a human to confirm.
    """
    if not deadline_text:
        return None

    skip_phrases = [
        "rolling", "continuous", "unknown", "tbd", "not yet",
        "not announced", "check ", "year-round", "updated ",
        "recurring", "multiple cohorts", "always open",
        "quarterly", "each quarter", "monthly", "ongoing",
        # An entry with no real submission window: any date in the string
        # belongs to something else (a past job posting, a cohort that ran).
        "self-paced", "self paced", "no deadline", "nomination-only",
        "nomination only",
    ]
    lower = deadline_text.lower()
    if any(p in lower for p in skip_phrases):
        return None

    cleaned = _expand_month(deadline_text)
    cleaned = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', cleaned)

    year_patterns = [
        rf'({_MONTH}\s+\d{{1,2}},?\s+\d{{4}})',
        rf'(\d{{1,2}}\s+{_MONTH}\s+\d{{4}})',
    ]
    year_formats = ["%B %d, %Y", "%B %d %Y", "%d %B %Y"]

    for pattern in year_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            date_str = match.group(1)
            for fmt in year_formats:
                try:
                    return datetime.strptime(date_str, fmt), False
                except ValueError:
                    continue

    current_year = datetime.now().year
    no_year_patterns = [
        rf'({_MONTH})\s+(\d{{1,2}})\b',
        rf'(\d{{1,2}})\s+({_MONTH})\b',
    ]
    for i, pattern in enumerate(no_year_patterns):
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            if i == 0:
                month_str, day_str = match.group(1), match.group(2)
            else:
                day_str, month_str = match.group(1), match.group(2)
            try:
                return (
                    datetime.strptime(f"{month_str} {day_str} {current_year}", "%B %d %Y"),
                    True,
                )
            except ValueError:
                continue

    return None


def derive_status_class(status, deadline_text):
    """
    Map a (status, deadline) pair to a CSS class.
    A deadline date in the past forces 'closed' regardless of stored status.
    """
    if status == "closed":
        return "closed"
    deadline_date = parse_deadline_date(deadline_text)
    if deadline_date and deadline_date.date() < datetime.now().date():
        return "closed"
    mapping = {
        "open": "open",
        "upcoming": "upcoming",
        "expression_of_interest": "upcoming",
        "closed": "closed",
    }
    return mapping.get(status, "")


# --- Expiry: once you can no longer submit, the card leaves the main list ---

def expiry(resource):
    """
    Decide whether a resource still accepts submissions.

    Returns (expired: bool, year_assumed: bool). A resource expires the day
    after its deadline — there is no grace period, because a passed deadline
    is exactly the thing we don't want on the page.
    """
    if resource.get("current_status") == "closed":
        return True, False
    deadline_text = resource.get("current_deadline") or ""
    # The crawler sometimes files a closed program under expression_of_interest
    # while writing "Closed - ..." into the deadline. Trust the deadline text.
    if "closed" in deadline_text.lower():
        return True, False
    parsed = parse_deadline(deadline_text)
    if not parsed:
        return False, False
    deadline_date, year_assumed = parsed
    return deadline_date.date() < datetime.now().date(), year_assumed


def is_expired(resource):
    return expiry(resource)[0]


def sort_key(resource):
    """
    Order live cards: soonest real deadline first, then everything undated
    (rolling, continuous, directories) alphabetically.
    """
    parsed = parse_deadline(resource.get("current_deadline") or "")
    if parsed:
        return (0, parsed[0], resource.get("name", "").lower())
    return (1, datetime.max, resource.get("name", "").lower())


# --- Card-block locator: find <div class="opp">...</div> by URL ---

def find_opp_blocks(content):
    """
    Yield (start_index, end_index) for each <div class="opp">...</div> block
    in `content`. .opp divs are flat (no nested .opp), so a depth counter
    over <div ... /</div> tags gives us reliable boundaries.

    Self-closing or void elements inside .opp are not counted (none used here).
    """
    open_re = re.compile(r'<div\b[^>]*>', re.IGNORECASE)
    close_re = re.compile(r'</div\s*>', re.IGNORECASE)
    opp_start_re = re.compile(r'<div\s+class="opp"\s*>', re.IGNORECASE)

    pos = 0
    while True:
        m = opp_start_re.search(content, pos)
        if not m:
            return
        start = m.start()
        depth = 0
        i = start
        while i < len(content):
            o = open_re.search(content, i)
            c = close_re.search(content, i)
            if not c:
                # Malformed; bail out of this block.
                pos = start + 1
                break
            if o and o.start() < c.start():
                depth += 1
                i = o.end()
            else:
                depth -= 1
                i = c.end()
                if depth == 0:
                    yield (start, i)
                    pos = i
                    break
        else:
            pos = start + 1


def extract_opp_url(block_html):
    """Return the first href URL in an .opp block, or None."""
    m = re.search(r'<a\b[^>]*\bhref="([^"]+)"', block_html, re.IGNORECASE)
    return m.group(1) if m else None


# --- Card renderers (one per HTML variant) ---

def render_index_card(resource, status_class):
    """Render the .opp block as it appears inside an index.html flip card."""
    url = attr_escape(resource["url"])
    name = text_escape(resource["name"])
    details = resource.get("current_details") or ""  # may contain inline HTML
    deadline = resource.get("current_deadline")

    parts = [
        '<div class="opp">',
        (
            f'<a class="opp-name" href="{url}" '
            f'onclick="event.stopPropagation()" target="_blank">{name}</a>'
        ),
    ]
    if details:
        parts.append(f'<div class="opp-detail">{details}</div>')
    if deadline:
        cls = f'opp-deadline {status_class}'.strip()
        parts.append(f'<div class="{cls}">{text_escape(deadline)}</div>')
    parts.append('</div>')
    return "\n".join(parts)


def render_directory_card(resource, status_class):
    """Render the .opp block as it appears in directory.html."""
    url = attr_escape(resource["url"])
    name = text_escape(resource["name"])
    details = resource.get("current_details") or ""  # may contain inline HTML
    deadline = resource.get("current_deadline")

    parts = [
        '<div class="opp">',
        f'<h3><a href="{url}">{name}</a></h3>',
    ]
    if details:
        parts.append(f'<div class="details">{details}</div>')
    if deadline:
        cls = f'deadline {status_class}'.strip()
        parts.append(f'<div class="{cls}">{text_escape(deadline)}</div>')
    parts.append('</div>')
    return "\n".join(parts)


# --- Closed entries: one grey line each, below the live cards ---
#
# A closed program still tells a reader something ("CHAI runs an internship,
# watch for it"), but it should cost one line to learn that, not a full card.
# So the name and the closing note survive; the description does not.

# The wrapper is marked so the next run can peel it off WITHOUT eating the
# cards inside it — a closed program must stay a real .opp block, or it can
# never be promoted back to live when its next cycle opens.
ARCHIVE_OPEN = ("<!-- archive:open -->", "<!-- /archive:open -->")
ARCHIVE_CLOSE = ("<!-- archive:close -->", "<!-- /archive:close -->")

ARCHIVE_HEADING = "Closed for now &mdash; worth knowing for the next cycle"


def render_index_closed(resource):
    """A closed card: same .opp wrapper, name and closing note only."""
    url = attr_escape(resource["url"])
    name = text_escape(resource["name"])
    note = resource.get("current_deadline") or "Closed"
    return (
        f'<div class="opp">\n'
        f'<a class="opp-name opp-name-past" href="{url}" '
        f'onclick="event.stopPropagation()" target="_blank">{name}</a>\n'
        f'<div class="opp-deadline closed">{text_escape(note)}</div>\n'
        f'</div>'
    )


def render_directory_closed(resource):
    url = attr_escape(resource["url"])
    name = text_escape(resource["name"])
    note = resource.get("current_deadline") or "Closed"
    return (
        f'<div class="opp">\n'
        f'<h3 class="opp-name-past"><a href="{url}">{name}</a></h3>\n'
        f'<div class="deadline closed">{text_escape(note)}</div>\n'
        f'</div>'
    )


def render_archive(closed_html):
    """Wrap the closed cards in peelable markers."""
    if not closed_html:
        return ""
    head = (
        f'{ARCHIVE_OPEN[0]}<div class="opp-archive">'
        f'<div class="opp-archive-heading">{ARCHIVE_HEADING}</div>{ARCHIVE_OPEN[1]}'
    )
    tail = f'{ARCHIVE_CLOSE[0]}</div>{ARCHIVE_CLOSE[1]}'
    return head + "\n" + "\n".join(closed_html) + "\n" + tail


def strip_archives(content):
    """
    Peel off archive wrappers, leaving the .opp cards they contain in place,
    so every run re-derives the live/closed split from resources.json.
    """
    for opener, closer in (ARCHIVE_OPEN, ARCHIVE_CLOSE):
        # Swallow the whitespace on either side too, so repeated runs converge
        # instead of adding a blank line per section every week.
        content = re.sub(
            r"\s*" + re.escape(opener) + r".*?" + re.escape(closer) + r"\s*",
            "\n",
            content,
            flags=re.DOTALL,
        )
    return content


# --- Rendering pass over a single HTML file ---

def rerender_file(filepath, resources, card_renderer, closed_renderer, container_pattern):
    """
    Rebuild every .opp block in `filepath` from resources.json.

    Unlike a card-for-card replacement, this re-partitions each container
    (a flip card's back, or a directory section): live opportunities first,
    ordered by how soon they close, then any hand-written blocks we don't
    own, then the grey one-line list of closed entries.
    """
    content = strip_archives(filepath.read_text(encoding="utf-8"))
    by_url = {r["url"]: r for r in resources}

    blocks = list(find_opp_blocks(content))
    if not blocks:
        logger.warning(f"No .opp blocks found in {filepath.name}")
        return 0

    container_starts = [m.start() for m in re.finditer(container_pattern, content, re.IGNORECASE)]
    grouped = {}
    for block in blocks:
        idx = bisect.bisect_right(container_starts, block[0]) - 1
        grouped.setdefault(idx, []).append(block)

    new_content = content
    rendered = live_total = closed_total = skipped = 0

    # Reverse document order keeps earlier offsets valid as we splice.
    for idx in sorted(grouped, reverse=True):
        group = grouped[idx]
        live, closed, orphans = [], [], []

        for start, end in group:
            block_html = content[start:end]
            url = extract_opp_url(block_html)
            resource = by_url.get(url) if url else None
            if resource is None:
                orphans.append(block_html)
                skipped += 1
            elif is_expired(resource):
                closed.append(resource)
            else:
                live.append(resource)

        live.sort(key=sort_key)
        closed.sort(key=lambda r: r.get("name", "").lower())

        parts = [
            card_renderer(
                r,
                derive_status_class(r.get("current_status"), r.get("current_deadline")),
            )
            for r in live
        ]
        parts.extend(orphans)
        archive = render_archive([closed_renderer(r) for r in closed])
        if archive:
            parts.append(archive)

        new_content = (
            new_content[: group[0][0]] + "\n".join(parts) + new_content[group[-1][1] :]
        )
        rendered += len(live) + len(closed)
        live_total += len(live)
        closed_total += len(closed)

    if new_content != filepath.read_text(encoding="utf-8"):
        filepath.write_text(new_content, encoding="utf-8")
        logger.info(
            f"  {filepath.name}: {live_total} live, {closed_total} moved to closed lists "
            f"({skipped} hand-written blocks preserved)"
        )
    else:
        logger.info(f"  {filepath.name}: no changes")
    return rendered


# --- What's New + Last updated stamp ---

def load_report():
    if not REPORT_FILE.exists():
        return []
    with open(REPORT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_whats_new_html(report):
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    status_changes = []
    deadline_updates = []
    errors = []

    for entry in report:
        action = entry.get("action", "")
        name = entry.get("name", "Unknown")

        if action == "changed":
            old_s = entry.get("old_status", "unknown")
            new_s = entry.get("new_status", "unknown")
            old_d = entry.get("old_deadline")
            new_d = entry.get("new_deadline")

            if new_s != old_s and new_s != "unknown":
                status_changes.append(
                    f'<li><strong>{html_escape(name)}</strong> — status changed from '
                    f'<span class="wn-old">{html_escape(str(old_s))}</span> to '
                    f'<span class="wn-new wn-{html_escape(str(new_s))}">{html_escape(str(new_s))}</span></li>'
                )
            if new_d and new_d != "null" and new_d != old_d:
                deadline_updates.append(
                    f'<li><strong>{html_escape(name)}</strong> — deadline updated to {html_escape(str(new_d))}</li>'
                )
        elif action in ("fetch_error", "http_error"):
            # Recorded in change_report.json and surfaced in the run log; a
            # reader of the site has no use for our HTTP status codes.
            errors.append(f'{name} — {entry.get("error", "could not fetch")}')

    if errors:
        logger.warning(
            "  %d fetch error(s) this cycle (not shown on the page): %s",
            len(errors), "; ".join(errors),
        )

    if not status_changes and not deadline_updates:
        inner = '<p class="wn-empty">No changes detected this cycle.</p>'
    else:
        parts = []
        if status_changes:
            parts.append(
                '<div class="wn-group"><h4>Status Changes</h4><ul>'
                + "\n".join(status_changes) + "</ul></div>"
            )
        if deadline_updates:
            parts.append(
                '<div class="wn-group"><h4>Deadline Updates</h4><ul>'
                + "\n".join(deadline_updates) + "</ul></div>"
            )
        inner = "\n".join(parts)

    return (
        f'<details class="whats-new" id="whats-new">\n'
        f'<summary>What\'s New &mdash; {today}</summary>\n'
        f'<div class="wn-content">\n{inner}\n</div>\n'
        f'</details>'
    )


def update_whats_new(report):
    new_html = generate_whats_new_html(report)
    existing_pattern = re.compile(
        r'<details class="whats-new" id="whats-new">.*?</details>',
        re.DOTALL,
    )
    for filepath in [INDEX_FILE, DIRECTORY_FILE]:
        content = filepath.read_text(encoding="utf-8")
        if existing_pattern.search(content):
            new_content = existing_pattern.sub(new_html, content)
        elif '<!-- whats-new -->' in content:
            new_content = content.replace('<!-- whats-new -->', new_html)
        else:
            logger.warning(f"  No whats-new marker in {filepath.name} — skipping")
            continue
        if new_content != content:
            filepath.write_text(new_content, encoding="utf-8")
            logger.info(f"  Updated 'What's New' in {filepath.name}")


def latest_content_change_date(resources):
    """Return the most recent last_content_change across resources, formatted."""
    dates = []
    for r in resources:
        v = r.get("last_content_change") or r.get("last_checked")
        if not v:
            continue
        try:
            dates.append(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    if not dates:
        return datetime.now(timezone.utc).strftime("%B %d, %Y")
    return max(dates).strftime("%B %d, %Y")


def update_last_updated_stamp(resources):
    stamp = latest_content_change_date(resources)
    pattern = re.compile(r'(id="last-updated">Last updated: )(.*?)(</)')
    for filepath in [INDEX_FILE, DIRECTORY_FILE]:
        content = filepath.read_text(encoding="utf-8")
        new_content = pattern.sub(rf'\g<1>{stamp}\3', content)
        if new_content != content:
            filepath.write_text(new_content, encoding="utf-8")
            logger.info(f"  Last updated stamp -> {stamp} in {filepath.name}")


# --- Entry point ---

def report_expiries(resources):
    """Log what dropped off the page this run, and what needs a human."""
    expired = [r for r in resources if is_expired(r)]
    assumed = [r["name"] for r in resources if expiry(r) == (True, True)]
    logger.info(
        f"  {len(resources) - len(expired)} live, {len(expired)} closed"
    )
    if assumed:
        logger.warning(
            "  Expired on an assumed year (deadline text has no year — verify): %s",
            ", ".join(assumed),
        )


def load_resources():
    if not RESOURCES_FILE.exists():
        logger.error(f"Resources file not found: {RESOURCES_FILE}")
        sys.exit(1)
    with open(RESOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def run_renderer():
    resources = load_resources()
    report = load_report()

    logger.info(f"Rendering {len(resources)} resources -> HTML")
    logger.info(f"\nRendering {INDEX_FILE.name}...")
    rerender_file(
        INDEX_FILE, resources, render_index_card, render_index_closed,
        r'<div class="card-back">',
    )

    logger.info(f"\nRendering {DIRECTORY_FILE.name}...")
    rerender_file(
        DIRECTORY_FILE, resources, render_directory_card, render_directory_closed,
        r'<section\b',
    )

    report_expiries(resources)

    logger.info(f"\nUpdating 'What's New' section...")
    update_whats_new(report)

    logger.info(f"\nUpdating 'Last updated' stamp...")
    update_last_updated_stamp(resources)


if __name__ == "__main__":
    run_renderer()
