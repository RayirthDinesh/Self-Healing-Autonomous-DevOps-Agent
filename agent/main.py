"""FastAPI webhook server for the Self-Healing SRE Agent.

GitHub Actions posts the result of every CI run on a watched branch to
/webhook. A failure starts the agent pipeline in the background: diagnose,
fix, validate, open a pull request.

    python main.py                       # serve on port 8000
    curl http://localhost:8000/health    # liveness check
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# Must run before the local imports below. llm_client reads OPENROUTER_API_KEY
# at import time, so the .env has to be in the environment by then.
load_dotenv()

import dashboard  # noqa: E402
from models import WebhookPayload  # noqa: E402
from pipeline import run as run_pipeline  # noqa: E402

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sre-agent-webhook")

app = FastAPI(title="Self-Healing SRE Agent Webhook")
app.include_router(dashboard.router)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    """Authenticate every request via the X-Webhook-Secret header.

    /health is exempt so uptime checks need no credential. Dashboard routes
    accept the same secret from a header, a ?key= parameter or the sre_key
    cookie (see dashboard.secret_ok) so a browser can reach them. Everything
    else must present the header or gets a 401.
    """
    path = request.url.path
    if path != "/health":
        if path == "/ui" or path.startswith("/api/"):
            authorised = dashboard.secret_ok(request)
        else:
            authorised = (bool(WEBHOOK_SECRET)
                          and request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET)
        if not authorised:
            logger.warning("Rejected request to %s: bad webhook secret", path)
            return JSONResponse(status_code=401,
                                content={"detail": "invalid webhook secret"})
    return await call_next(request)


@app.get("/health")
def health():
    """Liveness probe. Confirms the server process is up and serving."""
    return {"status": "ok"}


@app.post("/webhook")
def webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """Receive one CI result from GitHub Actions.

    FastAPI validates the body against WebhookPayload before this runs. The
    response goes out immediately and the pipeline runs afterwards, because a
    full run takes 60-90 seconds and GitHub would time out waiting for it.
    """
    logger.info(
        "Incoming run | branch=%s commit=%s status=%s",
        payload.branch,
        payload.commit_sha,
        payload.status,
    )

    if payload.status == "failure":
        logger.info("CI failure on %s, starting agent pipeline", payload.branch)
        background_tasks.add_task(
            run_pipeline,
            repo=payload.repo,
            branch=payload.branch,
            commit_sha=payload.commit_sha,
            test_logs=payload.test_logs,
        )
        return {"received": True, "action": "agent pipeline started"}

    logger.info("CI success on %s (%s), no action needed",
                payload.branch, payload.commit_sha)
    return {"received": True, "action": "no action needed"}


if __name__ == "__main__":
    # Bound to all interfaces so the server is reachable on the deployment VM.
    #
    # http="httptools" (from uvicorn[standard]) rather than the default h11
    # parser: h11 mishandles "Expect: 100-continue" from clients such as curl
    # over real network latency and drops the request body.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, http="httptools")
