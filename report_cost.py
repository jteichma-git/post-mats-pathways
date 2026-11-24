"""
Post this run's Claude API cost to the "API cost tracker" GitHub issue.

GitHub emails a comment on that issue to everyone watching the repo, so John
gets one short email per workflow run. A spike gets a warning in the title
line so it stands out in the inbox.

Usage (in a workflow, after the main step, with `if: always()`):
    python report_cost.py --workflow crawl --jobs crawler reviewer
Needs GH_TOKEN (and GITHUB_RUN_ID / GITHUB_REPOSITORY, which Actions sets).
Without GH_TOKEN it just prints the report, for local runs.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ISSUE_TITLE = "API cost tracker"
LABEL = "api-cost"

# A run is a spike if it costs more than SPIKE_ABS dollars, or more than
# SPIKE_RATIO times the median of the last few runs (ignoring tiny baselines).
SPIKE_ABS = 1.00
SPIKE_RATIO = 3.0
BASELINE_RUNS = 4
MIN_BASELINE = 0.05


def load_job(job):
    path = BASE / f"run_cost_{job}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_history(path):
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def append_history(path, row):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def spike_reason(total, history):
    if total > SPIKE_ABS:
        return f"over the ${SPIKE_ABS:.2f} per-run alert line"
    recent = [float(r["cost_usd"]) for r in history[-BASELINE_RUNS:]]
    if recent:
        base = max(statistics.median(recent), MIN_BASELINE)
        if total > SPIKE_RATIO * base:
            return f"{total / base:.0f}x the recent median (${base:.2f})"
    return None


def gh(*args):
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def find_or_create_issue():
    found = gh(
        "issue", "list", "--label", LABEL, "--state", "open",
        "--json", "number", "--jq", ".[0].number",
    )
    if found:
        return found
    subprocess.run(
        ["gh", "label", "create", LABEL, "--color", "D93F0B",
         "--description", "Weekly Claude API spend"],
        capture_output=True, text=True,
    )  # fine if it already exists
    url = gh(
        "issue", "create", "--title", ISSUE_TITLE, "--label", LABEL,
        "--body",
        "Each weekly workflow run posts its Claude API cost here as a comment, "
        "so it arrives by email. Runs that spike are marked with a warning.\n\n"
        "Keep this issue open; closing it starts a fresh one next run.",
    )
    return url.rstrip("/").split("/")[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True, help="crawl or scan")
    ap.add_argument("--jobs", nargs="+", required=True)
    ap.add_argument("--status", default="", help="outcome of the main step")
    args = ap.parse_args()

    jobs = [j for j in (load_job(name) for name in args.jobs) if j]
    total = sum(j["cost_usd"] for j in jobs)
    calls = sum(j["calls"] for j in jobs)
    searches = sum(j["web_searches"] for j in jobs)

    hist_path = BASE / f"cost_history_{args.workflow}.csv"
    history = load_history(hist_path)
    spike = spike_reason(total, history)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY"):
        run_url = (
            f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )

    name = {"crawl": "Weekly update (crawler)", "scan": "Weekly scan"}.get(
        args.workflow, args.workflow
    )
    head = f"**{name}, {today}: ${total:.2f}**"
    if spike:
        head = f"⚠️ **COST SPIKE: {name}, {today}: ${total:.2f}** ({spike})"
    lines = [head, ""]
    if args.status and args.status != "success":
        lines += [f"The run itself ended in **{args.status}**.", ""]
    lines += [f"{calls} Claude calls" + (f", {searches} web searches" if searches else "")]
    for j in jobs:
        for model, m in sorted(j["by_model"].items()):
            lines.append(f"- {j['job']}: {model}: {m['calls']} calls, ${m['cost_usd']:.3f}")
    if not jobs:
        lines.append("- No cost data (the step stopped before calling Claude).")
    if history:
        recent = ", ".join(f"${float(r['cost_usd']):.2f}" for r in history[-BASELINE_RUNS:])
        lines += ["", f"Previous runs: {recent}"]
    if run_url:
        lines += ["", f"[Run log]({run_url})"]
    body = "\n".join(lines)
    print(body)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(body + "\n")

    append_history(hist_path, {
        "date": today,
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "calls": calls,
        "web_searches": searches,
        "cost_usd": f"{total:.4f}",
        "spike": "yes" if spike else "",
    })

    if os.environ.get("GH_TOKEN"):
        try:
            number = find_or_create_issue()
            gh("issue", "comment", str(number), "--body", body)
        except subprocess.CalledProcessError as e:
            print(f"Could not post to the cost issue: {e.stderr}", file=sys.stderr)


if __name__ == "__main__":
    main()
