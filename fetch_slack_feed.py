#!/usr/bin/env python3
"""
fetch_slack_feed.py — Mirrors the last N days of the MATS #opportunities
Slack channel into the "Jobs" block of index.html / directory.html.

This replaces the old 80,000 Hours live-jobs feed. The #opportunities channel
is hand-curated by John and carries lots of roles, fellowships, funding and
collaboration asks that never appear on the 80K board — so we simply mirror it.

Production (in CI): reads messages via the Slack Web API (conversations.history)
using the SLACK_BOT_TOKEN environment variable. The bot must be a member of the
channel and have the channels:history + channels:read scopes.

Local preview / testing: render from a saved JSON dump instead of calling Slack:
    python fetch_slack_feed.py --from-json some_dump.json
where the dump is {"messages": [{"ts": "...", "text": "...", ...}, ...]}
(the same shape conversations.history returns).

Writes slack_feed.json with the raw messages actually rendered.

HTML markers (must already exist in both pages):
    <!-- slack-feed:start -->
    ...generated content...
    <!-- slack-feed:end -->

Cards use <div class="opp opp-live"> so the renderer (which matches
class="opp" EXACTLY) leaves these blocks alone.
"""

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
OUTPUT_JSON = BASE_DIR / "slack_feed.json"
INDEX_FILE = BASE_DIR / "index.html"
DIRECTORY_FILE = BASE_DIR / "directory.html"

# MATS workspace #opportunities channel.
CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C05LWT1UG67")
WORKSPACE = os.environ.get("SLACK_WORKSPACE", "mats-program")

WINDOW_DAYS = 7

# Skip context-free link drops / one-word posts: if the message has fewer than
# this many characters of actual prose (after stripping links, emoji and
# markup) we treat it as noise and leave it off the page.
MIN_TEXT_CHARS = 15

START_MARKER = "<!-- slack-feed:start -->"
END_MARKER = "<!-- slack-feed:end -->"

SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"

# Subtypes that are channel housekeeping, not opportunities.
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "pinned_item",
    "bot_add", "bot_remove", "tombstone",
}

# A small map of the emoji shortcodes that actually show up in this channel.
# Unknown / custom emoji (e.g. :mats-torch:) are simply dropped.
EMOJI = {
    "rotating_light": "\U0001F6A8", "date": "\U0001F4C5", "calendar": "\U0001F4C5",
    "alarm_clock": "⏰", "spiral_note_pad": "\U0001F5D2️",
    "spiral_calendar_pad": "\U0001F5D3️", "moneybag": "\U0001F4B0",
    "white_check_mark": "✅", "link": "\U0001F517", "star2": "\U0001F31F",
    "fire": "\U0001F525", "dart": "\U0001F3AF", "trophy": "\U0001F3C6",
    "round_pushpin": "\U0001F4CD", "memo": "\U0001F4DD", "microphone": "\U0001F3A4",
    "teacher": "\U0001F9D1‍\U0001F3EB", "pray": "\U0001F64F",
    "bridge_at_night": "\U0001F309", "muscle": "\U0001F4AA", "rocket": "\U0001F680",
    "heart": "❤️", "tada": "\U0001F389", "bulb": "\U0001F4A1",
    "handshake": "\U0001F91D", "sparkles": "✨", "mega": "\U0001F4E3",
    "loudspeaker": "\U0001F4E2", "clipboard": "\U0001F4CB", "+1": "\U0001F44D",
    "earth_americas": "\U0001F30E", "globe_with_meridians": "\U0001F310",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Slack markup (mrkdwn) -> HTML
# --------------------------------------------------------------------------- #

def _attr_escape(text):
    text = html.unescape(text or "")
    return (text.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _text_escape(text):
    text = html.unescape(text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _display_url(url):
    u = re.sub(r"^https?://", "", url)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def _render_token(content):
    """Render one Slack <...> control sequence to HTML."""
    if content.startswith("@"):  # user mention <@U123|Name>
        body = content[1:]
        name = body.split("|", 1)[1] if "|" in body else "someone"
        return _text_escape(name)
    if content.startswith("#"):  # channel mention <#C123|name>
        body = content[1:]
        name = body.split("|", 1)[1] if "|" in body else "channel"
        return "#" + _text_escape(name)
    if content.startswith("!"):  # <!here>, <!channel>, <!subteam...>
        body = content[1:]
        label = body.split("|", 1)[1] if "|" in body else body
        return "@" + _text_escape(label)
    if content.startswith("mailto:"):
        body = content[len("mailto:"):]
        addr, label = (body.split("|", 1) + [None])[:2]
        label = label or addr
        return '<a href="mailto:{}">{}</a>'.format(_attr_escape(addr), _text_escape(label))
    # plain link <url|label> or <url>
    url, label = (content.split("|", 1) + [None])[:2]
    label = label or _display_url(url)
    return '<a href="{}" target="_blank" rel="noopener">{}</a>'.format(
        _attr_escape(url), _text_escape(label)
    )


def _emoji_sub(match):
    return EMOJI.get(match.group(1), "")


def _inline(text):
    """Format the inline content of a single line (no block-level newlines)."""
    tokens = []

    def grab(m):
        tokens.append(_render_token(m.group(1)))
        return "\x00{}\x00".format(len(tokens) - 1)

    # 1. pull out <...> control sequences so their URLs are never touched again
    text = re.sub(r"<([^<>\n]+)>", grab, text)
    # 2. normalise + escape the remaining human text
    text = _text_escape(text)
    # 3. emoji shortcodes
    text = re.sub(r":([a-z0-9_+\-]+):", _emoji_sub, text)
    # 4. inline styling
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*(?=\S)(.+?)(?<=\S)\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![A-Za-z0-9])_(?=\S)(.+?)(?<=\S)_(?![A-Za-z0-9])",
                  r"<em>\1</em>", text)
    # 5. restore the links/mentions
    text = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)
    return text


