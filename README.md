# Self-Healing Autonomous SRE Agent

An autonomous agent that watches a repo's CI. When a build or test run fails,
GitHub Actions POSTs the logs to this agent's webhook server; the agent clones
the failing branch, asks an LLM to diagnose the root cause and write a fix,
validates the fix by re-running the full test suite locally, and - only if the
suite goes green - pushes an `autofix/*` branch and opens a pull request.

Watch it in action against the demo target repo:
[sre-demo-app](https://github.com/RayirthDinesh/sre-demo-app) - ten `bug/*`
branches with planted failures from easy (typo → NameError) to extra-hard
(silently wrong values, no crash).

## How it works

```
push to bug/* ─→ CI fails ─→ POST logs to webhook (FastAPI, port 8000)
                                        │
                       clone failing branch (temp dir)
                                        │
                LLM diagnosis + fix  (OpenRouter, tencent/hy3-preview)
                                        │
                    re-run pytest on the patched clone
                                        │
              green? ─→ push autofix/<branch>-<sha> ─→ open PR
              red?  ─→ log and stop (never ships an unverified fix)
```

## Layout

```
agent/
├── main.py         # FastAPI webhook server (X-Webhook-Secret auth, /health)
├── models.py       # WebhookPayload schema
├── pipeline.py     # orchestrator: clone → diagnose → fix → validate → PR
├── llm_client.py   # OpenRouter call, strict-JSON fix format
├── repo_ops.py     # git clone/apply/test/push (subprocess)
├── github_ops.py   # PR creation via REST API
├── run_tracker.py  # run/event/artifact log every pipeline writes to
├── dashboard.py    # read-only web UI + JSON/SSE API over that log
├── static/         # dashboard.html (single file, no build step)
└── .env.example    # WEBHOOK_SECRET, OPENROUTER_API_KEY, GITHUB_TOKEN, LLM_MODEL
```

## Watching a run

The dashboard shows, live and for past runs: the identified error, the files
being edited (with diffs), unit-test results per attempt, which pipeline step is
active, the codebase map the agent built, and every LLM call each agent node
made.

Clone the repo, run it, watch your own runs. No configuration:

```
python agent/dashboard.py        # http://127.0.0.1:8001/ui
```

It reads `~/.sre-agent/memory.db` (override with `MEMORY_DB`), so runs from the
webhook server, `scripts/replay_bugs.py`, and the SWE-bench harness all appear.
Point them at the same `MEMORY_DB` to see them in one place.

### Access

Which access rules apply depends on where the server listens.

**Local (default).** Bound to loopback, so only processes on your machine can
reach it. No secret required. Requests must come from `127.0.0.1` and carry a
`localhost` Host header, which keeps a DNS-rebinding page from reading the
console out of your browser.

**Exposed.** Set `DASHBOARD_HOST` to a routable address, or reach the console on
the webhook server at `http://<server>:8000/ui`, and every request must carry a
secret. Supply it as `?key=` (moved straight into a cookie and redirected away,
so it does not linger in access logs), an `sre_key` cookie, or the
`X-Webhook-Secret` header. Refusing to start unprotected is deliberate: run rows
hold repo diffs, CI logs and full LLM prompts.

Prefer `DASHBOARD_SECRET` over `WEBHOOK_SECRET` when you share access.
`WEBHOOK_SECRET` also lets its holder POST a CI failure and start a run, which
spends model credits and can push a branch; `DASHBOARD_SECRET` only reads.

Over the public internet the secret still crosses the wire in clear text on
plain HTTP, so put TLS in front or use `ssh -L 8001:localhost:8001`.

## Design choices

- **Fix must earn the PR** - the patched clone re-runs the whole pytest suite;
  a fix that doesn't turn it green is discarded.
- **The LLM never sees the tests** - only `src/` and `requirements.txt` are
  sent, so it can't "fix" a failure by rewriting the assertions.
- **Server answers instantly** - the pipeline runs as a FastAPI background
  task so GitHub's webhook call never times out.

## Setup

**[agent/README.md](agent/README.md)** is the full guide: running it locally,
what your codebase must satisfy for the agent to work on it, the CI workflow to
copy in, environment reference, deploying on an Oracle Cloud Always-Free VM, and
a troubleshooting table.

[DEPLOY.md](DEPLOY.md) is the VM-level walkthrough it builds on: instance
creation, the two firewalls, systemd, and every gotcha hit along the way
(UTF-8 BOM in `.env`, trailing newlines in secrets, uvicorn's h11
`Expect: 100-continue` bug).

The short version:

1. Deploy the server, install Docker, set `.env` (webhook secret, OpenRouter
   key, GitHub PAT).
2. In the repo you want watched, add a CI step that POSTs `{repo, branch,
   commit_sha, workflow_run_id, test_logs, status}` to
   `http://<server>:8000/webhook` with the `X-Webhook-Secret` header.
3. Add `WEBHOOK_URL` and `WEBHOOK_SECRET` as Actions secrets.

Your target repo needs to be public, have `requirements.txt` at its root, and
have a suite that passes under `python:3.11-slim`, since that is what the
validator runs.
