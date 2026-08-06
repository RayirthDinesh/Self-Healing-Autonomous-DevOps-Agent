"""Run tracking: the event log the dashboard reads.

Every pipeline (graph, legacy, SWE-bench) writes here; the dashboard
never imports pipeline code, it just reads these tables. Same contract as
memory.py: tracking is advisory and must never kill a fix, so every public
function is wrapped in _never_fatal and degrades to a neutral value.

Tables live in the same SQLite file as memory.py (~/.sre-agent/memory.db):

    runs       one row per pipeline execution
    events     one row per node start/end, small enough to poll cheaply
    artifacts  the big blobs (diffs, test output, LLM prompt/response)
"""

import contextlib
import contextvars
import functools
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger("sre-agent-webhook")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    branch      TEXT,
    commit_sha  TEXT,
    source      TEXT NOT NULL DEFAULT 'webhook',
    instance_id TEXT,
    mode        TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running|passed|failed|error
    outcome     TEXT,
    pr_url      TEXT,
    error_class TEXT,
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    node        TEXT NOT NULL,
    phase       TEXT NOT NULL DEFAULT 'end',      -- start|end|info
    status      TEXT NOT NULL DEFAULT 'ok',       -- ok|error|skipped
    detail      TEXT,                             -- JSON
    duration_ms INTEGER,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    event_seq  INTEGER,
    node       TEXT,
    kind       TEXT NOT NULL,   -- repo_map|proposed_fix|diff|test_output|llm_call|note
    name       TEXT,
    body       TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts (run_id, id);
"""

# Artifact bodies are for human reading, not archival. Keep the DB sane.
_BODY_CAP = 200_000

_current_run = contextvars.ContextVar("sre_run_id", default=None)
_current_node = contextvars.ContextVar("sre_node", default="")
_seq_lock = threading.Lock()


def _db_path() -> str:
    return os.environ.get(
        "MEMORY_DB",
        os.path.join(os.path.expanduser("~"), ".sre-agent", "memory.db"),
    )


@contextlib.contextmanager
def _connect():
    """Commit-and-close connection. The SSE endpoint polls every second, so
    leaving connections to the garbage collector (as memory.py does) would leak
    file handles for the lifetime of a stream."""
    path = _db_path()
    # Owner-only: events carry repo diffs, CI logs and full LLM prompts
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    if not os.path.exists(path):
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _never_fatal(default):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.warning("run_tracker: %s failed (%s) - continuing", fn.__name__, e)
                return default() if callable(default) else default
        return wrapper
    return deco


def _dumps(detail) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, default=str)
    except Exception:
        return str(detail)


# ── Run lifecycle ────────────────────────────────────────────────────────────

@_never_fatal(None)
def start_run(repo: str, branch: str = "", commit_sha: str = "",
              source: str = "webhook", instance_id: str = "", mode: str = "") -> str:
    """Open a run and make it current for this thread/task. Returns the run id."""
    run_id = uuid.uuid4().hex[:16]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, repo, branch, commit_sha, source, instance_id, mode,"
            " status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (run_id, repo, branch, commit_sha, source, instance_id, mode, time.time()),
        )
    _current_run.set(run_id)
    logger.info("run_tracker: run %s started (%s %s@%s)", run_id, source, repo, branch)
    return run_id


def current_run():
    return _current_run.get()


def set_current_run(run_id):
    _current_run.set(run_id)


def current_node() -> str:
    return _current_node.get()


def set_current_node(node: str):
    _current_node.set(node)


@_never_fatal(None)
def update_run(run_id=None, **fields):
    """Patch run columns. Unknown columns are ignored."""
    run_id = run_id or current_run()
    if not run_id or not fields:
        return
    allowed = {"repo", "branch", "commit_sha", "source", "instance_id", "mode",
               "status", "outcome", "pr_url", "error_class", "llm_calls",
               "attempts", "finished_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?",
                     (*sets.values(), run_id))


@_never_fatal(None)
def finish_run(status: str, outcome: str = "", run_id=None, **fields):
    """Close a run. status: passed | failed | error."""
    run_id = run_id or current_run()
    if not run_id:
        return
    update_run(run_id, status=status, outcome=outcome,
               finished_at=time.time(), **fields)
    logger.info("run_tracker: run %s finished (%s / %s)", run_id, status, outcome or "-")


@_never_fatal(None)
def close_if_running(status: str = "error", outcome: str = "crashed", run_id=None):
    """Backstop: close a run whose pipeline died without finishing it.

    Without this a crashed run stays 'running' in the dashboard forever.
    """
    run_id = run_id or current_run()
    if not run_id:
        return
    run = get_run(run_id)
    if run and run.get("status") == "running":
        finish_run(status, outcome, run_id=run_id)


# ── Events ───────────────────────────────────────────────────────────────────

def _next_seq(conn, run_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?",
                       (run_id,)).fetchone()
    return (row[0] or 0) + 1


@_never_fatal(None)
def step(node: str, phase: str = "end", status: str = "ok", detail=None,
         duration_ms=None, run_id=None):
    """Record one node transition. Returns its seq, or None when untracked."""
    run_id = run_id or current_run()
    if not run_id:
        return None
    with _seq_lock, _connect() as conn:
        seq = _next_seq(conn, run_id)
        conn.execute(
            "INSERT INTO events (run_id, seq, node, phase, status, detail, duration_ms,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, seq, node, phase, status, _dumps(detail),
             int(duration_ms) if duration_ms is not None else None, time.time()),
        )
    return seq


@_never_fatal(None)
def artifact(kind: str, name: str, body, node: str = "", event_seq=None, run_id=None):
    """Store one large payload (diff, test output, LLM call, repo map)."""
    run_id = run_id or current_run()
    if not run_id:
        return None
    text = body if isinstance(body, str) else _dumps(body)
    if len(text) > _BODY_CAP:
        text = text[:_BODY_CAP] + f"\n… [truncated at {_BODY_CAP} chars]"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO artifacts (run_id, event_seq, node, kind, name, body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, event_seq, node or current_node(), kind, name, text, time.time()),
        )
        return cur.lastrowid


def repo_map_digest(repo_map: dict) -> dict:
    """Per-file structure the dashboard's codebase-map panel renders.

    Lives here rather than in graph_nodes so the legacy pipeline and the
    SWE-bench harness can build it without importing the graph (and its
    dependencies).
    """
    files = (repo_map or {}).get("files") or {}
    languages, entries = {}, []
    for path, entry in sorted(files.items()):
        lang = entry.get("lang", "text")
        languages[lang] = languages.get(lang, 0) + 1
        entries.append({
            "path": path,
            "lang": lang,
            "size": entry.get("size", 0),
            "is_test": bool(entry.get("is_test")),
            "symbols": len(entry.get("symbols") or []),
            "imports": len(entry.get("imports") or []),
        })
    return {
        "file_count": len(entries),
        "symbol_count": sum(e["symbols"] for e in entries),
        "test_files": sum(1 for e in entries if e["is_test"]),
        "languages": languages,
        "files": entries,
    }


@_never_fatal(None)
def llm_call(model: str, prompt: str, response: str, latency_ms: int, node: str = ""):
    """One LLM round-trip, shown in the dashboard's agent-activity panel."""
    node = node or current_node()
    return artifact(
        "llm_call", f"{node or 'llm'} · {model}",
        {"node": node, "model": model, "latency_ms": int(latency_ms),
         "prompt_chars": len(prompt or ""), "response_chars": len(response or ""),
         "prompt": prompt, "response": response},
        node=node,
    )


