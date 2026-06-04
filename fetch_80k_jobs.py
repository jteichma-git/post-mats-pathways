#!/usr/bin/env python3
"""
fetch_80k_jobs.py — Fetches current AI safety & policy listings from the
80,000 Hours job board (via the public Algolia search API) and patches
index.html / directory.html with a refreshed live-listings section.

Writes 80k_jobs.json with the raw fetched data.

HTML markers (must already exist in both pages):
    <!-- 80k-jobs:start -->
    ...generated content...
    <!-- 80k-jobs:end -->

The block between these markers is regenerated on each run. We use
<div class="opp opp-live"> so the existing renderer (which matches
class="opp" exactly) leaves these blocks alone.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
OUTPUT_JSON = BASE_DIR / "80k_jobs.json"
INDEX_FILE = BASE_DIR / "index.html"
DIRECTORY_FILE = BASE_DIR / "directory.html"

ALGOLIA = {
    "app_id": "W6KM1UDIB3",
    "api_key": "d1d7f2c8696e7b36837d5ed337c4a319",
    "index": "jobs_prod",
    "area_filter": "AI safety & policy",
    "hits_per_page": 100,
}

MAX_JOBS = 40

START_MARKER = "<!-- 80k-jobs:start -->"
END_MARKER = "<!-- 80k-jobs:end -->"

PUBLIC_FILTER_URL = (
    "https://jobs.80000hours.org/?refinementList%5Btags_area%5D%5B0%5D="
    "AI%20safety%20%26%20policy"
)

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
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fetch_jobs():
    endpoint = "https://{}-dsn.algolia.net/1/indexes/{}/query".format(
        ALGOLIA["app_id"], ALGOLIA["index"]
    )
    headers = {
        "X-Algolia-Application-Id": ALGOLIA["app_id"],
        "X-Algolia-API-Key": ALGOLIA["api_key"],
        "Content-Type": "application/json",
    }
    all_jobs = []
    seen = set()
    page = 0
    while True:
        body = {
            "query": "",
            "filters": 'tags_area:"{}"'.format(ALGOLIA["area_filter"]),
            "hitsPerPage": ALGOLIA["hits_per_page"],
            "page": page,
            "attributesToRetrieve": [
                "title", "company_name", "url_external",
                "tags_city", "tags_country", "tags_location_type",
                "tags_role_type", "tags_skill", "tags_exp_required",
                "posted_at", "closes_at",
            ],
        }
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Algolia page %d failed: %s", page, e)
            break
        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            break
        for hit in hits:
            url = (hit.get("url_external") or "").split("?")[0]
            if not url or url in seen:
                continue
            seen.add(url)
            # tags_city already includes country (e.g. "London, UK"), so prefer
            # city when present and fall back to country only when there is no city.
            def _aslist(v):
                if v is None:
                    return []
                return v if isinstance(v, list) else [v]
            city = _aslist(hit.get("tags_city"))
            country = _aslist(hit.get("tags_country"))
            loc_type = _aslist(hit.get("tags_location_type"))
            location_parts = list(city) if city else list(country)
            # Append location_type (e.g. "Remote") if not already implied.
            for lt in loc_type:
                if lt and lt not in location_parts and not any(lt.lower() in p.lower() for p in location_parts):
                    location_parts.append(lt)
            all_jobs.append({
                "title": hit.get("title", ""),
                "company": hit.get("company_name", ""),
                "url": url,
                "location": location_parts,
                "role_type": hit.get("tags_role_type") or [],
                "skill": hit.get("tags_skill") or [],
                "experience": hit.get("tags_exp_required") or [],
                "posted_at": hit.get("posted_at"),
                "closes_at": hit.get("closes_at"),
            })
        page += 1
        if page >= data.get("nbPages", 1):
            break
        time.sleep(1)

    all_jobs.sort(key=lambda j: j.get("posted_at") or 0, reverse=True)
    return all_jobs


def format_meta(job):
    bits = []
    # Deduplicate location parts while preserving order
    loc_parts = job.get("location") or []
    seen_loc = set()
    loc_unique = []
    for p in loc_parts:
        if p and p not in seen_loc:
            seen_loc.add(p)
            loc_unique.append(p)
    if loc_unique:
        bits.append(", ".join(loc_unique))
    role = ", ".join(job.get("role_type") or [])
    if role:
        bits.append(role)
    exp = ", ".join(job.get("experience") or [])
    if exp:
        bits.append(exp)
    ts = job.get("posted_at")
    if ts:
        try:
            posted = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            bits.append("posted " + posted.strftime("%b %d, %Y"))
        except (ValueError, OSError, TypeError):
            pass
    closes = job.get("closes_at")
    if closes:
        try:
            cd = datetime.fromtimestamp(int(closes), tz=timezone.utc)
            bits.append("closes " + cd.strftime("%b %d, %Y"))
        except (ValueError, OSError, TypeError):
            pass
    return " · ".join(bits)


def render_jobs_html(jobs, variant):
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    if not jobs:
        return (
            "{start}\n"
            "<p class=\"live-jobs-empty\">No live 80,000 Hours listings could be fetched "
            "(last attempted {today}). See "
            "<a href=\"{full}\" target=\"_blank\">the 80,000 Hours job board</a> directly.</p>\n"
            "{end}"
        ).format(start=START_MARKER, end=END_MARKER, today=today, full=PUBLIC_FILTER_URL)

    rendered = jobs[:MAX_JOBS]
    lines = [START_MARKER]
    lines.append(
        '<div class="live-jobs-header"><strong>Live from 80,000 Hours</strong> — '
        'showing latest <strong>{n}</strong> of {total} AI safety &amp; policy '
        'listings · last fetched {today} · '
        '<a href="{full}" target="_blank">see all on 80,000 Hours →</a></div>'.format(
            n=len(rendered), total=len(jobs), today=today, full=PUBLIC_FILTER_URL
        )
    )

    for j in rendered:
        url = attr_escape(j["url"])
        company = text_escape(j.get("company") or "Unknown company")
        title = text_escape(j.get("title") or "Untitled role")
        meta = format_meta(j)

        if variant == "index":
            lines.append('<div class="opp opp-live">')
            lines.append(
                '<a class="opp-name" href="{url}" '
                'onclick="event.stopPropagation()" target="_blank">'
                '<strong>{company}</strong>: {title}</a>'.format(
                    url=url, company=company, title=title
                )
            )
            if meta:
                lines.append('<div class="opp-detail">{}</div>'.format(text_escape(meta)))
            lines.append('</div>')
        else:  # directory
            lines.append('<div class="opp opp-live">')
            lines.append(
                '<h3><a href="{url}" target="_blank">'
                '<strong>{company}</strong>: {title}</a></h3>'.format(
                    url=url, company=company, title=title
                )
            )
            if meta:
                lines.append('<div class="details">{}</div>'.format(text_escape(meta)))
            lines.append('</div>')

    lines.append(END_MARKER)
    return "\n".join(lines)


def patch_file(filepath, html_block):
    content = filepath.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(START_MARKER) + r'.*?' + re.escape(END_MARKER),
        re.DOTALL,
    )
    if not pattern.search(content):
        logger.warning(
            "  No 80k-jobs markers found in %s — skipping. "
            "Add %s and %s to enable.",
            filepath.name, START_MARKER, END_MARKER,
        )
        return
    new_content = pattern.sub(html_block, content)
    if new_content != content:
        filepath.write_text(new_content, encoding="utf-8")
        logger.info("  Patched %s (%d bytes)", filepath.name, len(html_block))
    else:
        logger.info("  %s already up to date", filepath.name)


def main():
    logger.info("Fetching 80K Hours AI safety & policy jobs via Algolia...")
    jobs = fetch_jobs()
    logger.info("Got %d unique jobs", len(jobs))

    OUTPUT_JSON.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "filter": ALGOLIA["area_filter"],
                "count": len(jobs),
                "jobs": jobs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Wrote %s", OUTPUT_JSON.name)

    patch_file(INDEX_FILE, render_jobs_html(jobs, "index"))
    patch_file(DIRECTORY_FILE, render_jobs_html(jobs, "directory"))


if __name__ == "__main__":
    main()
