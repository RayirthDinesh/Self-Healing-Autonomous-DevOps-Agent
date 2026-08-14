"""Read-only web dashboard over the agent's run log.

Clone the repo, run this, watch your own runs. No configuration:

    python dashboard.py            # http://127.0.0.1:8001/ui

Two access modes, chosen by where the server listens:

  local   bound to loopback, so only processes on this machine can reach it.
          No secret required. This is the default and the common case: every
          pipeline (webhook, SWE-bench) writes to the same SQLite file, so a
          fresh clone sees its runs immediately.

  exposed bound to a routable interface, or mounted into the webhook server in
          main.py. A secret is then mandatory, because the console serves repo
          diffs, CI logs and full LLM prompts. Set DASHBOARD_SECRET (preferred,
          it is read-only) or fall back to WEBHOOK_SECRET.

The dashboard never imports pipeline code: it only reads runs/events/artifacts.
"""

import asyncio
import hmac
import json
import logging
import os
import time

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, RedirectResponse,
                               StreamingResponse)

import run_tracker

logger = logging.getLogger("sre-agent-webhook")

router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_PAGE = os.path.join(_STATIC_DIR, "dashboard.html")

# How often the SSE endpoint re-reads the DB for new events.
_POLL_SECONDS = 0.75
# Stop streaming a finished run after this much quiet time.
_TERMINAL_GRACE = 2.0

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}

# Only the standalone entry point turns this on, and only for a loopback bind.
# Importing the router into another app (main.py) leaves it off, so mounting the
# console on the public webhook server can never silently drop authentication.
_local_mode = False


def enable_local_mode(on: bool = True):
    global _local_mode
    _local_mode = on


def local_mode() -> bool:
    return _local_mode


def _is_local_request(request: Request) -> bool:
    """True only for a browser on this machine talking to a loopback listener.

    The Host check matters: without it a DNS rebinding page could point its own
    hostname at 127.0.0.1 and read the console out of the user's browser.
    """
    if not _local_mode:
        return False
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK_CLIENTS:
        return False
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def expected_secret() -> str:
    """The secret this console accepts.

    DASHBOARD_SECRET is preferred because it grants reading only, while
    WEBHOOK_SECRET also lets the holder POST a CI failure and start a run.
    """
    return os.getenv("DASHBOARD_SECRET") or os.getenv("WEBHOOK_SECRET") or ""


def secret_ok(request: Request) -> bool:
    """Local requests pass freely. Everything else must present the secret."""
    if _is_local_request(request):
        return True
    expected = expected_secret()
    if not expected:
        return False
    provided = (request.headers.get("X-Webhook-Secret")
                or request.query_params.get("key")
                or request.cookies.get("sre_key"))
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _require(request: Request):
    if not secret_ok(request):
        raise HTTPException(status_code=401, detail="invalid dashboard secret")


# --- Page ---

@router.get("/ui")
def ui(request: Request, key: str = ""):
    """Serve the console, moving a ?key= secret into a cookie first."""
    _require(request)
    if not os.path.exists(_PAGE):
        raise HTTPException(status_code=500, detail=f"dashboard.html missing at {_PAGE}")
    if key:
        # Move the secret into a cookie and bounce to a bare /ui. Access logs,
        # browser history and Referer headers keep the query string, so it must
        # not survive past the first request.
        response = RedirectResponse("/ui", status_code=303)
        response.set_cookie(
            "sre_key", key, httponly=True, samesite="lax", max_age=86400,
            secure=request.url.scheme == "https",
        )
        return response
    return FileResponse(_PAGE, media_type="text/html")


# --- JSON API ---

@router.get("/api/runs")
def api_runs(request: Request, limit: int = Query(50, ge=1, le=500),
             source: str = "", repo: str = ""):
    _require(request)
    return {"runs": run_tracker.list_runs(limit=limit, source=source, repo=repo)}


@router.get("/api/runs/{run_id}")
def api_run(request: Request, run_id: str):
    _require(request)
    run = run_tracker.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "run": run,
        "events": run_tracker.get_events(run_id),
        "artifacts": run_tracker.get_artifact_index(run_id),
    }


@router.get("/api/artifacts/{artifact_id}")
def api_artifact(request: Request, artifact_id: int):
    _require(request)
    row = run_tracker.get_artifact(artifact_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown artifact")
    return row


@router.get("/api/runs/{run_id}/stream")
async def api_stream(request: Request, run_id: str):
    """Server-sent events: new events/artifacts for one run, then close.

    Polls SQLite rather than hooking the pipeline in-process, so it works even
    when the run is happening in another process (webhook server, harness).
    """
    _require(request)
    if not run_tracker.get_run(run_id):
        raise HTTPException(status_code=404, detail="unknown run")

    async def gen():
        last_seq, last_artifact, finished_at = 0, 0, None
        while True:
            if await request.is_disconnected():
                return
            run = run_tracker.get_run(run_id) or {}
            events = run_tracker.get_events(run_id, after_seq=last_seq)
            artifacts = run_tracker.get_artifact_index(run_id, after_id=last_artifact)
            if events:
                last_seq = events[-1]["seq"]
            if artifacts:
                last_artifact = artifacts[-1]["id"]
            payload = {"run": run, "events": events, "artifacts": artifacts}
            yield f"data: {json.dumps(payload, default=str)}\n\n"

            if run.get("status") and run["status"] != "running":
                # One extra pass, so late writes (final artifacts) still ship.
                if finished_at is None:
                    finished_at = time.time()
                elif time.time() - finished_at >= _TERMINAL_GRACE:
                    yield "event: done\ndata: {}\n\n"
                    return
            await asyncio.sleep(_POLL_SECONDS)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# --- Standalone mode ---

def build_app():
    """Wrap the router in a minimal app for the standalone entry point."""
    app = FastAPI(title="SRE Agent Dashboard")
    app.include_router(router)

    @app.get("/")
    def root():
        return RedirectResponse("/ui")

    return app


def _is_loopback_bind(host: str) -> bool:
    return host.strip("[]") in {"localhost", "127.0.0.1", "::1"}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8001"))

    if _is_loopback_bind(host):
        # Nothing off this machine can connect, so do not demand a secret.
        # This is what makes "clone the repo and run it" work with no setup.
        enable_local_mode()
        logger.info("Console (local, no secret needed): http://%s:%d/ui", host, port)
    elif expected_secret():
        logger.warning("DASHBOARD_HOST=%s is reachable off this machine; "
                       "every request must carry the secret", host)
        logger.info("Console: http://%s:%d/ui?key=<DASHBOARD_SECRET>", host, port)
    else:
        raise SystemExit(
            f"DASHBOARD_HOST={host} is reachable from outside this machine, and the "
            "console serves repo diffs, CI logs and full LLM prompts.\n"
            "Set DASHBOARD_SECRET (read-only, preferred) or WEBHOOK_SECRET first, or "
            "drop DASHBOARD_HOST to run it locally with no secret."
        )

    logger.info("Reading runs from %s", run_tracker._db_path())
    uvicorn.run(build_app(), host=host, port=port)
