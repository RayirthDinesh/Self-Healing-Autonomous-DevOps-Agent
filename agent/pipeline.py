"""Orchestrates the full self-healing pipeline for a CI failure."""

import logging
import os
import re
import shutil
import tempfile

import memory
import run_tracker
from github_ops import create_pull_request
from llm_client import call_llm
from repo_map import get_repo_map
from repo_ops import (
    apply_fixes, clone_branch, commit_and_push, get_diff,
    read_source_files, run_static_analysis, run_tests,
)
from retrieval import select_context

logger = logging.getLogger("sre-agent-webhook")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def _extract_failed_tests(output: str) -> set:
    """Parse pytest output for FAILED test IDs."""
    return set(re.findall(r"FAILED\s+([\w/.\\-]+::[\w_\[\]-]+)", output))


def _augment_logs_with_regression(original_logs: str, fix_output: str) -> str:
    """Extend the original failure log with what went wrong after the first fix attempt."""
    original_failures = _extract_failed_tests(original_logs)
    new_failures = _extract_failed_tests(fix_output)
    regressions = new_failures - original_failures

    if regressions:
        reg_list = "\n".join(f"  - {t}" for t in sorted(regressions))
        header = (
            "\n\n## YOUR PREVIOUS FIX WAS APPLIED BUT CAUSED REGRESSIONS\n"
            f"These tests were passing before your fix and now fail:\n{reg_list}\n\n"
            "You must fix the original failure WITHOUT breaking these previously passing tests.\n\n"
            f"Full output after your fix:\n{fix_output[-3000:]}"
        )
    else:
        header = (
            "\n\n## YOUR PREVIOUS FIX DID NOT RESOLVE THE FAILURE\n"
            f"Output after applying your fix:\n{fix_output[-3000:]}"
        )
    return original_logs + header


def _track_context(repo_map, context):
    """Feed the dashboard's codebase-map panel from the legacy retrieval step."""
    if repo_map:
        run_tracker.artifact("repo_map", "codebase map",
                             run_tracker.repo_map_digest(repo_map))
    if isinstance(context, dict):
        detail = {"mode": "full-repo", "files": sorted(context)}
    else:
        detail = {"mode": "tiered", "files": sorted(getattr(context, "full", []) or [])}
    run_tracker.step("retrieval", detail=detail)


def _reset_workdir(workdir: str):
    """Empty the workdir so the branch can be re-cloned pristine."""
    for entry in os.listdir(workdir):
        path = os.path.join(workdir, entry)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


def run(repo: str, branch: str, commit_sha: str, test_logs: str):
    """Dispatch: AGENT_MODE=legacy -> single-shot pipeline, else the graph.

    The graph is the default because it is what the docs, the console and every
    screenshot describe. Leaving legacy as the default meant a fresh clone
    silently ran a different pipeline (no router, triage, localizer or critic)
    than the one being demonstrated.
    """
    try:
        if os.getenv("AGENT_MODE", "graph").lower() != "legacy":
            try:
                from agent_graph import run_graph
            except Exception as e:
                logger.error("LangGraph unavailable (%s) - falling back to the "
                             "legacy pipeline; run pip install -r requirements.txt", e)
            else:
                return run_graph(repo, branch, commit_sha, test_logs)
        return _run_legacy(repo, branch, commit_sha, test_logs)
    finally:
        # A crash (or an early return that missed its finish_run) must not leave
        # the dashboard showing a run that is still "running".
        run_tracker.close_if_running(outcome="pipeline crashed")


