"""Read-only web dashboard over the agent's run log.

Mounted into the webhook app in main.py, and runnable on its own:

    python dashboard.py            # http://127.0.0.1:8001/ui?key=<WEBHOOK_SECRET>

Standalone mode exists so local replay / SWE-bench runs can be watched without
starting the webhook server, since every pipeline writes to the same SQLite file.

The dashboard never imports pipeline code: it only reads runs/events/artifacts.
"""

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import run_tracker

logger = logging.getLogger("sre-agent-webhook")

router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_PAGE = os.path.join(_STATIC_DIR, "dashboard.html")

# How often the SSE endpoint re-reads the DB for new events.
_POLL_SECONDS = 0.75
# Stop streaming a finished run after this much quiet time.
_TERMINAL_GRACE = 2.0


def secret_ok(request: Request) -> bool:
    """The dashboard carries repo diffs and CI logs, so never serve it unauthenticated.

    Accepts the shared secret from the webhook header, a ?key= query param, or
    the sre_key cookie that /ui sets from ?key=.
    """
    expected = os.getenv("WEBHOOK_SECRET")
    if not expected:
        return False
    provided = (request.headers.get("X-Webhook-Secret")
                or request.query_params.get("key")
                or request.cookies.get("sre_key"))
    return provided == expected


def _require(request: Request):
    if not secret_ok(request):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


# ── Page ─────────────────────────────────────────────────────────────────────

@router.get("/ui")
def ui(request: Request, key: str = ""):
    _require(request)
    if not os.path.exists(_PAGE):
        raise HTTPException(status_code=500, detail=f"dashboard.html missing at {_PAGE}")
    response = FileResponse(_PAGE, media_type="text/html")
    if key:
        # Cookie so the page's own fetch/SSE calls authenticate without the
        # secret sitting in every URL. httponly=False is deliberate: nothing
        # here reads it from JS, but it keeps the cookie visible for debugging.
        response.set_cookie("sre_key", key, httponly=False, samesite="lax", max_age=86400)
    return response


# ── JSON API ─────────────────────────────────────────────────────────────────

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
    when the run is happening in another process (replay script, harness).
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
                # One extra pass so late writes (final artifacts) still ship
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


# ── Standalone mode ──────────────────────────────────────────────────────────

def build_app():
    from fastapi import FastAPI
    app = FastAPI(title="SRE Agent Dashboard")
    app.include_router(router)

    @app.get("/")
    def root():
        return JSONResponse({"ui": "/ui?key=<WEBHOOK_SECRET>"})

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if not os.getenv("WEBHOOK_SECRET"):
        raise SystemExit("WEBHOOK_SECRET is not set — refusing to serve run data")
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8001"))
    logger.info("Dashboard on http://%s:%d/ui?key=<WEBHOOK_SECRET>", host, port)
    uvicorn.run(build_app(), host=host, port=port)
