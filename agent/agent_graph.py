r"""LangGraph wiring for the multi-agent SRE pipeline.

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
import run_tracker
from graph_state import AgentState
from repo_ops import ValidatorUnavailable, clone_branch

logger = logging.getLogger("sre-agent-webhook")


def build_graph(checkpointer=None):
    """Wire the nodes and edges above into a compiled LangGraph app."""
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
    """SqliteSaver when it is available. Checkpointing never blocks a run."""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        path = os.path.join(os.path.expanduser("~"), ".sre-agent", "graph_checkpoints.db")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        return SqliteSaver(sqlite3.connect(path, check_same_thread=False))
    except Exception as e:
        logger.warning("Graph checkpointing unavailable (%s)", e)
        return None


def run_graph(repo: str, branch: str, commit_sha: str, test_logs: str,
              source: str = "webhook"):
    """Run one CI failure through the graph. Called by pipeline.run."""
    logger.info("=== Graph pipeline started | branch=%s commit=%s ===", branch, commit_sha)
    run_id = run_tracker.start_run(repo, branch, commit_sha, source=source, mode="graph")
    run_tracker.artifact("ci_logs", "failing CI output", test_logs)
    try:
        return _run_graph_body(repo, branch, commit_sha, test_logs)
    finally:
        # Whether this run degraded to the fallback model is state about this
        # run only. Dropping it means the next run tries the preferred model
        # again, so topping up a balance resumes normal service by itself.
        nodes.forget_degraded(run_id)


def _run_graph_body(repo: str, branch: str, commit_sha: str, test_logs: str):
    with tempfile.TemporaryDirectory() as workdir:
        try:
            clone_branch(repo, branch, workdir)
        except Exception as e:
            logger.error("Clone failed: %s", e)
            run_tracker.step("clone", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "clone_failed")
            return
        run_tracker.step("clone", detail={"workdir": workdir})

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
        except nodes.ProviderUnavailable as e:
            # Not a failed fix attempt: the model was never reachable. Report
            # that plainly instead of as three exhausted attempts.
            logger.error("Model provider unavailable, standing down: %s", e)
            run_tracker.step("llm", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "provider_unavailable")
            return
        except ValidatorUnavailable as e:
            # No suite ever ran, so there is nothing to call red. Reporting this
            # as a failed fix would blame the patch for a missing daemon.
            logger.error("Validator unavailable, standing down: %s", e)
            run_tracker.step("validator", status="error", detail={"error": str(e)})
            run_tracker.finish_run("error", "validator_unavailable")
            return
        except Exception as e:
            logger.error("Graph run failed: %s", e)
            run_tracker.finish_run("error", f"{type(e).__name__}: {e}")
            return
        logger.info(
            "=== Graph pipeline complete | outcome=%s attempts=%d llm_calls=%d%s ===",
            final.get("done", "unknown"), final.get("attempt", 0),
            final.get("llm_calls", 0),
            f" | PR: {final['pr_url']}" if final.get("pr_url") else "",
        )
        run_tracker.finish_run(
            "passed" if final.get("passed") else "failed",
            final.get("done", "unknown"),
            pr_url=final.get("pr_url", ""),
            attempts=final.get("attempt", 0),
            llm_calls=final.get("llm_calls", 0),
        )
        return final
