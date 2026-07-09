# Self-Healing Autonomous SRE Agent

An autonomous agent that watches a repo's CI. When a build or test run fails,
GitHub Actions POSTs the logs to this agent's webhook server; the agent clones
the failing branch, asks an LLM to diagnose the root cause and write a fix,
validates the fix by re-running the full test suite locally, and — only if the
suite goes green — pushes an `autofix/*` branch and opens a pull request.

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
└── .env.example    # WEBHOOK_SECRET, OPENROUTER_API_KEY, GITHUB_TOKEN, LLM_MODEL
```

## Design choices

- **Fix must earn the PR** — the patched clone re-runs the whole pytest suite;
  a fix that doesn't turn it green is discarded.
- **The LLM never sees the tests** — only `src/` and `requirements.txt` are
  sent, so it can't "fix" a failure by rewriting the assertions.
- **Server answers instantly** — the pipeline runs as a FastAPI background
  task so GitHub's webhook call never times out.

## Deploying

See [DEPLOY.md](DEPLOY.md) — full walkthrough for an Oracle Cloud Always-Free
VM (systemd service, dual firewalls, and every gotcha hit along the way:
UTF-8 BOM in `.env`, trailing newlines in secrets, uvicorn's h11
`Expect: 100-continue` bug).

## Point it at your own repo

1. Deploy the server, set its `.env` (secret, OpenRouter key, GitHub PAT).
2. In your repo, add a CI step that POSTs `{repo, branch, commit_sha,
   workflow_run_id, test_logs, status}` to `http://<server>:8000/webhook`
   with the `X-Webhook-Secret` header (see sre-demo-app's
   `.github/workflows/ci.yml` for a copy-paste example).
3. Add `WEBHOOK_URL` + `WEBHOOK_SECRET` as Actions secrets. Done.
