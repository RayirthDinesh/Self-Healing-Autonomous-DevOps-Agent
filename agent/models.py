"""Pydantic models for the webhook server.

Declaring the request body as a model buys automatic validation: a payload
from GitHub Actions that is missing a field, or carries the wrong type, is
rejected with a 422 before the handler runs.
"""

from pydantic import BaseModel


class WebhookPayload(BaseModel):
    """The JSON body GitHub Actions POSTs to /webhook after a CI run."""

    repo: str             # full repo name, e.g. "username/sre-agent-demo"
    branch: str           # branch that was pushed, e.g. "bug/logic-error"
    commit_sha: str       # commit SHA that triggered the run
    workflow_run_id: str  # GitHub Actions run id (string, ids can be large)
    test_logs: str        # full captured pytest/install output
    status: str           # "failure" or "success"
