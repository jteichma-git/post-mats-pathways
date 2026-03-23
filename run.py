#!/usr/bin/env python3
"""
run.py - Orchestrator that runs crawler.py then updater.py,
and optionally commits/pushes changes.
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
REPORT_FILE = BASE_DIR / "change_report.json"


def run_command(cmd, cwd=None, timeout=None):
    """Run a command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        cwd=cwd or BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def has_git_changes() -> bool:
    """Check if there are uncommitted changes in the repo."""
    code, stdout, _ = run_command(["git", "status", "--porcelain"])
    if code != 0:
        return False
    return bool(stdout.strip())


def git_commit_and_push() -> bool:
    """Commit and push changes. Returns True on success."""
    # Stage the relevant files
    files_to_stage = [
        "resources.json",
        "index.html",
        "directory.html",
        "change_report.json",
    ]
    for f in files_to_stage:
        filepath = BASE_DIR / f
        if filepath.exists():
            code, _, stderr = run_command(["git", "add", str(filepath)])
            if code != 0:
                logger.error(f"Failed to stage {f}: {stderr}")
                return False

    # Check if there's anything to commit
    code, stdout, _ = run_command(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.info("No staged changes to commit.")
        return True

    # Load report for commit message details
    changes_summary = ""
    if REPORT_FILE.exists():
        with open(REPORT_FILE, "r") as f:
            report = json.load(f)
        changed = [r for r in report if r.get("action") == "changed"]
        if changed:
            changes_summary = "\n\nChanges detected:\n"
            for c in changed:
                changes_summary += f"- {c['name']}: {c['old_status']} -> {c['new_status']}"
                if c.get("new_deadline"):
                    changes_summary += f" (deadline: {c['new_deadline']})"
                changes_summary += "\n"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = f"Auto-update resource statuses ({now}){changes_summary}"

    code, _, stderr = run_command(["git", "commit", "-m", commit_msg])
    if code != 0:
        logger.error(f"Git commit failed: {stderr}")
        return False
    logger.info("Changes committed.")

    code, _, stderr = run_command(["git", "push"])
    if code != 0:
        logger.error(f"Git push failed: {stderr}")
        return False
    logger.info("Changes pushed to remote.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run the resource crawler and updater pipeline"
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit and push changes to git if updates are detected",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying any files",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Run cross-check reviewer after updating (uses Claude Sonnet)",
    )
    args = parser.parse_args()

    # Step 1: Run crawler
    logger.info("=" * 60)
    logger.info("STEP 1: Running crawler...")
    logger.info("=" * 60)

    crawler_args = [sys.executable, str(BASE_DIR / "crawler.py")]
    if args.dry_run:
        crawler_args.append("--dry-run")

    code, stdout, stderr = run_command(crawler_args)
    print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    if code != 0:
        logger.error(f"Crawler failed with exit code {code}")
        sys.exit(1)

    # Step 2: Run updater
    logger.info("=" * 60)
    logger.info("STEP 2: Running updater...")
    logger.info("=" * 60)

    updater_args = [sys.executable, str(BASE_DIR / "updater.py")]
    if args.dry_run:
        updater_args.append("--dry-run")

    code, stdout, stderr = run_command(updater_args)
    print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    if code != 0:
        logger.error(f"Updater failed with exit code {code}")
        sys.exit(1)

    # Step 3: Optionally run cross-check reviewer
    if args.review:
        logger.info("=" * 60)
        logger.info("STEP 3: Running cross-check reviewer...")
        logger.info("=" * 60)

        reviewer_args = [sys.executable, str(BASE_DIR / "reviewer.py")]
        if args.dry_run:
            reviewer_args.append("--dry-run")

        code, stdout, stderr = run_command(reviewer_args, timeout=600)
        print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        if code != 0:
            logger.warning(f"Reviewer finished with exit code {code} (non-fatal)")

    # Step 4: Optionally commit and push
    if args.commit and not args.dry_run:
        logger.info("=" * 60)
        logger.info("STEP 4: Checking for changes to commit...")
        logger.info("=" * 60)

        if has_git_changes():
            logger.info("Changes detected, committing and pushing...")
            success = git_commit_and_push()
            if not success:
                logger.error("Failed to commit/push changes")
                sys.exit(1)
        else:
            logger.info("No changes to commit.")
    elif args.commit and args.dry_run:
        logger.info("\n[DRY RUN] Would commit and push changes if --commit was used without --dry-run")
    else:
        if has_git_changes():
            logger.info("\nNote: Changes were made but not committed. Use --commit to auto-commit.")

    logger.info("\nPipeline complete.")


if __name__ == "__main__":
    main()
