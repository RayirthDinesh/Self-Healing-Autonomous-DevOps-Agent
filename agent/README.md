# Setting up the SRE Agent

How to run the agent, point it at a real codebase, and deploy it on an Oracle
Cloud Always-Free VM.

For the VM creation and firewall specifics, [DEPLOY.md](../DEPLOY.md) is the
step-by-step reference. This document covers the whole path: local run, target
repo requirements, CI wiring, cloud deploy, and the console.

---

## What one run does

```
push to a watched branch
  -> GitHub Actions runs pytest
  -> POST {repo, branch, commit_sha, test_logs, status} to this server
  -> agent clones the failing branch into a temp dir
  -> reads the traceback, builds a tree-sitter map, picks the guilty files
  -> writes a patch as search/replace blocks
  -> runs the full suite in a throwaway Docker container
  -> green: push autofix/<branch>-<sha> and open a PR
     red:   retry up to 3 times, then stand down and push nothing
```

Roughly 60 to 90 seconds and a handful of model calls per run.

---

## Before you start: will it work on your repo?

The agent is not repo-agnostic magic. Four things must hold, and it is worth
checking them before deploying anything.

| Requirement | Why |
|---|---|
| **Public GitHub repo** | The agent clones over plain HTTPS with no credentials (`repo_ops.clone_branch`). Private repos will not clone until you change that call to embed a token. |
| **`requirements.txt` at the repo root** | The validator container runs `pip install -r requirements.txt` before the suite. No file, no run. |
| **`pytest` passes on `python:3.11-slim`** | That is the exact image the validator uses. If your suite needs system packages, a database, or a different Python, the validator will never go green and no PR will ever open. |
| **`main` is the PR base** | `create_pull_request` targets `main`. Change it in `github_ops.py` if your default branch differs. |

The agent will refuse to edit anything under `tests/`, `agent/`, `.git/` or
`.github/`, or any path containing `/test`. That is deliberate: it is what stops
a failing assertion from being "fixed" by deleting the assertion.

---

## 1. Run it locally first

Prove the pipeline works on your machine before putting it on a VM.

```bash
git clone https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent.git
cd Self-Healing-Autonomous-DevOps-Agent/agent
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

You also need **Docker running**, because the validator runs the suite in a
container. Without it every attempt reports red and nothing is ever published.

```bash
docker run --rm python:3.11-slim python -c "print('docker ok')"
```

Create `agent/.env` (see the reference below), then start the server:

```bash
python main.py                 # webhook server on :8000
```

Give it a real run. There is no simulation mode: the only way in is the same
`/webhook` payload GitHub Actions posts, carrying real pytest output. Capture
some from a branch that genuinely fails, then post it:

```bash
git clone -b bug/easy-1-nameerror https://github.com/RayirthDinesh/sre-demo-app /tmp/demo
(cd /tmp/demo && pytest -v --tb=long > run.log 2>&1)

python3 - > payload.json <<'PY'
import json, subprocess
sha = subprocess.run(["git", "-C", "/tmp/demo", "rev-parse", "HEAD"],
                     capture_output=True, text=True).stdout.strip()
print(json.dumps({
    "repo": "RayirthDinesh/sre-demo-app",
    "branch": "bug/easy-1-nameerror",
    "commit_sha": sha,
    "test_logs": open("/tmp/demo/run.log").read(),
    "status": "failure",
}))
PY

