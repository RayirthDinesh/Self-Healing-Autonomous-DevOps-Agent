"""Nodes of the LangGraph multi-agent pipeline.

Each node is a plain function: AgentState in, partial state update out.
LLM access goes through _chat() so tests can stub one seam.
"""

import json
import logging
import os
import shutil

from pydantic import BaseModel

import memory
from graph_tools import TOOL_DESCRIPTIONS, make_tools, _visible
from llm_client import _JSON_CONTRACT, _incidents_section
from repo_map import get_repo_map
from repo_ops import apply_fixes, clone_branch, commit_and_push, get_diff, run_tests
from retrieval import parse_failure_log
from github_ops import create_pull_request

logger = logging.getLogger("sre-agent-webhook")

MAX_ATTEMPTS = 3
MAX_LLM_CALLS = 15
_TOOL_CALL_CAP = 6
_CANDIDATE_CAP = 5
_CRITIC_MAX_ROUNDS = 1


class FileFix(BaseModel):
    filename: str
    content: str


class FixResult(BaseModel):
    diagnosis: str
    fixes: list[FileFix]


def _chat(prompt: str, model: str = None) -> str:
    """One LLM completion via OpenRouter. The single seam tests replace."""
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model=model or os.getenv("LLM_MODEL", "tencent/hy3-preview"),
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=0.1,
        timeout=90,
    )
    return llm.invoke(prompt).content


def _triage_model() -> str:
    return os.getenv("TRIAGE_MODEL") or os.getenv("LLM_MODEL", "tencent/hy3-preview")


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def _step(state, step: str, detail: str = ""):
    memory.log_agent_step(state.get("incident_id"), step, detail)


# ── Nodes ────────────────────────────────────────────────────────────────────

def ingest(state):
    """Phase 1 memory warm-up: PR fates, blame, few-shots, repo map sync."""
    repo, logs = state["repo"], state["test_logs"]
    memory.update_pr_fates(repo, os.getenv("GITHUB_TOKEN"))
    error_class = memory.classify_error(logs)
    blame = memory.blame_scores(repo, error_class)
    incidents = memory.similar_incidents(repo, logs)
    repo_map = get_repo_map(repo, state["commit_sha"], state["workdir"])
    memory.update_repo_state(repo, state["commit_sha"], state["workdir"], repo_map)
    _step(state, "ingest", json.dumps({
        "error_class": error_class, "blame_files": len(blame), "few_shots": len(incidents),
    }))
    return {"error_class": error_class, "blame": blame,
            "incidents": incidents, "repo_map": repo_map}


def router(state):
    """Deterministic: fire the fast path only for a proven failure shape."""
    fp = memory.fast_path_lookup(state["repo"], state["test_logs"])
    if fp:
        targets = [p for p in fp["target_files"] if _visible(p, state["repo_map"])]
        if targets:
            logger.info("Router: fast path fired for %s -> %s", fp["signature"], targets)
            _step(state, "router", f"fast-path {fp['signature']}")
            context = _load_files(state["workdir"], targets)
            return {"fast_path": fp, "fast_path_used": True,
                    "candidate_files": targets, "context": context,
                    "triage_summary": "(skipped — fast path from repo memory)"}
    _step(state, "router", "no fast path")
    return {"fast_path": None, "fast_path_used": False}


def route_after_router(state) -> str:
    return "fixer" if state.get("fast_path") else "triage"


def triage(state):
    """Cheap LLM: one-call summary of what kind of failure this is."""
    prompt = (
        "You triage CI failures. Summarize this failure in JSON only:\n"
        '{"summary": "2-3 sentences: what failed and the most likely area",'
        ' "suspects": ["relative/source/paths that look involved"]}\n\n'
        f"Error class: {state.get('error_class', 'other')}\n\n"
        f"## Failing test output\n```\n{state['test_logs'][-4000:]}\n```"
    )
    summary = ""
    try:
        parsed = _parse_json(_chat(prompt, model=_triage_model()))
        summary = parsed.get("summary", "")
    except Exception as e:
        logger.warning("Triage parse failed (%s) — continuing without summary", e)
    _step(state, "triage", summary)
    return {"triage_summary": summary, "llm_calls": state.get("llm_calls", 0) + 1}


