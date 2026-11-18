"""
Make Claude API outages fail the GitHub workflow instead of passing silently.

Why this exists: from 5 Oct to 18 Nov 2026 the Anthropic account was out of
credit. Every Claude call failed, the scripts logged a warning and carried on,
and both weekly workflows stayed green for six weeks while the site quietly
stopped updating.

Usage: call record(ok, error) after every Claude API call, and check() before
saving results. A billing/auth error exits immediately; otherwise check()
exits if too many calls failed.
"""

import logging
import sys

logger = logging.getLogger(__name__)

# Errors that mean no further call can succeed — stop at the first one.
FATAL_MARKERS = (
    "credit balance",
    "billing",
    "invalid x-api-key",
    "authentication_error",
    "permission_error",
)

calls = 0
failures = 0


def record(ok, error=None):
    """Record one Claude API call. Exits at once on a billing/auth error."""
    global calls, failures
    calls += 1
    if ok:
        return
    failures += 1
    if error is not None and any(m in str(error).lower() for m in FATAL_MARKERS):
        logger.error("Claude API is unusable — failing the run: %s", error)
        sys.exit(2)


def check(min_failures=5, max_ratio=0.25):
    """Exit non-zero if a meaningful share of Claude calls failed."""
    if failures >= min_failures and failures > max_ratio * calls:
        logger.error(
            "%d of %d Claude API calls failed — failing the run so it shows red",
            failures, calls,
        )
        sys.exit(2)
