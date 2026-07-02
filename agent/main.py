"""FastAPI webhook server for the Self-Healing SRE Agent.

GitHub Actions calls this server after every CI run on a watched branch.
On failure: validates the request, then kicks off the agent pipeline in the
background (diagnose -> fix -> validate -> open PR).

Run locally:   python main.py
Health check:  curl http://localhost:8000/health
"""

import logging
import os

from dotenv import load_dotenv
load_dotenv()
from fastapi import BackgroundTasks, FastAPI, Request

from models import WebhookPayload
from pipeline import run as run_pipeline

# --- Configuration -------------------------------------------------------

# Load WEBHOOK_SECRET (and anything else) from a .env file sitting next to
# this script, so secrets are never hardcoded or committed.

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
def webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """Receive a CI result from GitHub Actions.

    FastAPI validates the body against WebhookPayload before this runs.
    We respond immediately (so GitHub doesn't time out), then run the
    full agent pipeline in the background.
    """
    logger.info(
        "Incoming run | branch=%s commit=%s status=%s",
        payload.branch,
        payload.commit_sha,
        payload.status,
    )

    if payload.status == "failure":
        logger.info("CI failure on %s — starting agent pipeline", payload.branch)
        # BackgroundTasks: FastAPI sends the HTTP response right now, then
        # runs this function after. The pipeline can take 60-90 seconds;
        # without this, GitHub would time out waiting for a response.
        background_tasks.add_task(
            run_pipeline,
            repo=payload.repo,
            branch=payload.branch,
            commit_sha=payload.commit_sha,
            test_logs=payload.test_logs,
        )
        return {"received": True, "action": "agent pipeline started"}

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