_QUOTE_RE = re.compile(r"^\s*(?:>|&gt;)\s?(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[•‣▪◦]\s*|[-*]\s+|\d+[.)]\s+)(.*)$")
_SEP_RE = re.compile(r"^\s*(?:~~~|---|\*\*\*|___)\s*$")


def mrkdwn_to_html(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    blocks, para, quote, bullets = [], [], [], []

    def flush_para():
        if para:
            blocks.append("<p>" + "<br>".join(_inline(l) for l in para) + "</p>")
            para.clear()

    def flush_bullets():
        if bullets:
            blocks.append("<ul>" + "".join("<li>" + _inline(b) + "</li>"
                                           for b in bullets) + "</ul>")
            bullets.clear()

    def flush_quote():
        if quote:
            inner = mrkdwn_to_html("\n".join(quote))
            blocks.append("<blockquote>" + inner + "</blockquote>")
            quote.clear()

    def flush_all():
        flush_para(); flush_bullets(); flush_quote()

    for line in lines:
        if _SEP_RE.match(line):
            flush_all()
            continue
        qm = _QUOTE_RE.match(line)
        if qm and (line.lstrip().startswith(">") or line.lstrip().startswith("&gt;")):
            flush_para(); flush_bullets()
            quote.append(qm.group(1))
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            flush_para(); flush_quote()
            bullets.append(bm.group(1))
            continue
        if not line.strip():
            flush_all()
            continue
        flush_bullets(); flush_quote()
        para.append(line)

    flush_all()
    out = "\n".join(b for b in blocks if b)
    # merge adjacent lists that got split by blank quote lines
    out = out.replace("</ul>\n<ul>", "")
    return out


# --------------------------------------------------------------------------- #
# Noise filtering
# --------------------------------------------------------------------------- #

def _prose_length(text):
    t = re.sub(r"<[^<>\n]+>", "", text or "")       # drop control sequences
    t = re.sub(r":[a-z0-9_+\-]+:", "", t)           # drop emoji shortcodes
    t = re.sub(r'[*_~`>•"\s]', "", t)          # drop markup + whitespace
    return len(t)


def is_noise(msg):
    text = msg.get("text", "")
    if not text.strip():
        return True
    if "This message was deleted" in text:
        return True
    return _prose_length(text) < MIN_TEXT_CHARS


def keep_message(msg):
    # Default to "message" when absent so the script's own slack_feed.json
    # output (which stores only ts/user/text) can be re-read via --from-json.
    if msg.get("type", "message") != "message":
        return False
    if msg.get("subtype") in SKIP_SUBTYPES:
        return False
    if msg.get("user") == "USLACKBOT" or msg.get("subtype") == "bot_message":
        return False
    if msg.get("hidden"):
        return False
    return not is_noise(msg)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_messages_from_slack(token, channel, oldest):
    messages = []
    cursor = None
    while True:
        params = {"channel": channel, "oldest": "{:.6f}".format(oldest),
                  "limit": 200, "inclusive": "false"}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            SLACK_HISTORY_URL,
            headers={"Authorization": "Bearer {}".format(token)},
            params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError("Slack API error: {}".format(data.get("error")))
        messages.extend(data.get("messages", []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return messages


def load_messages_from_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("messages", [])
    return data


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _posted_date(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%b %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""


def _permalink(ts):
    try:
        p = "p" + str(ts).replace(".", "")
    except Exception:
        return None
    return "https://{ws}.slack.com/archives/{ch}/{p}".format(
        ws=WORKSPACE, ch=CHANNEL_ID, p=p
    )


def render_card(msg, variant):
    body = mrkdwn_to_html(msg.get("text", ""))
    date = _posted_date(msg.get("ts"))
    meta = date + " · #opportunities" if date else "#opportunities"
    return "\n".join([
        '<div class="opp opp-live">',
        '<div class="slack-msg">{}</div>'.format(body),
        '<div class="slack-meta">{}</div>'.format(_text_escape(meta)),
        '</div>',
    ])


def render_feed(messages, variant):
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    lines = [START_MARKER]
    if not messages:
        lines.append(
            '<p class="live-jobs-empty">No new posts in the MATS '
            '#opportunities channel in the past {days} days '
            '(last synced {today}).</p>'.format(days=WINDOW_DAYS, today=today)
        )
        lines.append(END_MARKER)
        return "\n".join(lines)

    n = len(messages)
    # The index page shows this feed on the back of a flip-card whose FRONT
    # already states the source and cadence, so the header would be redundant
    # (and cramped). Only the full directory page gets the intro header.
    if variant != "index":
        lines.append(
            '<div class="live-jobs-header"><strong>From the MATS '
            '#opportunities channel</strong> — {n} post{s} shared in the past '
            '{days} days · last synced {today}. Hand-curated roles, '
            'fellowships, funding and collaborations.</div>'.format(
                n=n, s="" if n == 1 else "s", days=WINDOW_DAYS, today=today
            )
        )
    for msg in messages:
        lines.append(render_card(msg, variant))
    lines.append(END_MARKER)
    return "\n".join(lines)


def patch_file(filepath, html_block):
    content = filepath.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL
    )
    if not pattern.search(content):
        logger.warning(
            "  No slack-feed markers in %s — skipping. Add %s and %s to enable.",
            filepath.name, START_MARKER, END_MARKER,
        )
        return
    new_content = pattern.sub(lambda _: html_block, content)
    if new_content != content:
        filepath.write_text(new_content, encoding="utf-8")
        logger.info("  Patched %s (%d bytes)", filepath.name, len(html_block))
    else:
        logger.info("  %s already up to date", filepath.name)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-json", metavar="PATH",
                        help="Render from a saved conversations.history dump "
                             "instead of calling the Slack API (for preview/testing).")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = parser.parse_args()

    if args.from_json:
        logger.info("Loading messages from %s", args.from_json)
        raw = load_messages_from_json(args.from_json)
    else:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            logger.error("SLACK_BOT_TOKEN not set — cannot fetch from Slack.")
            sys.exit(1)
        oldest = datetime.now(timezone.utc).timestamp() - args.window_days * 86400
        logger.info("Fetching #opportunities (%s) messages from the past %d days...",
                    CHANNEL_ID, args.window_days)
        raw = fetch_messages_from_slack(token, CHANNEL_ID, oldest)
        logger.info("Got %d raw messages", len(raw))

    kept = [m for m in raw if keep_message(m)]
    # newest first
    kept.sort(key=lambda m: float(m.get("ts", 0)), reverse=True)
    logger.info("Rendering %d messages (%d dropped as noise/system)",
                len(kept), len(raw) - len(kept))

    OUTPUT_JSON.write_text(
        json.dumps({
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "channel": CHANNEL_ID,
            "window_days": args.window_days,
            "count": len(kept),
            "messages": [
                {"ts": m.get("ts"), "user": m.get("user"), "text": m.get("text", "")}
                for m in kept
            ],
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote %s", OUTPUT_JSON.name)

    patch_file(INDEX_FILE, render_feed(kept, "index"))
    patch_file(DIRECTORY_FILE, render_feed(kept, "directory"))


if __name__ == "__main__":
    main()