def _fallback_candidates(state) -> list:
    """No-LLM localization: traceback seeds, then blame priors."""
    repo_map, files = state["repo_map"], state["repo_map"]["files"]
    seeds = []

    def add(path):
        if _visible(path, repo_map) and path not in seeds:
            seeds.append(path)

    hits = parse_failure_log(state["test_logs"])
    if hits.install_failure and "requirements.txt" in files:
        add("requirements.txt")
    for path in hits.files:
        if path not in files:
            continue
        if files[path]["is_test"]:
            for target in files[path]["imports"]:
                add(target)
        else:
            add(path)
    for path, _ in sorted(state.get("blame", {}).items(), key=lambda kv: -kv[1]):
        add(path)
    return seeds


def _load_files(workdir: str, paths: list) -> dict:
    context = {}
    for path in paths:
        try:
            with open(os.path.join(workdir, path), encoding="utf-8", errors="replace") as f:
                context[path] = f.read()
        except OSError:
            continue
    return context


def localizer(state):
    """LLM drives the tool belt to pick the files the fixer will see in full."""
    tools = make_tools(state["workdir"], state["repo_map"])
    llm_calls = state.get("llm_calls", 0)
    overview = "\n".join(sorted(
        p for p in state["repo_map"]["files"] if _visible(p, state["repo_map"])
    ))
    feedback = state.get("failure_feedback", "")
    transcript = [(
        "You locate the source files that must change to fix a CI failure.\n"
        f"{TOOL_DESCRIPTIONS}\n\n"
        'Respond with JSON only. Either {"tool": "name", "args": {...}} to call a tool,\n'
        'or {"files": ["path", ...], "reason": "..."} once you know the culprit files '
        f"(max {_CANDIDATE_CAP}). You get at most {_TOOL_CALL_CAP} tool calls.\n\n"
        f"Triage: {state.get('triage_summary', '(none)')}\n\n"
        f"Source files:\n{overview}\n\n"
        f"## Failing test output\n```\n{state['test_logs'][-4000:]}\n```"
        + (f"\n\nA previous fix attempt FAILED validation with:\n```\n{feedback[-2000:]}\n```"
           f"\nPick different or additional files this time." if feedback else "")
    )]

    candidates = None
    tool_calls = 0
    while tool_calls < _TOOL_CALL_CAP and llm_calls < MAX_LLM_CALLS:
        try:
            raw = _chat("\n\n".join(transcript))
        except Exception as e:
            logger.warning("Localizer LLM call failed (%s)", e)
            break
        llm_calls += 1
        try:
            msg = _parse_json(raw)
        except Exception:
            transcript.append("That was not valid JSON. Reply with JSON only.")
            tool_calls += 1
            continue
        if "files" in msg:
            candidates = [str(p).replace("\\", "/") for p in msg["files"]]
            break
        name = msg.get("tool")
        if name in tools:
            result = tools[name](msg.get("args") or {})
            transcript.append(f"{raw}\n\nResult of {name}:\n{result}")
        else:
            transcript.append(f"Unknown tool {name!r}. {TOOL_DESCRIPTIONS}")
        tool_calls += 1

    if candidates:
        candidates = [p for p in candidates if _visible(p, state["repo_map"])]
    if not candidates:
        logger.info("Localizer gave no usable files — falling back to traceback+blame seeds")
        candidates = _fallback_candidates(state)
    candidates = candidates[:_CANDIDATE_CAP]

    _step(state, "localizer", json.dumps({"files": candidates, "tool_calls": tool_calls}))
    return {"candidate_files": candidates,
            "context": _load_files(state["workdir"], candidates),
            "llm_calls": llm_calls}


