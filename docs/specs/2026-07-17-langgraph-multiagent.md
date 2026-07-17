# Phase 2 — LangGraph Multi-Agent Rework

Date: 2026-07-17
Status: approved in design conversation, implementing

## Goal

Replace the single monolithic LLM call with a LangGraph state machine of small,
specialized agents, wired into the Phase 1 memory so the system gets faster and
more targeted with every merged fix. Legacy pipeline stays the default
(`AGENT_MODE=legacy`); the graph activates with `AGENT_MODE=graph`.

## Graph

```
ingest → router ─(fast-path hit)→ fixer
              └─(miss)→ triage → localizer → fixer → critic ─(revise, max 1)→ fixer
                                                          └─(approve)→ validator
validator ─(green)→ publisher
          ├─(red, fast-path was used)→ demote fast-path → triage
          ├─(red, attempt < 3)→ localizer  (failure feedback attached)
          └─(red, attempts exhausted)→ reflect
```

- **ingest** — PR-fate sweep, error classification, blame priors, similar
  incidents, repo map + memory sync (all Phase 1 calls).
- **router** — deterministic, no LLM. Fast-path key =
  `(error_class, sorted failing test files, top traceback source path)`.
  Fires only when that exact signature has ≥ 2 merged PRs and < 2 misses and the
  target files still exist in the repo map. On fire: target files preloaded as
  fixer context, triage + localizer skipped.
- **triage** — cheap LLM (`TRIAGE_MODEL`, defaults to `LLM_MODEL`): one call,
  JSON `{summary, suspects}`.
- **localizer** — LLM with tools: `search_repo`, `read_file`, `get_signatures`,
  `get_importers`. Model-agnostic JSON tool protocol (no function-calling API
  dependency), hard cap 6 tool calls, then falls back to traceback + blame
  seeds. Output: ≤ 5 candidate files loaded full.
- **fixer** — main LLM. Context: candidate files, triage summary, past-incident
  few-shots, failure/critic feedback when retrying. Output validated through a
  Pydantic model; fixes touching `tests/`, `agent/`, `.github/` are dropped
  (guardrail preserved).
- **critic** — cheap LLM reviews the proposed diff against the diagnosis,
  JSON `{verdict: approve|revise, feedback}`. Max 1 revise round.
- **validator** — Docker `run_tests` (unchanged sandbox). Every attempt recorded
  as a Phase 1 incident (failures = negative examples). Red resets the workdir
  to a pristine clone before re-entering the loop.
- **publisher** — commit, push `autofix/*`, open PR, `set_incident_pr`.
- **reflect** — logs a run post-mortem into `agent_steps`, gives up cleanly.

## Budget rails

- Max **3 validator attempts** per run.
- Max **15 LLM calls** per run — any conditional edge routes to reflect when
  exceeded.

## Memory additions (`memory.py`)

- `incidents.signature` column (guarded `ALTER TABLE` migration).
- `failure_signature(test_logs)` — composite fast-path key.
- `update_pr_fates` also feeds `fast_paths`: merged incident with a signature →
  `merged_count += 1`, remembers target files.
- `fast_path_lookup(repo, test_logs)` / `fast_path_miss(repo, signature)`.
- Every node appends to `agent_steps` (incident-linked when one exists).

## Plumbing

- LLM via `langchain_openai.ChatOpenAI` pointed at OpenRouter
  (`base_url=https://openrouter.ai/api/v1`), same `OPENROUTER_API_KEY`.
- Checkpointing: LangGraph `SqliteSaver` into `~/.sre-agent/graph_checkpoints.db`
  when `langgraph-checkpoint-sqlite` is importable; silently skipped otherwise.
- `pipeline.run()` becomes a dispatcher: `AGENT_MODE=graph` → `agent_graph.run_graph`,
  anything else → the untouched legacy path.
- New deps in `agent/requirements.txt`: `langgraph`, `langchain-core`,
  `langchain-openai`, `langgraph-checkpoint-sqlite`.

## Files

| file | contents |
|---|---|
| `agent/graph_state.py` | `AgentState` TypedDict |
| `agent/graph_tools.py` | localizer tool belt bound to a workdir |
| `agent/graph_nodes.py` | all node functions + `_chat` LLM wrapper |
| `agent/agent_graph.py` | graph wiring, conditional edges, `run_graph()` |
