"""Persistent memory — SQLite mental map of the target repo plus incident history.

Everything here is advisory: a memory failure must never kill a pipeline run,
so every public function degrades to a neutral value and logs a warning.
"""

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import time

import requests

logger = logging.getLogger("sre-agent-webhook")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_state (
    repo        TEXT PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_history (
    repo             TEXT NOT NULL,
    path             TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    size             INTEGER NOT NULL,
    symbol_count     INTEGER NOT NULL,
    last_seen_commit TEXT NOT NULL,
    times_changed    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, path)
);
CREATE TABLE IF NOT EXISTS co_change (
    repo    TEXT NOT NULL,
    path_a  TEXT NOT NULL,
    path_b  TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, path_a, path_b)
);
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo          TEXT NOT NULL,
    branch        TEXT,
    commit_sha    TEXT,
    error_class   TEXT NOT NULL,
    log_excerpt   TEXT,
    log_embedding BLOB,
    diagnosis     TEXT,
    files_fixed   TEXT,          -- JSON list of paths
    fix_diff      TEXT,
    suite_green   INTEGER NOT NULL DEFAULT 0,
    pr_url        TEXT,
    pr_number     INTEGER,
    pr_state      TEXT NOT NULL DEFAULT 'none',  -- none|open|merged|closed
    attempt       INTEGER,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blame (
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    error_class TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, path, error_class)
);
CREATE TABLE IF NOT EXISTS agent_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER,
    step        TEXT NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fast_paths (
    repo         TEXT NOT NULL,
    error_class  TEXT NOT NULL,
    signature    TEXT NOT NULL,
    target_files TEXT,
    merged_count INTEGER NOT NULL DEFAULT 0,
    miss_count   INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (repo, error_class, signature)
);
"""

# Ordered: first match wins
_ERROR_CLASSES = [
    ("install-failure", re.compile(
        r"ERROR: (?:No matching distribution|Could not find a version|Cannot install)"
        r"|error: subprocess-exited-with-error"
        r"|ModuleNotFoundError: No module named")),
    ("import-error", re.compile(r"\bImportError\b")),
    ("name-error", re.compile(r"\bNameError\b")),
    ("type-error", re.compile(r"\bTypeError\b")),
    ("assertion-value", re.compile(r"\bAssertionError\b|^E\s+assert ", re.MULTILINE)),
]

_LOG_EXCERPT_CHARS = 2000
_SIM_THRESHOLD = 0.55
_SIM_CAP = 2
_DIFF_PROMPT_LINES = 40


def _db_path() -> str:
    return os.environ.get(
        "MEMORY_DB",
        os.path.join(os.path.expanduser("~"), ".sre-agent", "memory.db"),
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def _never_fatal(default):
    """Memory is advisory — log and return a neutral value on any failure."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.warning("memory: %s failed (%s) — continuing without it", fn.__name__, e)
                return default() if callable(default) else default
        return wrapper
    return deco


def _embed(text: str):
    """Embedding vector for text, or None if fastembed is unavailable."""
    try:
        from embeddings import embed
        return embed(text)
    except Exception as e:
        logger.warning("memory: embedding unavailable (%s)", e)
        return None


def classify_error(log: str) -> str:
    for name, pattern in _ERROR_CLASSES:
        if pattern.search(log):
            return name
    return "other"


# ── Repo mental map ──────────────────────────────────────────────────────────

@_never_fatal(list)
def update_repo_state(repo: str, commit_sha: str, clone_path: str, repo_map: dict) -> list:
    """Snapshot the repo's files, bump churn counters and co-change pairs.

    Returns the list of paths that changed since the last observed commit.
    """
    now = time.time()
    changed = []
    with _connect() as conn:
        known = {
            path: sha for path, sha in conn.execute(
                "SELECT path, sha256 FROM file_history WHERE repo = ?", (repo,))
        }
        first_sync = not known
        for path, entry in repo_map.get("files", {}).items():
            try:
                with open(os.path.join(clone_path, path), "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
            if known.get(path) != digest:
                if not first_sync:
                    changed.append(path)
                conn.execute(
                    "INSERT INTO file_history (repo, path, sha256, size, symbol_count,"
                    " last_seen_commit, times_changed) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(repo, path) DO UPDATE SET sha256 = excluded.sha256,"
                    " size = excluded.size, symbol_count = excluded.symbol_count,"
                    " last_seen_commit = excluded.last_seen_commit,"
                    " times_changed = times_changed + 1",
                    (repo, path, digest, entry.get("size", 0),
                     len(entry.get("symbols", [])), commit_sha, 0),
                )
        # Files that changed together in this pull are related
        for i, a in enumerate(sorted(changed)):
            for b in sorted(changed)[i + 1:]:
                conn.execute(
                    "INSERT INTO co_change (repo, path_a, path_b, count) VALUES (?, ?, ?, 1)"
                    " ON CONFLICT(repo, path_a, path_b) DO UPDATE SET count = count + 1",
                    (repo, a, b),
                )
        conn.execute(
            "INSERT INTO repo_state (repo, commit_sha, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(repo) DO UPDATE SET commit_sha = excluded.commit_sha,"
            " updated_at = excluded.updated_at",
            (repo, commit_sha, now),
        )
    if changed:
        logger.info("memory: %d file(s) changed since last sync: %s", len(changed), changed)
    return changed


# ── Incidents ────────────────────────────────────────────────────────────────

@_never_fatal(None)
def record_incident(repo: str, branch: str, commit_sha: str, test_logs: str,
                    diagnosis: str, files_fixed: list, fix_diff: str,
                    suite_green: bool, attempt: int):
    """Store one fix attempt that reached the test suite. Returns the incident id."""
    excerpt = test_logs[-_LOG_EXCERPT_CHARS:]
    vec = _embed(excerpt)
    blob = vec.astype("float32").tobytes() if vec is not None else None
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (repo, branch, commit_sha, error_class, log_excerpt,"
            " log_embedding, diagnosis, files_fixed, fix_diff, suite_green, attempt,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, branch, commit_sha, classify_error(test_logs), excerpt, blob,
             diagnosis, json.dumps(files_fixed), fix_diff, int(suite_green), attempt,
             time.time()),
        )
        return cur.lastrowid


