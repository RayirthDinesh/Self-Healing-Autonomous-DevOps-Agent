"""FastAPI webhook server for the Self-Healing SRE Agent.

GitHub Actions calls this server after every CI run on a watched branch.
On a "failure" payload this is where the agent pipeline would kick off
(diagnose -> write fix -> test in sandbox -> open PR). For now it validates
the request, authenticates it, and logs it cleanly.

Run locally:   python main.py
Health check:  curl http://localhost:8000/health
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from models import WebhookPayload

# --- Configuration -------------------------------------------------------

# Load WEBHOOK_SECRET (and anything else) from a .env file sitting next to
# this script, so secrets are never hardcoded or committed.
load_dotenv()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# --- Logging -------------------------------------------------------------

# INFO level with a timestamp so each incoming request is traceable.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sre-agent-webhook")

# --- App -----------------------------------------------------------------

app = FastAPI(title="Self-Healing SRE Agent Webhook")


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    """Authenticate every request via the X-Webhook-Secret header.

    /health is exempt so uptime checks don't need the secret. Any other path
    must present a header matching WEBHOOK_SECRET, otherwise we return 401.
    """
    if request.url.path != "/health":
        provided = request.headers.get("X-Webhook-Secret")
        if not WEBHOOK_SECRET or provided != WEBHOOK_SECRET:
            logger.warning("Rejected request to %s: bad webhook secret", request.url.path)
            # Returned as JSON via a small inline response to keep middleware simple.
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "invalid webhook secret"})
    return await call_next(request)


@app.get("/health")
def health():
    """Liveness probe — confirms the server process is up and serving."""
    return {"status": "ok"}


@app.post("/webhook")
def webhook(payload: WebhookPayload):
    """Receive a CI result from GitHub Actions.

    FastAPI has already validated the body against WebhookPayload by the time
    we get here. We log the run, then branch on status:
      - failure -> this is where the agent would be triggered
      - success -> nothing to do, a fix (or clean run) succeeded
    """
    # One concise INFO line per request: timestamp comes from the log format.
    logger.info(
        "Incoming run | branch=%s commit=%s status=%s",
        payload.branch,
        payload.commit_sha,
        payload.status,
    )

    if payload.status == "failure":
        # Full, readable dump of the failed run for the agent / portfolio demo.
        logger.info("=== CI FAILURE ===")
        logger.info("repo:            %s", payload.repo)
        logger.info("branch:          %s", payload.branch)
        logger.info("commit_sha:      %s", payload.commit_sha)
        logger.info("workflow_run_id: %s", payload.workflow_run_id)
        logger.info("test_logs:\n%s", payload.test_logs)
        logger.info("==================")
        return {"received": True, "action": "triggering agent"}

    # Any non-failure status (expected: "success").
    logger.info("CI success on %s (%s) — no action needed", payload.branch, payload.commit_sha)
    return {"received": True, "action": "no action needed"}


if __name__ == "__main__":
    # Bind to all interfaces so it's reachable on the EC2 box, port 8000.
    # http="httptools" (from uvicorn[standard]) instead of the default h11
    # parser: h11 mishandles "Expect: 100-continue" from clients like curl
    # over real network latency, dropping the request body. httptools handles
    # it correctly.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, http="httptools")
