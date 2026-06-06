#!/usr/bin/env python3
"""
jobs_filter.py — Gates the static "AI safety orgs hiring" cards in the Jobs
section on whether the org actually has a current role posted on the
80,000 Hours board this week.

Background: the Jobs section used to carry a fixed set of org cards that all
read "Rolling". Those cards stayed up regardless of whether the org had any
live opening — they were on the page simply because they were seeded there.
This step replaces that behaviour: an org card is rendered for a given week
ONLY if we spot a genuinely-dated, current listing for that org in
80k_jobs.json (the feed fetched by fetch_80k_jobs.py).

"Genuinely-dated" matters because the 80K feed contains a large block of
bulk-import entries all stamped Jan 2022 (sentinel dates). Those are not real
current adverts, so we ignore anything posted before RECENCY_CUTOFF_DAYS.

The managed region lives between these markers in both pages (added once):
    <!-- jobs-orgs:start -->
    ...generated content...
    <!-- jobs-orgs:end -->

Cards are emitted as <div class="opp opp-org"> so the renderer (which matches
class="opp" exactly) leaves them alone — this module is their sole owner.
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
RESOURCES_FILE = BASE_DIR / "resources.json"
JOBS_FILE = BASE_DIR / "80k_jobs.json"
INDEX_FILE = BASE_DIR / "index.html"
DIRECTORY_FILE = BASE_DIR / "directory.html"

START_MARKER = "<!-- jobs-orgs:start -->"
END_MARKER = "<!-- jobs-orgs:end -->"

# Ignore listings older than this — cleanly excludes the Jan-2022 sentinel
# (bulk-import) timestamps while keeping every genuinely-dated current role.
RECENCY_CUTOFF_DAYS = 540

# resources.json URL  ->  exact 80,000 Hours company name(s) to match against.
# Exact (case-insensitive) match avoids substring false positives
# (e.g. "metr" matching "Asymmetric Security").
JOBS_SECTION_ORGS = {
    "https://www.apolloresearch.ai/careers/": ["Apollo Research"],
    "https://www.beneficialaifoundation.org/jobs": ["Beneficial AI Foundation"],
    "https://www.forethought.org/careers": ["Forethought"],
    "https://www.goodfire.ai/careers": ["Goodfire"],
    "https://www.grayswan.ai/careers": ["Gray Swan", "Gray Swan AI"],
    "https://job-boards.greenhouse.io/lawzero": ["LawZero"],
    "https://metr.org/careers": ["Model Evaluation and Threat Research", "METR"],
    "https://techgov.intelligence.org/blog/announcing-miri-technical-governance-team-research-fellowship":
        ["Machine Intelligence Research Institute"],
    "https://www.redwoodresearch.org/careers": ["Redwood Research"],
    "https://saif.org/opportunities/": ["Safe AI Forum", "SAIF"],
    "https://www.simplexaisafety.com/": ["Simplex"],
    "https://transluce.org/": ["Transluce"],
    "https://www.whiteboxresearch.org/": ["WhiteBox", "WhiteBox Research"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def attr_escape(text):
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def text_escape(text):
    """Leave inline HTML markup (e.g. <strong>) and apostrophes alone."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;")


def load_json(path, default):
    if not path.exists():
        logger.warning("  %s not found — treating as empty", path.name)
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def current_listings_by_company(jobs, cutoff_ts):
    """company-name(lowercased) -> list of (posted_ts, title) for current roles."""
    out = {}
    for j in jobs:
        ts = j.get("posted_at")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if ts < cutoff_ts:
            continue
        company = (j.get("company") or "").strip()
        if not company:
            continue
        out.setdefault(company.lower(), []).append((ts, j.get("title") or ""))
    return out