@_never_fatal(None)
def set_incident_pr(incident_id: int, pr_url: str):
    """Attach the opened PR to an incident so the next run can check its fate."""
    match = re.search(r"/pull/(\d+)", pr_url or "")
    number = int(match.group(1)) if match else None
    with _connect() as conn:
        conn.execute(
            "UPDATE incidents SET pr_url = ?, pr_number = ?, pr_state = 'open' WHERE id = ?",
            (pr_url, number, incident_id),
        )


@_never_fatal(list)
def similar_incidents(repo: str, test_logs: str, k: int = _SIM_CAP,
                      threshold: float = _SIM_THRESHOLD) -> list:
    """Past successful incidents that look like this failure.

    Cosine similarity over log embeddings when available; otherwise falls back
    to same-error-class, most recent first. Closed (rejected) PRs are excluded.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, error_class, log_embedding, diagnosis, files_fixed, fix_diff,"
            " pr_state, created_at FROM incidents"
            " WHERE repo = ? AND suite_green = 1 AND pr_state != 'closed'",
            (repo,),
        ).fetchall()
    if not rows:
        return []

    query_vec = _embed(test_logs[-_LOG_EXCERPT_CHARS:])
    scored = []
    if query_vec is not None:
        import numpy as np
        from embeddings import cosine
        for row in rows:
            if row[2] is None:
                continue
            vec = np.frombuffer(row[2], dtype="float32")
            score = cosine(query_vec, vec)
            if score >= threshold:
                scored.append((score, row))
        scored.sort(key=lambda item: -item[0])
    else:
        error_class = classify_error(test_logs)
        scored = [(1.0, row) for row in rows if row[1] == error_class]
        scored.sort(key=lambda item: -item[1][7])  # most recent first

    results = []
    for score, row in scored[:k]:
        diff_lines = (row[5] or "").splitlines()
        results.append({
            "id": row[0],
            "error_class": row[1],
            "diagnosis": row[3],
            "files_fixed": json.loads(row[4] or "[]"),
            "fix_diff": "\n".join(diff_lines[:_DIFF_PROMPT_LINES]),
            "pr_state": row[6],
            "score": round(score, 3),
        })
    return results


# ── PR fate + blame ──────────────────────────────────────────────────────────

@_never_fatal(dict)
def update_pr_fates(repo: str, github_token: str) -> dict:
    """Lazy sweep: resolve the fate of every still-open PR from past incidents.

    merged  -> blame weight +1.0 for each fixed file under the error class
    closed  -> blame weight *= 0.5 (decay)
    Returns {pr_number: fate} for anything that changed state.
    """
    if not github_token:
        return {}
    resolved = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, pr_number, error_class, files_fixed FROM incidents"
            " WHERE repo = ? AND pr_state = 'open' AND pr_number IS NOT NULL",
            (repo,),
        ).fetchall()
        for incident_id, number, error_class, files_json in rows:
            response = requests.get(
                f"https://api.github.com/repos/{repo}/pulls/{number}",
                headers={"Authorization": f"token {github_token}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=15,
            )
            response.raise_for_status()
            pr = response.json()
            if pr.get("merged"):
                fate = "merged"
            elif pr.get("state") == "closed":
                fate = "closed"
            else:
                continue  # still open
            conn.execute("UPDATE incidents SET pr_state = ? WHERE id = ?", (fate, incident_id))
            for path in json.loads(files_json or "[]"):
                if fate == "merged":
                    conn.execute(
                        "INSERT INTO blame (repo, path, error_class, weight) VALUES (?, ?, ?, 1.0)"
                        " ON CONFLICT(repo, path, error_class) DO UPDATE SET weight = weight + 1.0",
                        (repo, path, error_class),
                    )
                else:
                    conn.execute(
                        "UPDATE blame SET weight = weight * 0.5"
                        " WHERE repo = ? AND path = ? AND error_class = ?",
                        (repo, path, error_class),
                    )
            resolved[number] = fate
    if resolved:
        logger.info("memory: PR fates resolved: %s", resolved)
    return resolved


@_never_fatal(dict)
def blame_scores(repo: str, error_class: str) -> dict:
    """Learned prior {path: weight in [0,1]} for this error class."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, weight FROM blame WHERE repo = ? AND error_class = ? AND weight > 0",
            (repo, error_class),
        ).fetchall()
    if not rows:
        return {}
    top = max(weight for _, weight in rows)
    return {path: weight / top for path, weight in rows}


# ── Agent step log (Phase 2 consumes this) ───────────────────────────────────

@_never_fatal(None)
def log_agent_step(incident_id, step: str, detail: str = ""):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO agent_steps (incident_id, step, detail, created_at) VALUES (?, ?, ?, ?)",
            (incident_id, step, detail, time.time()),
        )
