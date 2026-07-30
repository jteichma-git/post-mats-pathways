#!/usr/bin/env python3
"""
Discovery scanner for new AI safety opportunities not already tracked in resources.json.

Phases:
  1. Scrape aggregator sites for org names and URLs
  2. Scrape community forums for new opportunities
  3. Check known org career pages for new program types
  4. Deduplicate against existing resources
  5. Evaluate relevance with Claude Haiku
  6. Output results to suggested_additions.json

Usage:
    python scanner.py
    python scanner.py --dry-run   # Skip Claude API calls
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGGREGATOR_URLS = [
    "https://www.aisafety.com/jobs",
    "https://www.aisafety.com/funding",
    "https://www.aisafety.com/events-and-training",
    # 80K Hours job board is JS-rendered; scraped via Algolia API in scrape_80k_hours()
]

# 80,000 Hours job board Algolia credentials (public search-only key)
ALGOLIA_80K = {
    "app_id": "W6KM1UDIB3",
    "api_key": "d1d7f2c8696e7b36837d5ed337c4a319",
    "index": "jobs_prod",
    "area_filter": "AI safety & policy",
    "hits_per_page": 100,
}

FORUM_URLS = [
    "https://forum.effectivealtruism.org/topics/ai-safety",
    "https://www.lesswrong.com/tag/ai-safety",
    "https://aisafetyfunding.substack.com/",
]

REQUEST_TIMEOUT = 20  # seconds
RATE_LIMIT_DELAY = 2  # seconds between requests
MIN_RELEVANCE_SCORE = 3

# How far back to look in the MATS #opportunities Slack channel for discovery.
# Wider than the 7-day live-feed window because this is about not *missing* new
# fellowships/academic/funding posts, not about freshness.
SLACK_LOOKBACK_DAYS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AISafetyScanner/1.0; "
        "+https://github.com/post-mats-site)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

RELEVANCE_PROMPT = """You are an expert on AI safety career opportunities. Analyze the following web page content and determine if it describes an organization or program specifically focused on AI safety, AI alignment, AI security, AI governance, or AI existential risk.

Be CONSERVATIVE. General AI/ML jobs, generic tech accelerators, or organizations that only tangentially touch AI safety should score low.

IMPORTANT: AI governance and AI policy work focused on safety/security/alignment should score EQUALLY HIGH as technical AI safety work. Both technical and governance approaches are core to the AI safety field.

Criteria for high scores:
- 5: Directly and primarily focused on AI safety/alignment/security/governance (e.g., an alignment research lab, an AI safety fellowship, an AI governance think tank, an AI security startup)
- 4: Strong AI safety/governance component as a major part of their mission
- 3: Meaningful AI safety or AI governance work among other focuses
- 2: Tangentially related (general tech policy, broad tech ethics without AI safety focus)
- 1: Not specifically AI safety focused

Page URL: {url}
Organization/Program name (if known): {name}

Page content (truncated):
{content}

Return ONLY a JSON object (no markdown, no explanation) with these fields:
- "name": string — the organization or program name
- "url": string — the best URL for this opportunity
- "relevance_score": integer 1-5
- "category": string — one of: "career-resources", "community", "fellowships", "grants", "startups", "jobs", "phd", "policy-internships", "tech-internships"
- "description": string — 1-2 sentence description of what this opportunity offers
- "status": string — one of: "open", "closed", "upcoming", "expression_of_interest", "unknown"
- "deadline": string or null — application deadline if mentioned"""

CAREER_CHECK_PROMPT = """You are an expert on AI safety career opportunities. I have an existing entry for this organization in our database:

Organization: {org_name}
Existing details: {existing_details}
Category: {existing_category}

Here is the current content of their careers/programs page:

{content}

Does this page now mention any NEW program types that are NOT reflected in the existing details above? For example:
- A new internship program
- A new fellowship
- A new grant or funding opportunity
- A new training program or course

Be conservative - only flag genuinely new offerings, not minor updates to existing programs.

If there are new programs, return a JSON array of objects, each with:
- "name": string
- "url": string
- "description": string
- "category": string (one of: "fellowships", "grants", "tech-internships", "policy-internships", "jobs", "community", "phd", "startups")

If there is nothing new, return an empty JSON array: []