curl -sS -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d @payload.json
```

From there it is the production path: clone, map, patch, validate in Docker,
and a PR if `GITHUB_TOKEN` is set. Watch it happen in the console:

```bash
python dashboard.py            # http://127.0.0.1:8001/ui
```

---

## 2. Environment reference

`agent/.env`, never committed (it is gitignored):

| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | Model access. Get one at openrouter.ai. |
| `WEBHOOK_SECRET` | for CI | Shared secret GitHub Actions sends as `X-Webhook-Secret`. |
| `GITHUB_TOKEN` | to open PRs | Classic PAT with the `repo` scope, or a fine-grained token with Contents and Pull requests write on the target repo. Without it the agent still fixes and validates, then logs "skipping push and PR". |
| `LLM_MODEL` | no | Default `tencent/hy3-preview`. Any OpenRouter model id works, including free ones such as `inclusionai/ling-3.0-flash:free`. |
| `TRIAGE_MODEL` | no | Cheaper model for the triage and review nodes. Falls back to `LLM_MODEL`. |
| `MEMORY_DB` | no | SQLite path. Default `~/.sre-agent/memory.db`. |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | no | Console bind. Default `127.0.0.1:8001`. |
| `DASHBOARD_SECRET` | when exposed | Read-only console secret. Falls back to `WEBHOOK_SECRET`. |

**Write `.env` without a byte order mark.** A BOM makes the first key parse as
`﻿WEBHOOK_SECRET`, so the real variable stays unset and every request 401s.
PowerShell's `Set-Content -Encoding utf8` adds one. On the VM use:

```bash
printf 'WEBHOOK_SECRET=%s\n' "$SECRET" > .env
printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >> .env
```

---

## 3. Point it at your own codebase

### a) Add the workflow

In the repo you want watched, create `.github/workflows/ci.yml`. The important
parts are capturing the full output into `run.log` and always notifying, on
success as well as failure, so the agent can resolve the fate of PRs it opened.

```yaml
name: CI
on:
  push:
    branches: [main, "bug/*"]      # watch whichever branches you like

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        shell: bash
        run: |
          set -o pipefail
          pip install -r requirements.txt 2>&1 | tee -a run.log

      - name: Run tests
        shell: bash
        run: |
          set -o pipefail
          pytest -v --tb=long 2>&1 | tee -a run.log

      - name: Notify the agent
        if: always()
        shell: bash
        env:
          REPO: ${{ github.repository }}
          BRANCH: ${{ github.ref_name }}
          COMMIT_SHA: ${{ github.sha }}
          RUN_ID: ${{ github.run_id }}
          JOB_STATUS: ${{ job.status }}
          WEBHOOK_URL: ${{ secrets.WEBHOOK_URL }}
          WEBHOOK_SECRET: ${{ secrets.WEBHOOK_SECRET }}
        run: |
          if [ "$JOB_STATUS" = "success" ]; then STATUS="success"; else STATUS="failure"; fi
          STATUS="$STATUS" python3 - > payload.json <<'PY'
          import json, os
          try:
              logs = open("run.log").read()
          except FileNotFoundError:
              logs = "no logs were captured"
          print(json.dumps({
              "repo": os.environ["REPO"],
              "branch": os.environ["BRANCH"],
              "commit_sha": os.environ["COMMIT_SHA"],
              "workflow_run_id": os.environ["RUN_ID"],
              "test_logs": logs,
              "status": os.environ["STATUS"],
          }))
          PY

          # A trailing newline in a GitHub secret becomes an embedded newline in
          # the header, which makes the request malformed before it is parsed.
          WEBHOOK_URL="$(printf %s "$WEBHOOK_URL" | tr -d '[:space:]')"
          WEBHOOK_SECRET="$(printf %s "$WEBHOOK_SECRET" | tr -d '[:space:]')"

          curl -sS -X POST "$WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
            -d @payload.json
```

`test_logs` is what the agent reasons over, so keep `-v --tb=long`. A bare
`pytest -q` gives it almost nothing to work with.

### b) Add the secrets

Repo, Settings, Secrets and variables, Actions:

| Secret | Value |
|---|---|
| `WEBHOOK_URL` | `http://<vm-ip>:8000/webhook` |
| `WEBHOOK_SECRET` | the same string as the server's `.env` |

Paste with no trailing newline.

### c) Try it

Push a deliberately broken commit to a watched branch. CI fails, the server logs
the incoming run, and a PR appears within a couple of minutes if the fix holds.

---

## 4. Deploy on Oracle Cloud (Always Free)

[DEPLOY.md](../DEPLOY.md) has the full VM walkthrough: creating the
`VM.Standard.A1.Flex` instance, assigning a public IP, and the two firewalls
(the Oracle security list **and** the VM's iptables, where the rule must be
inserted before the catch-all REJECT). Follow it through section 3, then come
back here.

### Install the runtime, including Docker

