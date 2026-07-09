"""Orchestrates the full self-healing pipeline for a CI failure."""

import logging
import os
import shutil
import tempfile

from github_ops import create_pull_request
from llm_client import call_llm
from repo_map import get_repo_map
from repo_ops import apply_fixes, clone_branch, commit_and_push, read_source_files, run_tests
from retrieval import select_context

logger = logging.getLogger("sre-agent-webhook")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def _reset_workdir(workdir: str):
    """Empty the workdir so the branch can be re-cloned pristine."""
    for entry in os.listdir(workdir):
        path = os.path.join(workdir, entry)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


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

        # ── Step 2: Repo map (cached per commit) + tiered retrieval ──────
        try:
            repo_map = get_repo_map(repo, commit_sha, workdir)
            context = select_context(test_logs, repo_map, workdir)
        except Exception as e:
            # Retrieval must never kill a run — fall back to the full repo
            logger.error("Retrieval failed (%s) — falling back to full repo", e)
            context = read_source_files(workdir)

        # ── Step 3+4: Diagnose, apply, validate — escalate once ──────────
        # Attempt 1 = tiered context. If the fix doesn't turn the suite
        # green, attempt 2 retries with the full repo (legacy behavior).
        diagnosis, passed = None, False
        for attempt in (1, 2):
            if attempt == 2:
                if isinstance(context, dict):
                    break  # attempt 1 was already full-repo
                logger.warning("Tiered-context fix failed — escalating to full repo")
                _reset_workdir(workdir)
                try:
                    clone_branch(repo, branch, workdir)
                except Exception as e:
                    logger.error("Re-clone for escalation failed: %s", e)
                    return
                context = read_source_files(workdir)

            try:
                result = call_llm(test_logs, context)
            except Exception as e:
                logger.error("LLM call failed: %s", e)
                return

            diagnosis = result.get("diagnosis", "unknown")
            fixes = result.get("fixes", [])
            logger.info("Attempt %d | Diagnosis: %s", attempt, diagnosis)
            logger.info("Files to fix: %s", [f["filename"] for f in fixes])

            if not fixes:
                logger.warning("LLM returned no fixes — cannot proceed")
                return

            apply_fixes(workdir, fixes)
            passed, test_output = run_tests(workdir)
            if passed:
                break

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
