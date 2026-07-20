"""Error-triggered context retrieval.

Given a failing CI log and the repo map, select which files the LLM sees:
  FULL       — files implicated by the traceback (highest precision signal)
  SIGNATURES — 1-hop import-graph neighbors + BM25 runners-up
  OVERVIEW   — one line per remaining file, so the model knows the terrain
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

try:
    from chunker import chunks_for_repo as _chunks_for_repo
    from embeddings import chunk_scores as _chunk_scores
    _EMBEDDINGS_AVAILABLE = True
except Exception:
    _EMBEDDINGS_AVAILABLE = False

logger = logging.getLogger("sre-agent-webhook")

_DEFAULT_FULL_MAX = 5
_DEFAULT_SIG_MAX = 15

# Path-bearing patterns seen in pytest/python/node failure output
_PATH_PATTERNS = [
    re.compile(r'File "([^"]+)", line \d+'),
    re.compile(r"(?:^|\s)([\w./\\-]+\.(?:py|js|jsx|ts|tsx)):\d+", re.MULTILINE),
    re.compile(r"FAILED ([\w./\\-]+\.(?:py|js|jsx|ts|tsx))"),
    re.compile(r"\(([\w./\\-]+\.py)\)"),  # ImportError: ... from 'x' (src/core.py)
    re.compile(r"at .+ \(([\w./\\-]+\.(?:js|jsx|ts|tsx)):\d+:\d+\)"),  # JS stack
]

_INSTALL_FAILURE_RE = re.compile(
    r"ERROR: (?:No matching distribution|Could not find a version|Cannot install)"
    r"|error: subprocess-exited-with-error"
    r"|ModuleNotFoundError: No module named",
)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class FailureHits:
    files: set = field(default_factory=set)
    install_failure: bool = False


@dataclass
class TieredContext:
    full: dict            # path -> full file content
    signatures: dict      # path -> signature lines
    overview: dict        # path -> one-line summary
    metrics: dict


def parse_failure_log(log: str) -> FailureHits:
    """Extract repo-relative file paths and failure class from raw CI output."""
    hits = FailureHits()
    for pattern in _PATH_PATTERNS:
        for match in pattern.findall(log):
            path = match.replace("\\", "/").lstrip("./")
            if not os.path.isabs(path):
                hits.files.add(path)
    hits.install_failure = bool(_INSTALL_FAILURE_RE.search(log))
    return hits


def _tokenize(text: str):
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _signature_block(entry):
    lines = []
    for sym in entry["symbols"]:
        doc = f'  # {sym["doc"].splitlines()[0]}' if sym["doc"] else ""
        lines.append(f'{sym["sig"]}{doc}')
    return "\n".join(lines)


def _overview_line(entry):
    names = ", ".join(s["name"] for s in entry["symbols"][:6])
    return names or f'({entry["lang"]}, {entry["size"]} bytes)'


def _estimate_tokens(text_parts) -> int:
    return sum(len(t) for t in text_parts) // 4


def select_context(test_logs: str, repo_map: dict, clone_path: str,
                   blame: dict = None) -> TieredContext:
    """Assemble the tiered LLM context for one failure.

    blame — optional {path: weight in [0,1]} prior from memory: files that
    historically fixed this error class rank higher.
    """
    started = time.monotonic()
    full_max = int(os.environ.get("CONTEXT_FULL_MAX", _DEFAULT_FULL_MAX))
    sig_max = int(os.environ.get("CONTEXT_SIG_MAX", _DEFAULT_SIG_MAX))

    files = repo_map["files"]
    source_files = {p: e for p, e in files.items() if not e["is_test"]}

    hits = parse_failure_log(test_logs)

    # Seeds: implicated source files; hits on test files pull in what they import
    seeds = set()
    for path in hits.files:
        if path not in files:
            continue
        if files[path]["is_test"]:
            seeds.update(t for t in files[path]["imports"] if t in source_files)
        else:
            seeds.add(path)
    if hits.install_failure and "requirements.txt" in files:
        seeds.add("requirements.txt")

    # BM25 over all source files: error text vs path+symbols+docs+content
    contents = {}
    for path in source_files:
        try:
            with open(os.path.join(clone_path, path), encoding="utf-8", errors="replace") as f:
                contents[path] = f.read()
        except OSError:
            contents[path] = ""
    corpus_paths = list(source_files)
    corpus = [
        _tokenize(" ".join(
            [path]
            + [s["name"] + " " + s["doc"] for s in source_files[path]["symbols"]]
            + [contents[path]]
        ))
        for path in corpus_paths
    ]
    query = _tokenize(test_logs[-4000:])
    bm25_raw = dict(zip(corpus_paths, BM25Okapi(corpus).get_scores(query))) if corpus else {}
    rank = repo_map.get("rank", {})

    # Normalise BM25 to [0,1] so it can be combined with cosine similarity
    bm25_max = max(bm25_raw.values(), default=1.0) or 1.0
    scores = {p: v / bm25_max for p, v in bm25_raw.items()}

    # Chunk-level semantic scores — embed each function separately, take best per file
    if _EMBEDDINGS_AVAILABLE:
        try:
            file_chunks = _chunks_for_repo(contents, repo_map["files"])
            sem = _chunk_scores(test_logs[-2000:], file_chunks)
        except Exception as e:
            logger.warning("Semantic scoring failed (%s) — using BM25 only", e)
            sem = {}
    else:
        sem = {}

    # Blame prior (learned from merged auto-fix PRs) joins as a third signal
    blame = blame or {}
    blame_weight = 0.2 if blame else 0.0
    sem_weight = (1.0 - blame_weight) / 2 if sem else 0.0
    bm25_weight = 1.0 - sem_weight - blame_weight

    def order(paths):
        return sorted(
            paths,
            key=lambda p: (
                -(bm25_weight * scores.get(p, 0.0)
                  + sem_weight * sem.get(p, 0.0)
                  + blame_weight * blame.get(p, 0.0)),
                -rank.get(p, 0.0),
            ),
        )

    full_paths = order(seeds)[:full_max]

    # 1-hop neighbors over the import graph (both directions), minus full tier
    neighbors = set()
    for src, dst in repo_map["edges"]:
        if src in full_paths and dst in source_files:
            neighbors.add(dst)
        if dst in full_paths and src in source_files:
            neighbors.add(src)
    # BM25 runners-up join the signature tier
    neighbors.update(p for p in order(source_files)[: sig_max] if p not in full_paths)
    neighbors -= set(full_paths)
    sig_paths = order(neighbors)[:sig_max]

    full = {p: contents.get(p, "") for p in full_paths}
    signatures = {p: _signature_block(source_files[p]) for p in sig_paths if source_files[p]["symbols"]}
    overview = {
        p: _overview_line(e)
        for p, e in source_files.items()
        if p not in full and p not in signatures
    }

    metrics = {
        "files_total": len(source_files),
        "files_full": len(full),
        "files_signatures": len(signatures),
        "tokens_sent": _estimate_tokens(
            list(full.values()) + list(signatures.values()) + list(overview.values())
        ),
        "tokens_full_repo": _estimate_tokens(contents.values()),
        "retrieval_ms": int((time.monotonic() - started) * 1000),
        "install_failure": hits.install_failure,
    }
    logger.info(
        "Context: %d/%d files full, %d sigs | ~%d tok vs ~%d full-repo (%d%%) | retrieval %dms",
        metrics["files_full"], metrics["files_total"], metrics["files_signatures"],
        metrics["tokens_sent"], metrics["tokens_full_repo"],
        100 * metrics["tokens_sent"] // max(metrics["tokens_full_repo"], 1) - 100,
        metrics["retrieval_ms"],
    )
    return TieredContext(full=full, signatures=signatures, overview=overview, metrics=metrics)
