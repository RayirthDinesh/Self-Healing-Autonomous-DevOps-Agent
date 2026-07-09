"""Replay every demo bug branch through the retrieval engine.

For each bug/* branch of the demo repo: clone it, run pytest to capture the
real failure log (what CI would have POSTed), build the repo map, run
retrieval, and assert the truly-buggy file(s) landed in the FULL tier.

Usage:
    python scripts/replay_bugs.py            # retrieval-only (no LLM calls)
    python scripts/replay_bugs.py --live     # + LLM fix + validation per branch
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from repo_map import build_map
from repo_ops import apply_fixes, clone_branch, run_tests
from retrieval import select_context

logging.basicConfig(level=logging.WARNING)

REPO = "RayirthDinesh/sre-demo-app"

# Ground truth: which file(s) each branch actually breaks
BRANCHES = {
    "bug/easy-1-nameerror": {"src/aggregator.py"},
    "bug/easy-2-offbyone": {"src/aggregator.py"},
    "bug/dependency": {"requirements.txt"},
    "bug/logic-error": {"src/aggregator.py"},
    "bug/type-error": {"src/validation.py"},
    "bug/medium-1-importerror": {"src/aggregator.py"},
    "bug/medium-2-wrongtype": {"src/aggregator.py"},
    "bug/edge-case": {"src/ingestion.py"},
    "bug/hard-1-cascade": {"src/aggregator.py", "src/ingestion.py"},
    "bug/extra-hard-1-silent": {"src/aggregator.py", "src/reporter.py"},
}


def replay(branch, expected, live):
    with tempfile.TemporaryDirectory() as workdir:
        clone_branch(REPO, branch, workdir)
        passed, test_logs = run_tests(workdir)
        if passed:
            # Local env can mask install failures (dep already present from a
            # previous run) — the pip ERROR lines still prove the CI failure.
            from retrieval import parse_failure_log
            if not parse_failure_log(test_logs).install_failure:
                return {"branch": branch, "ok": False, "note": "branch unexpectedly green"}

        repo_map = build_map(REPO, branch, workdir)
        ctx = select_context(test_logs, repo_map, workdir)

        hit = expected <= set(ctx.full)
        m = ctx.metrics
        row = {
            "branch": branch,
            "ok": hit,
            "full": sorted(ctx.full),
            "expected": sorted(expected),
            "tokens": f'{m["tokens_sent"]} vs {m["tokens_full_repo"]}',
            "saved": f'{100 - 100 * m["tokens_sent"] // max(m["tokens_full_repo"], 1)}%',
            "ms": m["retrieval_ms"],
        }

        if live and hit:
            from llm_client import call_llm
            result = call_llm(test_logs, ctx)
            apply_fixes(workdir, result.get("fixes", []))
            fixed, _ = run_tests(workdir)
            row["live_fix"] = "green" if fixed else "STILL RED"
        return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also call the LLM and validate the fix")
    parser.add_argument("--branch", help="replay a single branch only")
    args = parser.parse_args()

    targets = {args.branch: BRANCHES[args.branch]} if args.branch else BRANCHES
    failures = 0
    for branch, expected in targets.items():
        row = replay(branch, expected, args.live)
        status = "PASS" if row["ok"] else "FAIL"
        if not row["ok"]:
            failures += 1
        print(f'{status}  {row["branch"]:28s} full={row.get("full")} '
              f'expected={row.get("expected")} tokens={row.get("tokens", "-")} '
              f'saved={row.get("saved", "-")} {row.get("ms", "-")}ms '
              f'{row.get("live_fix", "")} {row.get("note", "")}')

    print(f"\n{len(targets) - failures}/{len(targets)} branches: buggy file(s) in FULL tier")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