Return ONLY the JSON array, no markdown or explanation."""


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication comparison."""
    url = url.strip().lower()
    # Remove protocol
    url = re.sub(r'^https?://', '', url)
    # Remove www prefix
    url = re.sub(r'^www\.', '', url)
    # Remove trailing slash
    url = url.rstrip('/')
    # Remove query parameters
    url = url.split('?')[0]
    # Remove fragment
    url = url.split('#')[0]
    return url


def extract_domain(url: str) -> str:
    """Extract the base domain from a URL."""
    normalized = normalize_url(url)
    return normalized.split('/')[0]


# ---------------------------------------------------------------------------
# Web fetching
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[str]:
    """Fetch a web page and return its HTML content."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.Timeout:
        logger.warning("Timeout fetching %s", url)
    except requests.exceptions.HTTPError as e:
        logger.warning("HTTP error fetching %s: %s", url, e)
    except requests.exceptions.ConnectionError as e:
        logger.warning("Connection error fetching %s: %s", url, e)
    except requests.exceptions.RequestException as e:
        logger.warning("Error fetching %s: %s", url, e)
    return None


def extract_text(html: str, max_chars: int = 8000) -> str:
    """Extract visible text from HTML, truncated to max_chars."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script and style elements
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Collapse multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Phase 1: Scrape aggregator sites
# ---------------------------------------------------------------------------

