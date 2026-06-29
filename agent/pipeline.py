"""Orchestrates the full self-healing pipeline for a CI failure."""

import logging
import os
import tempfile

from github_ops import create_pull_request
from llm_client import call_llm
from repo_ops import apply_fixes, clone_branch, commit_and_push, read_source_files, run_tests

logger = logging.getLogger("sre-agent-webhook")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def run(repo: str, branch: str, commit_sha: str, test_logs: str):
    """
    Full self-healing pipeline:
      1. Clone the failing branch
      2. Read its source files
      3. Ask the LLM to diagnose and fix the failure
      4. Apply the fix to the clone
      5. Run tests — if they pass, push a fix branch and open a PR
    """
    logger.info("=== Pipeline started | branch=%s commit=%s ===", branch, commit_sha)

    # Use a temp directory so each pipeline run is fully isolated.
    # tempfile.mkdtemp creates a fresh directory and returns its path.
    # We delete it at the end so the VM disk doesn't fill up over time.
    with tempfile.TemporaryDirectory() as workdir:
        # ── Step 1: Clone ────────────────────────────────────────────────
        try:
            clone_branch(repo, branch, workdir)
        except Exception as e:
            logger.error("Clone failed: %s", e)
            return

        # ── Step 2: Read source files ────────────────────────────────────
        source_files = read_source_files(workdir)
        logger.info("Read %d source files from clone", len(source_files))

        # ── Step 3: Call LLM ─────────────────────────────────────────────
        try:
            result = call_llm(test_logs, source_files)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return

        diagnosis = result.get("diagnosis", "unknown")
        fixes = result.get("fixes", [])
        logger.info("Diagnosis: %s", diagnosis)
        logger.info("Files to fix: %s", [f["filename"] for f in fixes])

        if not fixes:
            logger.warning("LLM returned no fixes — cannot proceed")
            return

        # ── Step 4: Apply fix and validate ───────────────────────────────
        apply_fixes(workdir, fixes)
        passed, test_output = run_tests(workdir)

        if not passed:
            logger.error("Fix did not resolve the failure — not pushing")
            return

        # ── Step 5: Push fix branch and open PR ──────────────────────────
        if not GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not set — skipping push and PR")
            return

        # Name the fix branch after the original branch so it's obvious where it came from
        safe_branch = branch.replace("/", "-")
        fix_branch = f"autofix/{safe_branch}-{commit_sha[:7]}"

        try:
            commit_and_push(workdir, fix_branch, GITHUB_TOKEN, repo)
        except Exception as e:
            logger.error("Push failed: %s", e)
            return

        try:
            pr_url = create_pull_request(
                token=GITHUB_TOKEN,
                repo=repo,
                head=fix_branch,
                base="main",
                title=f"[Auto-fix] {diagnosis}",
                body=(
                    f"**Branch:** `{branch}`\n"
                    f"**Commit:** `{commit_sha}`\n\n"
                    f"**Diagnosis:** {diagnosis}\n\n"
                    f"This fix was generated automatically by the SRE Agent and "
                    f"verified by running the full test suite locally before pushing."
                ),
            )
            logger.info("=== Pipeline complete | PR: %s ===", pr_url)
        except Exception as e:
            logger.error("PR creation failed: %s", e)