# ── Decorator used by the graph nodes ────────────────────────────────────────

def tracked(node: str, summary=None):
    """Wrap a graph node: emit start/end events with duration and a summary.

    summary(state, update) -> dict, called only when the node returns cleanly.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            token = _current_node.set(node)
            started = time.time()
            step(node, phase="start", detail={"attempt": (state or {}).get("attempt", 0)})
            try:
                update = fn(state, *args, **kwargs)
            except Exception as e:
                step(node, phase="end", status="error",
                     detail={"error": f"{type(e).__name__}: {e}"},
                     duration_ms=(time.time() - started) * 1000)
                _current_node.reset(token)
                raise
            detail = {}
            if summary is not None:
                try:
                    detail = summary(state, update) or {}
                except Exception as e:  # a bad summary must not break the node
                    detail = {"summary_error": str(e)}
            step(node, phase="end", status="ok", detail=detail,
                 duration_ms=(time.time() - started) * 1000)
            _current_node.reset(token)
            return update
        return wrapper
    return deco


# ── Read side (used by dashboard.py and tests) ───────────────────────────────

def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@_never_fatal(list)
def list_runs(limit: int = 50, source: str = "", repo: str = "") -> list:
    where, params = [], []
    if source:
        where.append("source = ?")
        params.append(source)
    if repo:
        where.append("repo = ?")
        params.append(repo)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        return _rows(conn, f"SELECT * FROM runs {clause} ORDER BY started_at DESC LIMIT ?",
                     (*params, int(limit)))


@_never_fatal(None)
def get_run(run_id: str):
    with _connect() as conn:
        rows = _rows(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    return rows[0] if rows else None


@_never_fatal(list)
def get_events(run_id: str, after_seq: int = 0) -> list:
    with _connect() as conn:
        rows = _rows(conn, "SELECT * FROM events WHERE run_id = ? AND seq > ?"
                           " ORDER BY seq", (run_id, int(after_seq)))
    for row in rows:
        raw = row.get("detail") or ""
        try:
            row["detail"] = json.loads(raw) if raw else {}
        except Exception:
            row["detail"] = {"text": raw}
    return rows


@_never_fatal(list)
def get_artifact_index(run_id: str, after_id: int = 0) -> list:
    """Artifact metadata without bodies - the UI fetches bodies on demand."""
    with _connect() as conn:
        return _rows(
            conn,
            "SELECT id, run_id, event_seq, node, kind, name, created_at,"
            " LENGTH(body) AS size FROM artifacts WHERE run_id = ? AND id > ?"
            " ORDER BY id", (run_id, int(after_id)),
        )


@_never_fatal(None)
def get_artifact(artifact_id: int):
    with _connect() as conn:
        rows = _rows(conn, "SELECT * FROM artifacts WHERE id = ?", (int(artifact_id),))
    return rows[0] if rows else None