def extract_org_links(html: str, base_url: str) -> List[Dict[str, str]]:
    """Extract organization names and URLs from an aggregator page."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_urls = set()  # type: Set[str]

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        text = a_tag.get_text(strip=True)

        if not href or not text:
            continue
        if len(text) < 3 or len(text) > 150:
            continue

        # Resolve relative URLs
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Skip non-http links, anchors, mailto, etc.
        if parsed.scheme not in ("http", "https"):
            continue

        # Skip links back to the same aggregator domain
        aggregator_domains = [
            "aisafety.com", "80000hours.org", "effectivealtruism.org",
            "lesswrong.com", "substack.com",
        ]
        link_domain = parsed.netloc.lower().replace("www.", "")
        is_aggregator = any(d in link_domain for d in aggregator_domains)

        # Skip common non-org links
        skip_patterns = [
            "twitter.com", "x.com", "linkedin.com", "facebook.com",
            "github.com", "youtube.com", "instagram.com", "mailto:",
            "javascript:", "google.com/maps", "maps.google",
            "#", "tel:", "privacy", "terms", "cookie",
        ]
        if any(pat in full_url.lower() for pat in skip_patterns):
            continue

        normalized = normalize_url(full_url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)

        # If linking to an external org, it is likely an org link
        if not is_aggregator:
            candidates.append({"name": text, "url": full_url})

    return candidates


def scrape_80k_hours() -> List[Dict[str, str]]:
    """Scrape the 80,000 Hours job board via its Algolia search API.

    Uses the 'AI safety & policy' area tag to filter results instead of
    keyword queries, which is more precise and matches the site's own
    categorization.
    """
    logger.info("Fetching 80K Hours job board via Algolia API (area: '%s')",
                ALGOLIA_80K["area_filter"])
    candidates = []
    seen_urls = set()  # type: Set[str]

    cfg = ALGOLIA_80K
    endpoint = "https://{}-dsn.algolia.net/1/indexes/{}/query".format(
        cfg["app_id"], cfg["index"]
    )
    headers = {
        "X-Algolia-Application-Id": cfg["app_id"],
        "X-Algolia-API-Key": cfg["api_key"],
        "Content-Type": "application/json",
    }

    page = 0
    total_hits = None
    while True:
        try:
            resp = requests.post(
                endpoint,
                headers=headers,
                json={
                    "query": "",
                    "filters": 'tags_area:"{}"'.format(cfg["area_filter"]),
                    "hitsPerPage": cfg["hits_per_page"],
                    "page": page,
                    "attributesToRetrieve": [
                        "title", "company_name", "url_external",
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])
            if total_hits is None:
                total_hits = data.get("nbHits", 0)
                logger.info("  Total listings tagged '%s': %d", cfg["area_filter"], total_hits)

            if not hits:
                break

            for hit in hits:
                url = hit.get("url_external", "")
                if not url:
                    continue
                url = url.split("?")[0]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                name = hit.get("title", "")
                company = hit.get("company_name", "")
                display = "{} @ {}".format(name, company) if company else name
                candidates.append({"name": display, "url": url})

            page += 1
            if page >= data.get("nbPages", 1):
                break
        except Exception as e:
            logger.warning("  Algolia page %d failed: %s", page, e)
            break
        time.sleep(RATE_LIMIT_DELAY)

    logger.info("  80K Hours total: %d unique candidates", len(candidates))
    return candidates


def scrape_aggregators() -> List[Dict[str, str]]:
    """Phase 1: Scrape aggregator sites for org links."""
    logger.info("=== Phase 1: Scraping aggregator sites ===")
    all_candidates = []

    for url in AGGREGATOR_URLS:
        logger.info("Fetching aggregator: %s", url)
        html = fetch_page(url)
        if html:
            links = extract_org_links(html, url)
            logger.info("  Found %d external links", len(links))
            all_candidates.extend(links)
        else:
            logger.warning("  Failed to fetch %s", url)
        time.sleep(RATE_LIMIT_DELAY)

    # 80K Hours job board (JS-rendered, use Algolia API)
    all_candidates.extend(scrape_80k_hours())

    logger.info("Phase 1 total: %d candidate links from aggregators", len(all_candidates))
    return all_candidates


# ---------------------------------------------------------------------------
# Phase 2: Scrape community forums
# ---------------------------------------------------------------------------

def scrape_forums() -> List[Dict[str, str]]:
    """Phase 2: Scrape community forums for new opportunity links."""
    logger.info("=== Phase 2: Scraping community forums ===")
    all_candidates = []

    for url in FORUM_URLS:
        logger.info("Fetching forum page: %s", url)
        html = fetch_page(url)
        if html:
            links = extract_org_links(html, url)
            logger.info("  Found %d external links", len(links))
            all_candidates.extend(links)
        else:
            logger.warning("  Failed to fetch %s", url)
        time.sleep(RATE_LIMIT_DELAY)

    logger.info("Phase 2 total: %d candidate links from forums", len(all_candidates))
    return all_candidates


# ---------------------------------------------------------------------------
# Phase 2b: Scan the MATS #opportunities Slack channel
# ---------------------------------------------------------------------------

# Utility / non-resource links that show up in posts but aren't opportunities
# worth adding to resources.json.
SLACK_SKIP_SUBSTRINGS = (
    "mailto:", "docs.google.com", "drive.google.com", "calendly.com",
    "luma.com", "lu.ma", "scholar.google", "airtable.com",
    "x.com", "twitter.com", "linkedin.com", "facebook.com",
    "jobs.80000hours.org",
)

# Link labels too generic to use as a candidate name.
_GENERIC_LABELS = {
    "here", "apply", "apply here", "link", "form", "this", "details",
    "role description", "application form", "more", ".", "",
}


def _slack_candidate_name(label: str, url: str) -> str:
    label = (label or "").strip()
    if label and label.lower() not in _GENERIC_LABELS and len(label) >= 3:
        return label[:150]
    netloc = urlparse(url).netloc.lower().replace("www.", "")
    return netloc or url


def scrape_slack_opportunities() -> List[Dict[str, str]]:
    """Phase 2b: Pull opportunity links from the MATS #opportunities channel.

    Reuses the same SLACK_BOT_TOKEN as fetch_slack_feed.py. Each real link in a
    post from the last SLACK_LOOKBACK_DAYS days becomes a candidate; the existing
    dedup + Claude relevance/category evaluation then routes it (fellowships, phd,
    jobs, grants, ...) into suggested_additions.json for review. Degrades to an
    empty list (with a warning) if the token or module is unavailable.
    """
    logger.info("=== Phase 2b: Scanning #opportunities Slack channel ===")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        logger.warning("  SLACK_BOT_TOKEN not set — skipping Slack scan.")
        return []
    try:
        from fetch_slack_feed import fetch_messages_from_slack, CHANNEL_ID
    except Exception as e:
        logger.warning("  Could not import fetch_slack_feed (%s) — skipping.", e)
        return []

    oldest = datetime.now(timezone.utc).timestamp() - SLACK_LOOKBACK_DAYS * 86400
    try:
        messages = fetch_messages_from_slack(token, CHANNEL_ID, oldest)
    except Exception as e:
        logger.warning("  Slack fetch failed (%s) — skipping.", e)
        return []

    link_re = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")
    candidates = []
    seen = set()  # type: Set[str]
    for msg in messages:
        if msg.get("subtype") in ("channel_join", "channel_leave", "tombstone"):
            continue
        text = msg.get("text", "") or ""
        if "This message was deleted" in text:
            continue
        for m in link_re.finditer(text):
            url = m.group(1).strip()
            label = m.group(2)
            if any(s in url.lower() for s in SLACK_SKIP_SUBSTRINGS):
                continue
            normalized = normalize_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append({"name": _slack_candidate_name(label, url), "url": url})

    logger.info("Phase 2b total: %d candidate links from #opportunities "
                "(%d messages scanned)", len(candidates), len(messages))
    return candidates


# ---------------------------------------------------------------------------
# Phase 2c: Sweep known AI-safety funder RFP / opportunity pages for new grants
# ---------------------------------------------------------------------------

# Curated funder pages that list open grants / RFPs. Scraped for candidate links
# each run; results flow through the same dedup + Claude relevance/category
# evaluation. Starting point — tune based on the per-source fetch log below
# (some funder pages are JS-heavy and yield little via static fetch).
GRANT_SOURCE_URLS = [
    "https://www.openphilanthropy.org/request-for-proposals/",
    "https://coefficientgiving.org/",
    "https://www.cooperativeai.com/",
    "https://www.aria.org.uk/",
    "https://www.longview.org/",
    "https://survivalandflourishing.fund/",
    "https://foresight.org/grants/",
    "https://www.aisafetyfund.org/",
    "https://www.schmidtsciences.org/",
]


def scrape_grant_sources() -> List[Dict[str, str]]:
    """Phase 2c: Sweep known funder RFP/opportunity pages for new grant links."""
    logger.info("=== Phase 2c: Sweeping AI-safety funder grant pages ===")
    all_candidates = []
    for url in GRANT_SOURCE_URLS:
        logger.info("Fetching grant source: %s", url)
        html = fetch_page(url)
        if html:
            links = extract_org_links(html, url)
            logger.info("  Found %d links", len(links))
            all_candidates.extend(links)
        else:
            logger.warning("  Failed to fetch %s", url)
        time.sleep(RATE_LIMIT_DELAY)
    logger.info("Phase 2c total: %d candidate links from funder pages",
                len(all_candidates))
    return all_candidates


def _tag_source(candidates: List[Dict[str, str]], source: str) -> List[Dict[str, str]]:
    """Tag each candidate with where it came from (carried through to the issue)."""
    for c in candidates:
        c.setdefault("source", source)
    return candidates


# ---------------------------------------------------------------------------
# Phase 2d: Web search for new grants (the wide net — noisy, opt-in in review)
# ---------------------------------------------------------------------------

def scrape_websearch_grants(anthropic_key: str, dry_run: bool = False) -> List[Dict[str, str]]:
    """Phase 2d: Ask Claude (with the web-search server tool) for newly-open
    AI-safety grants/RFPs anywhere on the web. This is the wide, noisy net —
    everything it finds is tagged source="web-search" and lands in a separate
    'unvetted' bucket of the weekly GitHub issue for manual opt-in, never
    auto-published. Uses the existing ANTHROPIC_API_KEY; a few queries/week.
    """
    logger.info("=== Phase 2d: Web-searching for new AI-safety grants ===")
    if dry_run:
        logger.info("  [DRY RUN] Skipping web search")
        return []
    if not anthropic_key:
        logger.warning("  ANTHROPIC_API_KEY not set — skipping web search")
        return []

    import anthropic
    client = anthropic.Anthropic(api_key=anthropic_key)
    prompt = (
        "Search the web for AI safety / AI alignment research GRANTS, RFPs, and "
        "funding calls that are currently open or were newly announced in roughly "
        "the last 30 days. Focus on funding for researchers and organizations — "
        "NOT job listings, fellowships, or courses. Prefer official funder pages "
        "over news articles.\n\n"
        "When done searching, respond with ONLY a JSON array (no prose, no "
        'markdown fences) of at most 25 objects: [{"name": "Funder — Program", '
        '"url": "https://..."}]. Use the canonical application or announcement URL.'
    )
    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning("  Web-search call failed (%s) — skipping", e)
        return []

    text = "".join(
        getattr(b, "text", "") for b in response.content
        if getattr(b, "type", "") == "text"
    ).strip()
    text = _extract_json(text)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("  Could not parse web-search JSON (%s) — skipping", e)
        return []

    candidates, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        name = (it.get("name") or "").strip()
        if not url or not name:
            continue
        normalized = normalize_url(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append({"name": name[:150], "url": url})

    logger.info("Phase 2d total: %d candidate grants from web search", len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# Phase 3: Check known org career pages for new programs
# ---------------------------------------------------------------------------

def check_career_pages(
    resources: List[Dict],
    anthropic_key: str,
    dry_run: bool = False,
) -> List[Dict[str, str]]:
    """Phase 3: Check existing org career pages for new program types."""
    logger.info("=== Phase 3: Checking known org career pages ===")
    new_programs = []  # type: List[Dict[str, str]]

    # Find orgs with career-like URLs
    career_keywords = ["career", "job", "hiring", "opportunit", "fellowship", "program"]
    career_orgs = []
    for r in resources:
        url_lower = r["url"].lower()
        if any(kw in url_lower for kw in career_keywords):
            career_orgs.append(r)

    logger.info("Found %d orgs with career-like URLs to check", len(career_orgs))

    if dry_run:
        logger.info("  [DRY RUN] Skipping career page analysis")
        return new_programs

    for r in career_orgs:
        if r.get("js_only", False):
            logger.info("  Skipping JS-only page: %s", r["url"])
            continue

        logger.info("  Checking: %s (%s)", r["name"], r["url"])
        html = fetch_page(r["url"])
        if not html:
            continue

        content = extract_text(html)
        if len(content.strip()) < 50:
            logger.info("    Page content too short, skipping")
            continue

        prompt = CAREER_CHECK_PROMPT.format(
            org_name=r["name"],
            existing_details=r.get("current_details", "N/A"),
            existing_category=r.get("category", "unknown"),
            content=content,
        )

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.content[0].text.strip()
            # Try to parse JSON from the response
            result_text = _extract_json(result_text)
            programs = json.loads(result_text)
            if isinstance(programs, list) and len(programs) > 0:
                logger.info("    Found %d new program(s)!", len(programs))
                new_programs.extend(programs)
            else:
                logger.info("    No new programs detected")
        except json.JSONDecodeError:
            logger.warning("    Failed to parse Claude response as JSON")
        except Exception as e:
            logger.warning("    Error calling Claude API: %s", e)

        time.sleep(RATE_LIMIT_DELAY)

    logger.info("Phase 3 total: %d new programs from career pages", len(new_programs))
    return new_programs


# ---------------------------------------------------------------------------
# Phase 4: Deduplicate against existing resources
# ---------------------------------------------------------------------------

def load_resources(path: str = "resources.json") -> List[Dict]:
    """Load existing resources from JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def build_known_sets(resources: List[Dict]) -> Tuple[Set[str], Set[str], Set[str]]:
    """Build sets of known URLs, domains, and org names for dedup."""
    known_urls = set()  # type: Set[str]
    known_domains = set()  # type: Set[str]
    known_names = set()  # type: Set[str]

    for r in resources:
        known_urls.add(normalize_url(r["url"]))
        known_domains.add(extract_domain(r["url"]))
        known_names.add(r["name"].lower().strip())

    return known_urls, known_domains, known_names


