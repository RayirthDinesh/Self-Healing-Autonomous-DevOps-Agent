# Self-Healing Autonomous SRE Agent

An autonomous agent that watches a repo's CI. When a build or test run fails,
GitHub Actions POSTs the logs to this agent's webhook server; the agent clones
the failing branch, retrieves the most relevant source files using hybrid RAG,
asks an LLM to diagnose the root cause and write a fix, validates the fix by
re-running the full test suite in Docker, and — only if the suite goes green —
pushes an `autofix/*` branch and opens a pull request.

**SWE-bench Verified:** 23/57 instances resolved (40.3%) on a 57-instance
subset, compared to a 14/57 (24.6%) naive baseline that dumps the full repo
without retrieval — a 64% relative improvement from the RAG pipeline.

Watch it in action against the demo target repo:
[sre-demo-app](https://github.com/RayirthDinesh/sre-demo-app) — ten `bug/*`
branches with planted failures from easy (typo → NameError) to extra-hard
(silently wrong values, no crash).

## How it works

```
push to bug/* ─→ CI fails ─→ POST logs to webhook (FastAPI, port 8000)
                                        │
                       clone failing branch (temp dir)
                                        │
              tree-sitter repo map + hybrid RAG retrieval
              (BM25 keyword + semantic embeddings + blame history)
                                        │
              LLM diagnosis + search/replace patch (OpenRouter)
                                        │
              re-run pytest in throwaway Docker container
                                        │
              green? ─→ push autofix/<branch>-<sha> ─→ open PR
              red?  ─→ regression feedback ─→ retry once, then stand down
```

## Layout

```
agent/
├── main.py           # FastAPI webhook server (X-Webhook-Secret auth, /health)
├── models.py         # WebhookPayload schema
├── pipeline.py       # orchestrator: clone → retrieve → fix → validate → PR
├── retrieval.py      # tiered context selection: full / signatures / overview
├── repo_map.py       # tree-sitter AST graph + PageRank over the import graph
├── chunker.py        # function-level code chunking for semantic search
├── embeddings.py     # fastembed semantic similarity scoring
├── llm_client.py     # OpenRouter call, strict JSON search/replace format
├── repo_ops.py       # git clone/apply/test/push (subprocess + Docker)
├── github_ops.py     # PR creation via GitHub REST API
├── memory.py         # incident store: blame priors + few-shot retrieval
├── agent_graph.py    # LangGraph multi-agent mode (triage→fixer→critic→validator)
├── run_tracker.py    # SQLite event log every pipeline run writes to
├── dashboard.py      # read-only web UI + SSE streaming API over that log
├── static/           # dashboard.html (single file, no build step)
└── scripts/
    └── replay_bugs.py  # replay all demo bug branches; --live calls the LLM
```

## Benchmark results

Evaluated on a 57-instance subset of SWE-bench Verified (real issues from
pytest, astropy, sympy, requests, and pylint):

| Mode | Resolve rate | Empty patches |
|---|---|---|
| Raw (no file context) | ~5% | high |
| Naive (full repo dump, no RAG) | 24.6% (14/57) | 33/57 |
| **Full pipeline (RAG + test context)** | **40.3% (23/57)** | low |

The naive baseline sends every file in the repo to the LLM with no scoring.
The full pipeline uses hybrid retrieval to send only the most relevant files,
adds the failing test source so the LLM sees exactly what assertions must pass,
and retries with regression feedback if the first fix breaks other tests.

## Watching a run

The dashboard shows, live and for past runs: the identified error, the files
being edited (with diffs), unit-test results per attempt, which pipeline step is
active, the codebase map the agent built, and every LLM call each agent node
made.

Clone the repo, run it, watch your own runs. No configuration:

```bash
python agent/dashboard.py        # http://127.0.0.1:8001/ui
```

Runs from the webhook server, `scripts/replay_bugs.py`, and the SWE-bench
harness all write to the same SQLite file (`~/.sre-agent/memory.db`, override
with `MEMORY_DB`) and appear in one place.

### Access

**Local (default).** Bound to loopback — no secret required. A DNS-rebinding
guard checks that requests carry a `localhost` Host header.

**Exposed.** Set `DASHBOARD_HOST` to a routable address, or reach the console
on the webhook server at `http://<server>:8000/ui`. Every request must carry
a secret via `?key=` (moved to a cookie on first load), an `sre_key` cookie,
or the `X-Webhook-Secret` header.

Prefer `DASHBOARD_SECRET` over `WEBHOOK_SECRET` for read-only dashboard access.
Over the public internet, put TLS in front or use `ssh -L 8001:localhost:8001`.

## Design choices

- **Fix must earn the PR** — the patched clone re-runs the whole pytest suite
  in Docker; a fix that doesn't turn it green is discarded, never pushed.
- **Retrieval, not full-repo dump** — hybrid BM25 + semantic search picks the
  files most likely to contain the bug. The failing test source is also included
  so the LLM sees what assertions must pass, without being able to delete them.
- **Search/replace patching** — the LLM outputs exact `search`/`replace` blocks
  rather than full rewrites, which keeps diffs minimal and avoids clobbering
  unrelated lines.
- **Regression feedback on retry** — if the first fix breaks previously passing
  tests, the LLM is told exactly which tests regressed before it tries again.
- **Server answers instantly** — the pipeline runs as a FastAPI background task
  so GitHub's webhook call never times out.

## Setup

**[agent/README.md](agent/README.md)** is the full guide: running locally,
target repo requirements, CI wiring, environment reference, deploying on an
Oracle Cloud Always-Free VM, and a troubleshooting table.

**[DEPLOY.md](DEPLOY.md)** is the VM-level walkthrough: instance creation,
firewalls, systemd, and common gotchas.

The short version:

1. Deploy the server, install Docker, set `.env` (webhook secret, OpenRouter
   key, GitHub PAT).
2. In the repo you want watched, add a CI step that POSTs
   `{repo, branch, commit_sha, workflow_run_id, test_logs, status}` to
   `http://<server>:8000/webhook` with the `X-Webhook-Secret` header.
3. Add `WEBHOOK_URL` and `WEBHOOK_SECRET` as Actions secrets.

Your target repo needs to be public, have `requirements.txt` at its root, and
have a suite that passes under `python:3.11-slim` — that is the exact image
the validator uses.
