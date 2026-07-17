# Phase 1 — Persistent Memory for the SRE Agent

Date: 2026-07-16
Status: approved (design conversation + AskUserQuestion decisions), implementing

## Goal

Give the agent a persistent, DB-backed "mental map" of the target repo that improves
retrieval and prompting on every run, and record structured per-run outcomes that
Phase 2 (LangGraph multi-agent rework) will consume for its error-class fast-path.

## Decisions locked

- Feedback signal: **suite green + PR fate** (merged / closed).
- Build order: **memory first, then LangGraph**.
- Storage: **SQLite on the VM** at `~/.sre-agent/memory.db` (override: `MEMORY_DB` env).
- PR fate: **lazy check at the start of the next run** (no polling daemon).
- Memory failures must **never kill a run** — every hook degrades to current behavior.

## Schema (`agent/memory.py`)

| table | purpose |
|---|---|
| `repo_state(repo, commit_sha, updated_at)` | last commit the map was synced at |
| `file_history(repo, path, sha256, size, symbol_count, last_seen_commit, times_changed)` | per-file snapshot + churn counter |
| `co_change(repo, path_a, path_b, count)` | files that changed together between observed pulls |
| `incidents(id, repo, branch, commit_sha, error_class, log_excerpt, log_embedding, diagnosis, files_fixed, fix_diff, suite_green, pr_url, pr_number, pr_state, attempt, created_at)` | one row per fix attempt that reached the test suite; failed attempts stored as negative examples (`suite_green=0`) |
| `blame(repo, path, error_class, weight)` | learned prior: which files historically fix which error class |
| `agent_steps(id, incident_id, step, detail, created_at)` | per-agent outcome log (Phase 2 consumes; populated lightly now) |
| `fast_paths(repo, error_class, signature, target_files, merged_count, miss_count, updated_at)` | Phase 2 router fast-path (table created now, unused until Phase 2) |

## Error classes

Regex buckets over the failing log: `name-error`, `import-error`, `type-error`,
`assertion-value`, `install-failure`, `other`.

## Feedback loop

- `update_pr_fates(repo, token)` runs at the start of each pipeline run: for every
  incident with `pr_state='open'`, one GitHub `GET /pulls/{n}`.
  - merged → `pr_state='merged'`, blame `weight += 1.0` for each fixed file under that error class.
  - closed unmerged → `pr_state='closed'`, blame `weight *= 0.5` (decay).
- `blame_scores(repo, error_class)` → `{path: weight}` normalized to [0,1].

## Retrieval integration (`retrieval.py`)

`select_context(..., blame=None)` gains a third ranking signal:

- blame present + semantic available: **BM25 0.4 / cosine 0.4 / blame 0.2**
- blame present, no semantic: BM25 0.8 / blame 0.2
- no blame: unchanged (0.5/0.5 or BM25-only)

## Prompt integration (`llm_client.py`)

`call_llm(test_logs, context, incidents=None)` — when `similar_incidents()` returns
matches (cosine ≥ 0.55, cap 2, diffs trimmed to ≤ 40 lines, `pr_state='closed'`
excluded, only `suite_green=1`), a `## Past incidents in this repo` block is inserted
after the Failed Test Output with the guard sentence:
*"Past incidents are historical hints — the current bug may differ. Verify against the code shown."*
Fallback when embeddings unavailable: match on same error class, most recent first.

## Pipeline hooks (`pipeline.py`)

1. After clone/map: `update_pr_fates()` (lazy sweep), `update_repo_state()` (snapshot +
   churn + co-change), `blame_scores()` → passed into `select_context`.
2. `similar_incidents()` → passed into `call_llm`.
3. After each `run_tests`: capture `git diff` (`repo_ops.get_diff`) and
   `record_incident(suite_green=passed)`.
4. After PR creation: `set_incident_pr(incident_id, pr_url)`.

Every hook is wrapped so a memory error logs a warning and the run continues.

## Deliberately deferred

- Incremental tree-sitter re-parse via per-file sha256: `build_map` takes ~84ms on the
  demo repo, so reusing unchanged parse results buys nothing yet. The sha256 snapshots
  in `file_history` are recorded now so the optimization can be added without a
  migration when repos get big.
- `fast_paths` population + router: Phase 2 (LangGraph).