def deduplicate(
    candidates: List[Dict[str, str]],
    known_urls: Set[str],
    known_domains: Set[str],
    known_names: Set[str],
) -> List[Dict[str, str]]:
    """Phase 4: Remove candidates that match existing resources."""
    logger.info("=== Phase 4: Deduplicating against %d known resources ===", len(known_urls))

    unique = []
    seen_normalized = set()  # type: Set[str]

    for candidate in candidates:
        url = candidate.get("url", "")
        name = candidate.get("name", "")
        normalized = normalize_url(url)
        domain = extract_domain(url)

        # Skip if URL already tracked
        if normalized in known_urls:
            continue

        # Skip if we already have this exact URL in our candidate list
        if normalized in seen_normalized:
            continue

        # Skip if same domain is already tracked (same org, different page)
        # But only if the name also partially matches a known name
        if domain in known_domains:
            name_lower = name.lower().strip()
            name_matches = any(
                _fuzzy_name_match(name_lower, kn)
                for kn in known_names
            )
            if name_matches:
                continue

        seen_normalized.add(normalized)
        unique.append(candidate)

    logger.info("After dedup: %d candidates (removed %d)", len(unique), len(candidates) - len(unique))
    return unique


def _fuzzy_name_match(name1: str, name2: str) -> bool:
    """Check if two org names are likely the same."""
    # Exact match
    if name1 == name2:
        return True
    # One contains the other
    if name1 in name2 or name2 in name1:
        return True
    # Check if significant words overlap
    words1 = set(re.findall(r'\w{3,}', name1))
    words2 = set(re.findall(r'\w{3,}', name2))
    if words1 and words2:
        overlap = words1 & words2
        if len(overlap) >= 2:
            return True
        # Single-word overlap with short names
        if len(overlap) >= 1 and (len(words1) <= 2 or len(words2) <= 2):
            # Only match if the overlapping word is substantial
            for word in overlap:
                if len(word) >= 5:
                    return True
    return False


