r"""LangGraph wiring for the multi-agent SRE pipeline (AGENT_MODE=graph).

ingest -> router -(fast path)-> fixer
                \-(miss)-> triage -> localizer -> fixer -> critic -(revise)-> fixer
                                                        \-(approve)-> validator
validator -(green)-> publisher | -(red, demoted)-> triage
          | -(red, attempt<3)-> localizer | -(exhausted)-> reflect
"""

import logging
import os
import tempfile
import time

from langgraph.graph import END, StateGraph

import graph_nodes as nodes
from graph_state import AgentState
from repo_ops import clone_branch

logger = logging.getLogger("sre-agent-webhook")


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("ingest", nodes.ingest)
    g.add_node("router", nodes.router)
    g.add_node("triage", nodes.triage)
    g.add_node("localizer", nodes.localizer)
    g.add_node("fixer", nodes.fixer)
    g.add_node("critic", nodes.critic)
    g.add_node("validator", nodes.validator)
    g.add_node("publisher", nodes.publisher)
    g.add_node("reflect", nodes.reflect)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "router")
    g.add_conditional_edges("router", nodes.route_after_router,
                            {"fixer": "fixer", "triage": "triage"})
    g.add_edge("triage", "localizer")
    g.add_edge("localizer", "fixer")
    g.add_edge("fixer", "critic")
    g.add_conditional_edges("critic", nodes.route_after_critic,
                            {"fixer": "fixer", "validator": "validator"})
    g.add_conditional_edges("validator", nodes.route_after_validator,
                            {"publisher": "publisher", "triage": "triage",
                             "localizer": "localizer", "reflect": "reflect"})
    g.add_edge("publisher", END)
    g.add_edge("reflect", END)
    return g.compile(checkpointer=checkpointer)


def _checkpointer():
    """SqliteSaver when available — optional, never blocks a run."""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        path = os.path.join(os.path.expanduser("~"), ".sre-agent", "graph_checkpoints.db")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        return SqliteSaver(sqlite3.connect(path, check_same_thread=False))
    except Exception as e:
        logger.warning("Graph checkpointing unavailable (%s)", e)
        return None


def run_graph(repo: str, branch: str, commit_sha: str, test_logs: str):
    """Graph-mode equivalent of pipeline.run — same inputs, same side effects."""
    logger.info("=== Graph pipeline started | branch=%s commit=%s ===", branch, commit_sha)
    with tempfile.TemporaryDirectory() as workdir:
        try:
            clone_branch(repo, branch, workdir)
        except Exception as e:
            logger.error("Clone failed: %s", e)
            return

        app = build_graph(_checkpointer())
        state: AgentState = {
            "repo": repo, "branch": branch, "commit_sha": commit_sha,
            "test_logs": test_logs, "workdir": workdir,
            "attempt": 0, "llm_calls": 0, "critic_rounds": 0,
        }
        try:
            # thread_id must be unique per run: reusing one resumes the old
            # checkpointed state and leaks keys (pr_url, done) across runs
            final = app.invoke(state, config={
                "configurable": {"thread_id": f"{repo}@{commit_sha}@{int(time.time())}"},
                "recursion_limit": 60,
                # LangSmith trace naming (no-op when tracing is off)
                "run_name": f"{branch}@{commit_sha[:7]}",
                "tags": ["sre-agent", branch],
                "metadata": {"repo": repo, "branch": branch, "commit": commit_sha},
            })
        except Exception as e:
            logger.error("Graph run failed: %s", e)
            return
        logger.info(
            "=== Graph pipeline complete | outcome=%s attempts=%d llm_calls=%d%s ===",
            final.get("done", "unknown"), final.get("attempt", 0),
            final.get("llm_calls", 0),
            f" | PR: {final['pr_url']}" if final.get("pr_url") else "",
        )
        return final