def fixer(state):
    """Main LLM writes the fix as validated structured output."""
    files_section = "".join(
        f"\n### {path}\n```\n{content}\n```\n"
        for path, content in state.get("context", {}).items()
    )
    feedback = state.get("failure_feedback", "")
    critique = state.get("critic_feedback", "")
    prompt = (
        "You are the fixer agent in a self-healing CI pipeline. Find the bug in the "
        "source files below and fix it.\n\n"
        f"## Failing test output\n```\n{state['test_logs'][-6000:]}\n```\n"
        f"{_incidents_section(state.get('incidents'))}"
        f"\nTriage: {state.get('triage_summary', '(none)')}\n"
        f"\n## Source files\n{files_section}\n"
        + (f"\nA previous attempt FAILED validation with:\n```\n{feedback[-2000:]}\n```\n"
           if feedback else "")
        + (f"\nThis exact change was already tried and FAILED — do NOT propose it again, "
           f"find a materially different fix:\n```diff\n{state['last_fix_diff'][-2000:]}\n```\n"
           if state.get("last_fix_diff") else "")
        + (f"\nReviewer feedback on your last proposal:\n{critique}\n" if critique else "")
        + f"\n{_JSON_CONTRACT}"
    )
    try:
        result = FixResult(**_parse_json(_chat(prompt)))
    except Exception as e:
        logger.error("Fixer output invalid (%s)", e)
        _step(state, "fixer", f"invalid output: {e}")
        return {"fixes": [], "llm_calls": state.get("llm_calls", 0) + 1}

    # Guardrail: the agent may never touch tests, its own code, or CI config
    fixes = []
    for fix in result.fixes:
        clean = fix.filename.replace("\\", "/").lstrip("./")
        if clean.startswith(("tests", "agent", ".git", ".github")) or "/test" in clean:
            logger.warning("Fixer tried to modify %s — dropped", clean)
            continue
        fixes.append({"filename": clean, "content": fix.content})

    _step(state, "fixer", json.dumps(
        {"diagnosis": result.diagnosis, "files": [f["filename"] for f in fixes]}))
    return {"diagnosis": result.diagnosis, "fixes": fixes,
            "llm_calls": state.get("llm_calls", 0) + 1}


def critic(state):
    """Cheap LLM sanity-checks the proposed fix before we pay for Docker."""
    if not state.get("fixes"):
        return {"critic_feedback": "", "llm_calls": state.get("llm_calls", 0)}
    changes = "".join(
        f"\n### {f['filename']} (proposed new content)\n```\n{f['content'][:4000]}\n```\n"
        for f in state["fixes"]
    )
    prompt = (
        "You review a proposed CI auto-fix. Judge only: does the change plausibly "
        "address the diagnosed failure without unrelated edits?\n"
        'Respond JSON only: {"verdict": "approve" | "revise", "feedback": "one short paragraph"}\n\n'
        f"Diagnosis: {state.get('diagnosis', '')}\n"
        f"## Failing test output\n```\n{state['test_logs'][-3000:]}\n```\n"
        f"## Proposed changes\n{changes}"
    )
    verdict, feedback = "approve", ""
    try:
        parsed = _parse_json(_chat(prompt, model=_triage_model()))
        verdict = parsed.get("verdict", "approve")
        feedback = parsed.get("feedback", "")
    except Exception as e:
        logger.warning("Critic parse failed (%s) — approving", e)
    _step(state, "critic", f"{verdict}: {feedback}")
    update = {"llm_calls": state.get("llm_calls", 0) + 1}
    if verdict == "revise":
        update["critic_feedback"] = feedback or "revise"
        update["critic_rounds"] = state.get("critic_rounds", 0) + 1
    else:
        update["critic_feedback"] = ""
    return update


def route_after_critic(state) -> str:
    if (state.get("critic_feedback")
            and state.get("critic_rounds", 0) <= _CRITIC_MAX_ROUNDS
            and state.get("llm_calls", 0) < MAX_LLM_CALLS
            and state.get("fixes")):
        return "fixer"
    return "validator"