# ---------------------------------------------------------------------------
# Phase 5: Evaluate relevance with Claude
# ---------------------------------------------------------------------------

def evaluate_candidates(
    candidates: List[Dict[str, str]],
    anthropic_key: str,
    dry_run: bool = False,
) -> List[Dict]:
    """Phase 5: Use Claude to evaluate relevance of each candidate."""
    logger.info("=== Phase 5: Evaluating %d candidates with Claude ===", len(candidates))

    if dry_run:
        logger.info("  [DRY RUN] Skipping Claude evaluation")
        # Return candidates with placeholder scores
        results = []
        for c in candidates:
            results.append({
                "name": c.get("name", "Unknown"),
                "url": c.get("url", ""),
                "relevance_score": 0,
                "category": "unknown",
                "description": "[Dry run - not evaluated]",
                "status": "unknown",
                "deadline": None,
                "source": c.get("source", "other"),
            })
        return results

    import anthropic
    client = anthropic.Anthropic(api_key=anthropic_key)
    evaluated = []

    for i, candidate in enumerate(candidates):
        url = candidate.get("url", "")
        name = candidate.get("name", "Unknown")
        logger.info("  [%d/%d] Evaluating: %s (%s)", i + 1, len(candidates), name, url)

        # Fetch the page
        html = fetch_page(url)
        if not html:
            logger.info("    Could not fetch page, skipping")
            time.sleep(RATE_LIMIT_DELAY)
            continue

        content = extract_text(html)
        if len(content.strip()) < 30:
            logger.info("    Page content too short, skipping")
            time.sleep(RATE_LIMIT_DELAY)
            continue

        prompt = RELEVANCE_PROMPT.format(
            url=url,
            name=name,
            content=content,
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.content[0].text.strip()
            result_text = _extract_json(result_text)
            result = json.loads(result_text)

            result["source"] = candidate.get("source", "other")
            score = result.get("relevance_score", 0)
            if score >= MIN_RELEVANCE_SCORE:
                logger.info("    RELEVANT (score=%d): %s", score, result.get("name", name))
                evaluated.append(result)
            else:
                logger.info("    Not relevant enough (score=%d)", score)
        except json.JSONDecodeError:
            logger.warning("    Failed to parse Claude response as JSON")
        except Exception as e:
            logger.warning("    Error calling Claude API: %s", e)

        time.sleep(RATE_LIMIT_DELAY)

    logger.info("Phase 5 total: %d candidates passed relevance filter", len(evaluated))
    return evaluated


def _extract_json(text: str) -> str:
    """Extract JSON from a response that might have markdown fences."""
    # Remove markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


# ---------------------------------------------------------------------------
# Phase 6: Output results
# ---------------------------------------------------------------------------

def save_results(results: List[Dict], output_path: str = "suggested_additions.json") -> None:
    """Phase 6: Save results and generate summary."""
    logger.info("=== Phase 6: Saving results ===")

    output = {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "total_found": len(results),
        "suggestions": results,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("Saved %d suggestions to %s", len(results), output_path)

    # Generate markdown summary
    if results:
        summary = generate_markdown_summary(results)
        logger.info("\n--- SUMMARY ---\n%s", summary)
    else:
        logger.info("No new opportunities found.")


def generate_markdown_summary(results: List[Dict]) -> str:
    """Generate a markdown summary of results grouped by category."""
    # Group by category
    by_category = {}  # type: Dict[str, List[Dict]]
    for r in results:
        cat = r.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(r)

    lines = []
    lines.append("## New AI Safety Opportunities Found\n")
    lines.append("Scan date: {}\n".format(datetime.now(timezone.utc).strftime("%Y-%m-%d")))

    category_labels = {
        "fellowships": "Fellowships",
        "grants": "Grants & Funding",
        "tech-internships": "Technical Internships",
        "policy-internships": "Policy Internships",
        "jobs": "Jobs",
        "community": "Community & Resources",

        "phd": "PhD & Academic",
        "startups": "Startups & Incubators",
        "career-resources": "Career Resources",
        "unknown": "Uncategorized",
    }

    for cat, items in sorted(by_category.items()):
        label = category_labels.get(cat, cat.replace("-", " ").title())
        lines.append("### {}\n".format(label))
        for item in items:
            score = item.get("relevance_score", "?")
            name = item.get("name", "Unknown")
            url = item.get("url", "")
            desc = item.get("description", "No description")
            status = item.get("status", "unknown")
            deadline = item.get("deadline")

            lines.append("- **[{}]({})** (relevance: {}/5)".format(name, url, score))
            lines.append("  - {}".format(desc))
            lines.append("  - Status: {}".format(status))
            if deadline:
                lines.append("  - Deadline: {}".format(deadline))
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for new AI safety opportunities")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Claude API calls; just show what was found from scraping",
    )
    args = parser.parse_args()

    # Check for API key (unless dry run)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not args.dry_run and not anthropic_key:
        logger.error("ANTHROPIC_API_KEY environment variable is required (or use --dry-run)")
        sys.exit(1)

    # Load existing resources
    script_dir = os.path.dirname(os.path.abspath(__file__))
    resources_path = os.path.join(script_dir, "resources.json")
    resources = load_resources(resources_path)
    logger.info("Loaded %d existing resources", len(resources))

    known_urls, known_domains, known_names = build_known_sets(resources)

    # Phase 1: Scrape aggregators
    aggregator_candidates = scrape_aggregators()

    # Phase 2: Scrape forums
    forum_candidates = scrape_forums()

    # Phase 2b: Scan the MATS #opportunities Slack channel
    slack_candidates = scrape_slack_opportunities()

    # Phase 2c: Sweep known funder RFP/opportunity pages for new grants
    grant_candidates = scrape_grant_sources()

    # Phase 2d: Wide web search for new grants (noisy — opt-in bucket in review)
    web_candidates = scrape_websearch_grants(anthropic_key, dry_run=args.dry_run)

    # Phase 3: Check career pages for new programs
    career_candidates = check_career_pages(resources, anthropic_key, dry_run=args.dry_run)

    # Combine all candidates, tagging each with its source (carried to the issue).
    # Higher-signal sources go first so they survive the MAX_TO_EVALUATE cap.
    all_candidates = (
        _tag_source(slack_candidates, "slack")
        + _tag_source(grant_candidates, "grant-page")
        + _tag_source(web_candidates, "web-search")
        + _tag_source(aggregator_candidates, "aggregator")
        + _tag_source(forum_candidates, "forum")
        + _tag_source(career_candidates, "career-page")
    )
    logger.info("Total raw candidates: %d", len(all_candidates))

    # Phase 4: Deduplicate
    unique_candidates = deduplicate(all_candidates, known_urls, known_domains, known_names)

    # Limit to a reasonable number to avoid excessive API calls
    MAX_TO_EVALUATE = 50
    if len(unique_candidates) > MAX_TO_EVALUATE:
        logger.info(
            "Limiting evaluation to %d candidates (from %d)",
            MAX_TO_EVALUATE, len(unique_candidates),
        )
        unique_candidates = unique_candidates[:MAX_TO_EVALUATE]

    # Phase 5: Evaluate with Claude
    results = evaluate_candidates(unique_candidates, anthropic_key, dry_run=args.dry_run)

    # Phase 6: Save results
    output_path = os.path.join(script_dir, "suggested_additions.json")
    save_results(results, output_path)

    logger.info("Scanner complete. Found %d relevant new opportunities.", len(results))


if __name__ == "__main__":
    main()