def build_org_spots(resources, jobs):
    """
    Return (kept, dropped) lists of dicts. `kept` orgs have >=1 current 80K
    listing; each carries the resource plus role count and latest posted ts.
    """
    now = datetime.now(timezone.utc)
    cutoff_ts = int(now.timestamp()) - RECENCY_CUTOFF_DAYS * 86400
    listings = current_listings_by_company(jobs, cutoff_ts)

    by_url = {r["url"]: r for r in resources}
    kept, dropped = [], []
    for url, aliases in JOBS_SECTION_ORGS.items():
        resource = by_url.get(url)
        if resource is None:
            logger.warning("  No resources.json entry for %s — skipping", url)
            continue
        matched = []
        for alias in aliases:
            matched.extend(listings.get(alias.lower(), []))
        if matched:
            latest_ts = max(ts for ts, _ in matched)
            kept.append({
                "resource": resource,
                "count": len(matched),
                "latest_ts": latest_ts,
            })
        else:
            dropped.append(resource["name"])

    kept.sort(key=lambda o: o["latest_ts"], reverse=True)
    return kept, dropped


def format_spot_line(org):
    n = org["count"]
    latest = datetime.fromtimestamp(org["latest_ts"], tz=timezone.utc)
    noun = "role" if n == 1 else "roles"
    return "{n} current {noun} on 80,000 Hours · latest posted {date}".format(
        n=n, noun=noun, date=latest.strftime("%b %d, %Y")
    )


def render_card(org, variant):
    r = org["resource"]
    url = attr_escape(r["url"])
    name = text_escape(r["name"])
    details = r.get("current_details") or ""  # may contain inline HTML
    spot = format_spot_line(org)

    if variant == "index":
        parts = [
            '<div class="opp opp-org">',
            ('<a class="opp-name" href="{url}" onclick="event.stopPropagation()" '
             'target="_blank">{name}</a>').format(url=url, name=name),
        ]
        if details:
            parts.append('<div class="opp-detail">{}</div>'.format(details))
        parts.append('<div class="opp-deadline open">{}</div>'.format(text_escape(spot)))
    else:  # directory
        parts = [
            '<div class="opp opp-org">',
            '<h3><a href="{url}">{name}</a></h3>'.format(url=url, name=name),
        ]
        if details:
            parts.append('<div class="details">{}</div>'.format(details))
        parts.append('<div class="deadline open">{}</div>'.format(text_escape(spot)))
    parts.append('</div>')
    return "\n".join(parts)


def render_region(kept, variant):
    lines = [START_MARKER]
    total = len(JOBS_SECTION_ORGS)
    if kept:
        lines.append(
            '<div class="live-jobs-header"><strong>AI safety orgs hiring now</strong> '
            '— shown only when we spot a current role on 80,000 Hours this week '
            '({n} of {total} tracked orgs)</div>'.format(n=len(kept), total=total)
        )
        for org in kept:
            lines.append(render_card(org, variant))
    else:
        lines.append(
            '<p class="live-jobs-empty">None of the tracked AI safety orgs have a '
            'current role on the 80,000 Hours board this week. See the live listings '
            'above.</p>'
        )
    lines.append(END_MARKER)
    return "\n".join(lines)


def patch_file(filepath, region_html):
    content = filepath.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(START_MARKER) + r'.*?' + re.escape(END_MARKER), re.DOTALL
    )
    if not pattern.search(content):
        logger.warning(
            "  No jobs-orgs markers in %s — skipping. Add %s and %s to enable.",
            filepath.name, START_MARKER, END_MARKER,
        )
        return
    new_content = pattern.sub(lambda _: region_html, content)
    if new_content != content:
        filepath.write_text(new_content, encoding="utf-8")
        logger.info("  Patched %s", filepath.name)
    else:
        logger.info("  %s already up to date", filepath.name)


def main():
    resources = load_json(RESOURCES_FILE, [])
    jobs_data = load_json(JOBS_FILE, {})
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else []

    logger.info("Gating Jobs-section org cards on current 80K listings...")
    kept, dropped = build_org_spots(resources, jobs)

    logger.info("  Showing %d orgs: %s", len(kept),
                ", ".join(o["resource"]["name"] for o in kept) or "(none)")
    logger.info("  Hiding %d orgs (no current 80K role this week): %s",
                len(dropped), ", ".join(dropped) or "(none)")

    patch_file(INDEX_FILE, render_region(kept, "index"))
    patch_file(DIRECTORY_FILE, render_region(kept, "directory"))


if __name__ == "__main__":
    main()