DEPLOY.md installs Python but not Docker. The validator cannot run without it,
so every fix would be reported red:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu        # log out and back in for this to apply
docker run --rm python:3.11-slim python -c "print('docker ok')"
```

The Ampere shape is arm64. `python:3.11-slim` is multi-arch so it pulls fine,
but note the suite then runs on arm64: if your project depends on x86-only
wheels, pin the container image instead in `repo_ops.run_tests`.

### Install the agent

```bash
git clone https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent.git
cd Self-Healing-Autonomous-DevOps-Agent/agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
printf 'WEBHOOK_SECRET=...\nOPENROUTER_API_KEY=...\nGITHUB_TOKEN=...\n' > .env
python main.py                        # smoke test
curl http://localhost:8000/health     # {"status":"ok"}
```

### Run it as a service

```bash
sudo tee /etc/systemd/system/sre-agent.service > /dev/null <<'EOF'
[Unit]
Description=SRE Agent Webhook Server
After=network.target docker.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Self-Healing-Autonomous-DevOps-Agent/agent
ExecStart=/home/ubuntu/Self-Healing-Autonomous-DevOps-Agent/agent/.venv/bin/python main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

pkill -f "python main.py"
sudo systemctl daemon-reload
sudo systemctl enable --now sre-agent
sudo journalctl -u sre-agent -f
```

`After=docker.service` matters: on reboot the agent must not accept a webhook
before the Docker daemon is up.

### Keep the IP stable

The public IP changes on stop and start, which silently breaks `WEBHOOK_URL`.
Either leave the instance running or attach a reserved public IP.

---

## 5. Watching runs

The console shows the identified error, the files being edited with diffs, test
results per attempt, which node is executing, and every model call.

**On your own machine**, no configuration and no secret:

```bash
python agent/dashboard.py        # http://127.0.0.1:8001/ui
```

It reads the same SQLite file the pipeline writes, so runs from the webhook
server and the SWE-bench harness both appear, as long as they share a
`MEMORY_DB`. It only shows runs recorded on that machine.

**On the VM**, the console is already mounted on the webhook server at
`http://<vm-ip>:8000/ui`, and there it requires a secret on every request.
Prefer setting `DASHBOARD_SECRET` so that viewing does not also grant the
ability to POST a fake CI failure and start runs.

Since that is plain HTTP, the safer option is not to expose it at all:

```bash
ssh -i your-key.key -L 8001:localhost:8001 ubuntu@<vm-ip>
# on the VM: python dashboard.py
# then open http://127.0.0.1:8001/ui in your own browser
```

Run rows hold repo diffs, CI logs and full model prompts, so treat access to the
console as access to the source of whatever repo the agent watches.

---

## 6. Troubleshooting

| Symptom | Cause |
|---|---|
| Every request 401s, secret looks correct | Byte order mark in `.env`. Rewrite it with `printf`. |
| `Invalid HTTP request received`, no 401 and no 422 | Trailing newline in a GitHub secret. The workflow above strips whitespace. |
| CI posts, server returns 422 | Old uvicorn h11 parser mishandling `Expect: 100-continue`. `main.py` already runs `http="httptools"`; make sure `uvicorn[standard]` is installed. |
| Fixes always report red, tests pass by hand | Docker is missing, stopped, or the service user is not in the `docker` group. |
| `HTTP 402 Payment Required` in the logs | OpenRouter balance exhausted. Switch `LLM_MODEL` to a `:free` model or top up. |
| Agent fixes the suite but opens no PR | `GITHUB_TOKEN` unset or lacking the `repo` scope. The log says "skipping push and PR". |
| Console is empty | No runs recorded yet in that `MEMORY_DB`. Only runs executed after the run tracker was added appear; older history lives in the `incidents` table, which the console does not read. |
| Console 401s in a browser on the VM | It is bound to a routable interface, so a secret is required. Use the SSH tunnel above. |
| Clone fails on a private repo | `clone_branch` clones over unauthenticated HTTPS. Embed a token there to support private repos. |

Server logs: `sudo journalctl -u sre-agent -f`.

---

## Cost

Oracle Always Free covers the VM. The only recurring cost is model calls,
typically four to twelve per run depending on retries. Free OpenRouter models
work for the whole pipeline; they are slower and less reliable at producing
valid JSON, which shows up as an occasional wasted attempt.
