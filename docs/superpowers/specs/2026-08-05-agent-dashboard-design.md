# Agent Run Dashboard - Design

**Date:** 2026-08-05
**Status:** approved, implementing

## Problem

The SRE agent runs headless. The only visibility is `logger.info` lines on the VM
and rows in `~/.sre-agent/memory.db`. There is no way to watch a run happen, see
which files the fixer touched, whether the suite went green, or what each agent
node actually did.

## Goal

A web UI that shows, live and historically:

- the error the agent identified (error class, triage summary, diagnosis)
- which files are being edited, with diffs
- unit test results per validation attempt
- which pipeline step is active (mapping the repo, patching, verifying, PR-ing)
- what each agent node is doing, including every LLM call

## Non-goals

- Editing/approving fixes from the UI (read-only)
- Multi-user accounts (single shared secret)
- Metrics/alerting backend (out of scope)

## Architecture

SQLite is the single source of truth. The webhook server, the local replay
script, and the SWE-bench harness all run on the same box and all write to
`~/.sre-agent/memory.db`. The UI reads that DB and never imports pipeline code,
so a run in any process shows up in the dashboard.

```
graph nodes ─┐
legacy pipe  ├─> run_tracker ──> memory.db (runs, events, artifacts)
replay/swe  ─┘                        │
                                      v
                          dashboard.py (FastAPI) ──> dashboard.html
                            /api/runs, /api/runs/{id},
                            /api/artifacts/{id}, /stream (SSE)
```

### Schema (additive; `agent_steps` untouched)

```sql
runs(id TEXT PK, repo, branch, commit_sha, source, instance_id,
     status, outcome, pr_url, error_class, llm_calls, attempts,
     started_at, finished_at)
events(id, run_id, seq, node, phase, status, detail JSON,
       duration_ms, created_at)
artifacts(id, run_id, event_seq, kind, name, body, created_at)
```

`events` stays small so SSE polling is cheap; `artifacts` holds the large blobs
(diffs, test output, LLM prompt/response, repo map JSON), fetched on demand.

`source` is one of `webhook` (graph or legacy pipeline), `replay`, `swebench`.
`status` is `running | passed | failed | error`.

### run_tracker module

`agent/run_tracker.py` exposes `start_run`, `step`, `artifact`, `update_run`,
`finish_run`, and a `tracked(node_name)` decorator. The active run id lives in a
`contextvars.ContextVar`, so nodes do not thread it through `AgentState`.

Every function is wrapped in a never-fatal decorator (same contract as
`memory.py`): tracking failures log a warning and return a neutral value. The
dashboard must never be able to kill a fix.

### Instrumentation

- `agent_graph.run_graph` - opens and closes the run.
- `graph_nodes` - every node wrapped with `@tracked`; emits start/end events with
  durations and a per-node summary. Explicit artifacts: repo map stats (ingest),
  proposed fixes (fixer), unified diff + full test output (validator).
- `graph_nodes._chat` - one `llm_call` artifact per call: node, model, latency,
  prompt, response.
- `pipeline._run_legacy` - same tracking at its own step boundaries, since
  `AGENT_MODE` defaults to `legacy`.
- `scripts/replay_bugs.py` and `swe-bench-agent-eval/swe_harness.py` - manual
  `start_run`/`step`/`finish_run` around each bug/instance.

### HTTP surface

`agent/dashboard.py` builds an `APIRouter` mounted into the existing FastAPI app
in `main.py`, and also runs standalone (`python dashboard.py`, port 8001) so local
eval runs are viewable without the webhook server.

```
GET /ui                       single-page HTML
GET /api/runs?limit=&source=  run list
GET /api/runs/{id}            run + events + artifact index
GET /api/artifacts/{id}       one artifact body
GET /api/runs/{id}/stream     SSE; tails events by seq
```

Auth depends on where the server listens, so that a fresh clone is usable with
no setup while an exposed one cannot be left open:

- **Local**: a loopback bind enables local mode. Requests from `127.0.0.1`
  carrying a loopback Host header pass without a secret. The Host check blocks
  DNS rebinding. Importing the router into another app never enables this, so
  mounting on the public webhook server stays authenticated.
- **Exposed**: any other bind requires `DASHBOARD_SECRET`, falling back to
  `WEBHOOK_SECRET`, via `X-Webhook-Secret`, `?key=` or the `sre_key` cookie,
  compared with `hmac.compare_digest`. `/ui?key=…` sets an HttpOnly cookie and
  redirects to a bare `/ui` so the secret leaves the query string after one hop.
  The standalone entry point refuses to start on a routable bind with no secret.

`DASHBOARD_SECRET` is preferred for sharing: `WEBHOOK_SECRET` also authorizes
`POST /webhook`, which starts a run and can push a branch.

### Frontend

`agent/static/dashboard.html` - one file, no build step, no external assets.

- Left rail: run list with status pill, source badge, repo@branch, elapsed time.
- Top strip: the nine pipeline nodes with pending/active/done/failed/skipped
  states; validator carries the attempt counter for retry loops.
- Panels: Errors (class, triage, diagnosis), Files edited (tabs + unified diff),
  Tests (pass/fail counts parsed from the pytest summary, per-attempt history,
  raw tail), Codebase map (file/symbol/language counts, candidate files),
  Agent activity (chronological LLM calls with expandable prompt/response).
- Live updates over SSE; falls back to polling if the stream drops.

## Error handling

Tracking is advisory everywhere. The API returns 404 for unknown run ids, 401
without a valid secret. The SSE endpoint closes when the run reaches a terminal
status. A missing `static/dashboard.html` returns a 500 with a clear message
rather than a stack trace.

## Testing

- `agent/tests/test_run_tracker.py` - run lifecycle, event ordering, artifact
  round-trip, never-fatal on an unwritable DB.
- `agent/tests/test_dashboard_api.py` - 401 without key, run list shape, run
  detail shape, artifact fetch, unknown-run 404.

## Dependencies

None new. FastAPI, uvicorn, pydantic and sqlite3 are already in use.
