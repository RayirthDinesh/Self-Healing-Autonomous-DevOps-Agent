# Self-Healing Autonomous SRE Agent

[![CI](https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

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

## Quick start

Two things are needed before anything runs: **Docker** (the fix is validated in
a throwaway container) and an **OpenRouter API key**.

```bash
git clone https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent.git
cd Self-Healing-Autonomous-DevOps-Agent
cp agent/.env.example agent/.env     # fill in OPENROUTER_API_KEY and WEBHOOK_SECRET
docker compose up --build            # webhook on :8000
```

Check the machine before expecting a run to work:

```bash
python agent/doctor.py                       # or --repo owner/name
```

It verifies Python, dependencies, the Docker daemon, the model your key can
actually call, token scopes and the run database — and prints what to do about
anything it finds. Exit status is non-zero on a blocking problem, so it can
gate a deploy.

Then drive a complete run against a canned real CI failure — no repo of your
own to break first, no CI to wait for:

```bash
scripts/try-it.sh                    # posts examples/sample-payload.json
```

Watch it at `http://localhost:8000/ui?key=<WEBHOOK_SECRET>` — under Compose the
run history lives in a container volume, so use the console the server itself
serves rather than starting one on the host.

That payload is genuine pytest output from `bug/easy-1-nameerror`, so it drives
the whole production path: clone, map, retrieve, patch, validate in Docker, PR.
Leave `GITHUB_TOKEN` unset and the run stops cleanly after validation instead of
trying to push.

Prefer a virtualenv to Docker Compose? `pip install -r agent/requirements.txt`
then `cd agent && python main.py`. Full detail in
[agent/README.md](agent/README.md).

## Layout

```
.
├── docker-compose.yml    # webhook + optional console, one command
├── Dockerfile            # runtime image (git + docker CLI + deps)
├── examples/             # sample-payload.json: a real captured CI failure
├── scripts/
│   ├── try-it.sh             # post the sample payload at a running server
│   └── capture-payload.sh    # build a payload from any repo/branch
└── agent/
    ├── main.py           # FastAPI webhook server (X-Webhook-Secret auth, /health)
    ├── models.py         # WebhookPayload schema
    ├── config.py         # env-backed settings: validator image, PR base branch
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
    ├── graph_nodes.py    # the nodes themselves, one function per agent
    ├── run_tracker.py    # SQLite event log every pipeline run writes to
    ├── dashboard.py      # read-only web UI + SSE streaming API over that log
    ├── static/           # dashboard.html (single file, no build step)
    └── tests/            # pytest suite, run in CI on 3.11 and 3.12
```

## Benchmark results

Evaluated on all 57 instances using the same model (`claude-haiku-4.5`) and
the same patch format across all three modes — the only variable is what goes
into the context window:

| Mode | Context sent to LLM | Resolved | Resolve rate |
|---|---|---|---|
| Raw | Problem statement only, no files | 0/57 | 0% |
| Naive | Full repo dump, no scoring | 8/57 | 14.0% |
| **Full pipeline** | **Focused RAG + failing test files** | **13/57** | **22.8%** |

The full pipeline outperforms the naive baseline by **63% relative** (22.8% vs
14.0%), isolating the contribution of hybrid retrieval. The raw baseline
confirms the LLM cannot fix bugs without seeing the relevant source code.

**How each mode differs:**
- **Raw** — sends only the problem statement and failing test names. Zero files.
- **Naive** — dumps every `.py` file in the repo with no filtering or scoring,
  flooding the model with irrelevant code.
- **Full pipeline** — uses BM25 keyword search + semantic embeddings to select
  the most relevant files, always includes the failing test source so the LLM
  sees what assertions must pass, and retries with regression feedback if the
  first fix breaks other tests.

## Watching a run

The dashboard shows, live and for past runs: the identified error, the files
being edited (with diffs), unit-test results per attempt, which pipeline step is
active, the codebase map the agent built, and every LLM call each agent node
made.

Clone the repo, run it, watch your own runs. No configuration:

```bash
python agent/dashboard.py        # http://127.0.0.1:8001/ui
```

Runs from the webhook server and from the SWE-bench harness write to the same
SQLite file (`~/.sre-agent/memory.db`, override with `MEMORY_DB`), so pointing
both at one path shows them in one place.

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
- **Retries only what retrying can fix** — a bad key, exhausted credits, or a
  retired model id stops the run on the first call with the actual reason,
  rather than spending the whole attempt budget failing the same way.
- **Degrades instead of dying** — if the configured model cannot be called at
  all, the run continues on a free fallback rather than stopping. The
  preferred model is tried again at the start of every run, so topping up a
  balance resumes normal service with no restart. A fallback fix still has to
  turn the suite green before anything is published.

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

Your target repo needs `requirements.txt` at its root and a suite that passes
inside the validator container. Everything else adapts through the environment
rather than a source edit:

| If your repo… | Set |
|---|---|
| is private | `GITHUB_TOKEN` — the clone authenticates with it |
| does not default to `main` | `PR_BASE_BRANCH` |
| needs system packages or another Python | `VALIDATOR_IMAGE` |
| has a slow suite | `VALIDATOR_TIMEOUT` |

[agent/.env.example](agent/.env.example) is the complete environment reference.

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
setup, how to run the tests, and the invariants worth knowing before changing
them.

## Security

The agent executes model-written code and holds a token that can write to your
repository. [SECURITY.md](SECURITY.md) covers the threat model, how to scope
the token, and how to report a vulnerability privately.

## Licence

[Apache 2.0](LICENSE).