def _run_legacy(repo: str, branch: str, commit_sha: str, test_logs: str):
    """
    Full self-healing pipeline:
      1. Clone the failing branch
      2. Read its source files
      3. Ask the LLM to diagnose and fix the failure
      4. Apply the fix to the clone
      5. Run tests - if they pass, push a fix branch and open a PR
    """
    logger.info("=== Pipeline started | branch=%s commit=%s ===", branch, commit_sha)
    run_tracker.start_run(repo, branch, commit_sha, source="webhook", mode="legacy")
    run_tracker.artifact("ci_logs", "failing CI output", test_logs)

    # Use a temp directory so each pipeline run is fully isolated.
    # tempfile.mkdtemp creates a fresh directory and returns its path.
    # We delete it at the end so the VM disk doesn't fill up over time.
    with tempfile.TemporaryDirectory() as workdir:
        # ── Step 1: Clone ────────────────────────────────────────────────
        try:
            clone_branch(repo, branch, workdir)
        except Exception as e:
            logger.error("Clone failed: %s", e)
            run_tracker.step("clone", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "clone_failed")
            return
        run_tracker.step("clone", detail={"workdir": workdir})

        # ── Step 1.5: Memory - resolve past PR fates, sync the mental map ─
        # (memory functions never raise; they degrade to neutral values)
        memory.update_pr_fates(repo, GITHUB_TOKEN)
        error_class = memory.classify_error(test_logs)
        blame = memory.blame_scores(repo, error_class)
        incidents = memory.similar_incidents(repo, test_logs)
        if incidents:
            logger.info("Memory: %d similar past incident(s) added to prompt", len(incidents))
        run_tracker.update_run(error_class=error_class)
        run_tracker.step("ingest", detail={"error_class": error_class,
                                           "blame_files": len(blame),
                                           "few_shots": len(incidents)})

        # ── Step 2: Repo map (cached per commit) + tiered retrieval ──────
        try:
            repo_map = get_repo_map(repo, commit_sha, workdir)
            memory.update_repo_state(repo, commit_sha, workdir, repo_map)
            context = select_context(test_logs, repo_map, workdir, blame=blame)
        except Exception as e:
            # Retrieval must never kill a run - fall back to the full repo
            logger.error("Retrieval failed (%s) - falling back to full repo", e)
            repo_map, context = None, read_source_files(workdir)
        _track_context(repo_map, context)

        # ── Step 3+4: Diagnose, apply, validate - escalate once ──────────
        # Attempt 1 = tiered context. If the fix doesn't turn the suite
        # green, attempt 2 retries with the full repo (legacy behavior).
        diagnosis, passed, incident_id = None, False, None
        test_output, active_logs = "", test_logs
        for attempt in (1, 2):
            if attempt == 2:
                # Tell the LLM what went wrong with its first fix
                active_logs = _augment_logs_with_regression(test_logs, test_output)
                if isinstance(context, dict):
                    break  # attempt 1 was already full-repo
                logger.warning("Tiered-context fix failed - escalating to full repo with regression feedback")
                _reset_workdir(workdir)
                try:
                    clone_branch(repo, branch, workdir)
                except Exception as e:
                    logger.error("Re-clone for escalation failed: %s", e)
                    run_tracker.finish_run("error", "reclone_failed", attempts=1)
                    return
                context = read_source_files(workdir)

            run_tracker.set_current_node("fixer")
            try:
                result = call_llm(active_logs, context, incidents=incidents)
            except Exception as e:
                logger.error("LLM call failed: %s", e)
                run_tracker.step("fixer", status="error", detail={"error": str(e)})
                run_tracker.finish_run("error", "llm_failed", attempts=attempt)
                return

            diagnosis = result.get("diagnosis", "unknown")
            fixes = result.get("fixes", [])
            logger.info("Attempt %d | Diagnosis: %s", attempt, diagnosis)
            logger.info("Files to fix: %s", [f["filename"] for f in fixes])
            run_tracker.step("fixer", detail={
                "attempt": attempt, "diagnosis": diagnosis,
                "files": [f["filename"] for f in fixes]})
            for fix in fixes:
                run_tracker.artifact("proposed_fix", fix["filename"], fix, node="fixer")

            if not fixes:
                logger.warning("LLM returned no fixes - cannot proceed")
                run_tracker.finish_run("failed", "no_fixes", attempts=attempt)
                return

            apply_fixes(workdir, fixes)

            # Fast pre-check: flake8 catches syntax errors and undefined names
            # in ~1 second. If it fails, skip the 60-second Docker run entirely
            # and feed the error back as the failure signal for the next attempt.
            run_tracker.set_current_node("validator")
            static_ok, static_out = run_static_analysis(workdir)
            run_tracker.step("static_analysis",
                             status="ok" if static_ok else "error",
                             detail={"attempt": attempt, "passed": static_ok})
            if not static_ok:
                passed, test_output = False, static_out
            else:
                passed, test_output = run_tests(workdir)

            fix_diff = get_diff(workdir)
            # Every attempt that reached the suite is an incident - failed
            # attempts are stored as negative examples (suite_green=0)
            incident_id = memory.record_incident(
                repo=repo, branch=branch, commit_sha=commit_sha, test_logs=test_logs,
                diagnosis=diagnosis, files_fixed=[f["filename"] for f in fixes],
                fix_diff=fix_diff, suite_green=passed, attempt=attempt,
                validator_output=test_output,
            )
            run_tracker.artifact("diff", f"attempt {attempt}", fix_diff, node="validator")
            run_tracker.artifact("test_output", f"attempt {attempt}", test_output,
                                 node="validator")
            run_tracker.step("validator", detail={"attempt": attempt, "passed": passed,
                                                  "static_ok": static_ok})
            run_tracker.update_run(attempts=attempt)
            if passed:
                break

        if not passed:
            logger.error("Fix did not resolve the failure - not pushing")
            run_tracker.finish_run("failed", "tests_red", attempts=attempt)
            return

        # ── Step 5: Push fix branch and open PR ──────────────────────────
        if not GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not set - skipping push and PR")
            run_tracker.finish_run("passed", "no_token", attempts=attempt)
            return

        # Name the fix branch after the original branch so it's obvious where it came from
        safe_branch = branch.replace("/", "-")
        fix_branch = f"autofix/{safe_branch}-{commit_sha[:7]}"

        run_tracker.set_current_node("publisher")
        try:
            commit_and_push(workdir, fix_branch, GITHUB_TOKEN, repo)
        except Exception as e:
            logger.error("Push failed: %s", e)
            run_tracker.step("publisher", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "push_failed", attempts=attempt)
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
            if incident_id is not None:
                memory.set_incident_pr(incident_id, pr_url)
            logger.info("=== Pipeline complete | PR: %s ===", pr_url)
            run_tracker.step("publisher", detail={"pr_url": pr_url})
            run_tracker.finish_run("passed", "published", pr_url=pr_url, attempts=attempt)
        except Exception as e:
            logger.error("PR creation failed: %s", e)
            run_tracker.step("publisher", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "pr_failed", attempts=attempt)
