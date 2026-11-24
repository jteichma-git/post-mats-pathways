"""
Count what each run spends on the Claude API.

Why this exists: in Nov 2026 an Opus-based pipeline used up a $20 top-up in two
days and nobody noticed until the credits ran out. Now every Claude call is
priced as it happens, and report_cost.py posts the total for each run to a
GitHub issue (which emails John), flagging spikes.

Usage: client = cost_tracker.track(anthropic.Anthropic(...), job="crawler")
Every messages.create() on that client is then counted. Totals are written to
run_cost_<job>.json when the process exits (including on a failed run).
"""

import atexit
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# US$ per million tokens: (input, output). Anthropic list prices, Nov 2026.
# Cache writes bill at 1.25x input, cache reads at 0.1x input.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),   # prompts <=100K tokens; ours are ~3K
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
}
UNKNOWN_MODEL_PRICE = (5.00, 25.00)  # assume the worst rather than report $0
WEB_SEARCH_PRICE = 10.00 / 1000      # $10 per 1,000 searches

_totals = {}  # job -> dict
OUT_DIR = Path(__file__).resolve().parent


def _price_for(model):
    for key, price in PRICES.items():
        if model and model.startswith(key):
            return price
    return UNKNOWN_MODEL_PRICE


def add(job, message):
    """Add one API response's usage to the job's running total."""
    u = getattr(message, "usage", None)
    if u is None:
        return
    model = getattr(message, "model", "") or ""
    p_in, p_out = _price_for(model)
    inp = getattr(u, "input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

    cost = (
        inp * p_in + out * p_out + cw * p_in * 1.25 + cr * p_in * 0.1
    ) / 1_000_000 + searches * WEB_SEARCH_PRICE

    t = _totals.setdefault(job, {
        "job": job, "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "web_searches": 0, "cost_usd": 0.0, "by_model": {},
    })
    t["calls"] += 1
    t["input_tokens"] += inp + cw + cr
    t["output_tokens"] += out
    t["web_searches"] += searches
    t["cost_usd"] += cost
    m = t["by_model"].setdefault(model, {"calls": 0, "cost_usd": 0.0})
    m["calls"] += 1
    m["cost_usd"] += cost


def track(client, job):
    """Wrap client.messages.create so every successful call is priced."""
    original = client.messages.create

    def create(*args, **kwargs):
        message = original(*args, **kwargs)
        try:
            add(job, message)
        except Exception as e:  # never let accounting break a run
            logger.warning("cost_tracker: could not price a call: %s", e)
        return message

    client.messages.create = create
    _totals.setdefault(job, {
        "job": job, "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "web_searches": 0, "cost_usd": 0.0, "by_model": {},
    })
    return client


@atexit.register
def _write():
    for job, t in _totals.items():
        t["cost_usd"] = round(t["cost_usd"], 4)
        for m in t["by_model"].values():
            m["cost_usd"] = round(m["cost_usd"], 4)
        path = OUT_DIR / f"run_cost_{job}.json"
        path.write_text(json.dumps(t, indent=2))
        logger.info(
            "API cost for %s: $%.4f over %d calls", job, t["cost_usd"], t["calls"]
        )