def validator(state):
    """Docker is the judge. Red resets the clone so the next attempt starts clean."""
    workdir = state["workdir"]
    attempt = state.get("attempt", 0) + 1
    update = {"attempt": attempt, "demoted_fast_path": False}

    if not state.get("fixes"):
        update.update(passed=False, test_output="(no fixes produced)",
                      failure_feedback="The model produced no applicable fixes.")
        return update

    apply_fixes(workdir, state["fixes"])
    passed, test_output = run_tests(workdir)
    fix_diff = get_diff(workdir)
    incident_id = memory.record_incident(
        repo=state["repo"], branch=state["branch"], commit_sha=state["commit_sha"],
        test_logs=state["test_logs"], diagnosis=state.get("diagnosis", ""),
        files_fixed=[f["filename"] for f in state["fixes"]],
        fix_diff=fix_diff, suite_green=passed, attempt=attempt,
    )
    update.update(passed=passed, test_output=test_output, incident_id=incident_id)
    _step({**state, "incident_id": incident_id}, "validator",
          f"attempt {attempt}: {'green' if passed else 'red'}")

    if not passed:
        update["failure_feedback"] = test_output[-3000:]
        update["last_fix_diff"] = fix_diff
        if state.get("fast_path_used"):
            memory.fast_path_miss(state["repo"], state["fast_path"]["signature"])
            logger.info("Fast path missed — demoted, rerouting through triage")
            update.update(fast_path_used=False, fast_path=None, demoted_fast_path=True)
        # pristine tree for the next attempt
        try:
            for entry in os.listdir(workdir):
                p = os.path.join(workdir, entry)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            clone_branch(state["repo"], state["branch"], workdir)
        except Exception as e:
            logger.error("Workdir reset failed (%s)", e)
    return update


def route_after_validator(state) -> str:
    if state.get("passed"):
        return "publisher"
    if state.get("llm_calls", 0) >= MAX_LLM_CALLS or state.get("attempt", 0) >= MAX_ATTEMPTS:
        return "reflect"
    if state.get("demoted_fast_path"):
        return "triage"
    return "localizer"


def publisher(state):
    """Same publish contract as the legacy path: push autofix branch, open PR."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        logger.warning("GITHUB_TOKEN not set — skipping push and PR")
        return {"done": "no_token"}
    safe_branch = state["branch"].replace("/", "-")
    fix_branch = f"autofix/{safe_branch}-{state['commit_sha'][:7]}"
    try:
        commit_and_push(state["workdir"], fix_branch, token, state["repo"])
        pr_url = create_pull_request(
            token=token, repo=state["repo"], head=fix_branch, base="main",
            title=f"[Auto-fix] {state.get('diagnosis', 'automated fix')}",
            body=(
                f"**Branch:** `{state['branch']}`\n"
                f"**Commit:** `{state['commit_sha']}`\n\n"
                f"**Diagnosis:** {state.get('diagnosis', '')}\n\n"
                f"This fix was generated by the SRE Agent multi-agent graph and "
                f"verified by running the full test suite before pushing."
            ),
        )
    except Exception as e:
        logger.error("Publish failed: %s", e)
        return {"done": "publish_failed"}
    if state.get("incident_id") is not None:
        memory.set_incident_pr(state["incident_id"], pr_url)
    _step(state, "publisher", pr_url)
    return {"pr_url": pr_url, "done": "published"}


def reflect(state):
    """Run post-mortem into agent_steps, then give up cleanly."""
    _step(state, "reflect", json.dumps({
        "attempts": state.get("attempt", 0),
        "llm_calls": state.get("llm_calls", 0),
        "last_diagnosis": state.get("diagnosis", ""),
        "candidate_files": state.get("candidate_files", []),
        "fast_path_tried": bool(state.get("demoted_fast_path")),
    }))
    logger.error("Graph gave up after %d attempt(s), %d LLM call(s)",
                 state.get("attempt", 0), state.get("llm_calls", 0))
    return {"done": "gave_up"}
