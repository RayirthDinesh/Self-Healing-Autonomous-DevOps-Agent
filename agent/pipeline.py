"""Entry point from the webhook server into the agent.

Thin on purpose: the work lives in agent_graph/graph_nodes. This module exists
so main.py has one stable call to make, and so a crash can never leave the
console showing a run that is still "running".
"""

import logging

import run_tracker
from agent_graph import run_graph

logger = logging.getLogger("sre-agent-webhook")


def run(repo: str, branch: str, commit_sha: str, test_logs: str):
    """Hand one CI failure to the graph."""
    try:
        return run_graph(repo, branch, commit_sha, test_logs)
    finally:
        # A crash (or an early return that missed its finish_run) must not leave
        # the dashboard showing a run that is still "running".
        run_tracker.close_if_running(outcome="pipeline crashed")
