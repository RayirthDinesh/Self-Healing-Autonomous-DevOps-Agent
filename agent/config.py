"""Deployment settings, read from the environment.

Every setting has a default matching the reference deployment, so a fresh
clone runs with nothing configured. The point of the module is that adapting
the agent to a different target repo (a non-`main` default branch, a suite
that needs a fatter image) is an environment change, not a source edit.

Settings are read through functions rather than module constants so that a
value is picked up whenever it is used. `load_dotenv()` runs in `main.py` and
the console starts on its own, so there is no single import moment at which
the environment is guaranteed to be populated.
"""

import logging
import os

logger = logging.getLogger("sre-agent-webhook")

DEFAULT_VALIDATOR_IMAGE = "python:3.11-slim"
DEFAULT_VALIDATOR_TIMEOUT = 300
DEFAULT_PR_BASE_BRANCH = "main"

# Free by default, so a fresh clone runs the whole pipeline on a new OpenRouter
# account with no credit on it. Free slugs are slower and less consistent at
# emitting valid JSON than a paid model, which costs the occasional retried
# attempt: a fair trade for "it works without a card".
#
# OpenRouter retires free slugs without notice, and a dead one 404s every node.
# Verified live 2026-08-13. If it stops working, list current ids with:
#     curl -s https://openrouter.ai/api/v1/models | grep -o '"id":"[^"]*:free"'
DEFAULT_LLM_MODEL = "poolside/laguna-s-2.1:free"


def _env(name: str, default: str) -> str:
    """Read a setting, treating whitespace-only as unset.

    These values arrive from a .env file or a CI secret, where a trailing
    space or an empty assignment is common. Without the strip, `KEY=` would
    override a working default with the empty string.
    """
    return (os.getenv(name) or "").strip() or default


def validator_image() -> str:
    """Container image the candidate fix is tested in.

    Must have Python and pip on PATH. Override when the target suite needs
    system packages, a different Python, or a pinned architecture (the Ampere
    free tier is arm64, so an x86-only wheel needs an explicit image here).
    """
    return _env("VALIDATOR_IMAGE", DEFAULT_VALIDATOR_IMAGE)


def validator_timeout() -> int:
    """Seconds a single validation run may take before it is killed.

    Covers `pip install` plus the whole suite. A timeout counts as a failed
    attempt, never as a pass, so a too-low value costs retries rather than
    shipping an unverified fix.
    """
    raw = _env("VALIDATOR_TIMEOUT", str(DEFAULT_VALIDATOR_TIMEOUT))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("VALIDATOR_TIMEOUT=%r is not a number, using %s",
                       raw, DEFAULT_VALIDATOR_TIMEOUT)
        return DEFAULT_VALIDATOR_TIMEOUT
    if value <= 0:
        logger.warning("VALIDATOR_TIMEOUT=%s is not positive, using %s",
                       value, DEFAULT_VALIDATOR_TIMEOUT)
        return DEFAULT_VALIDATOR_TIMEOUT
    return value


def llm_model() -> str:
    """Model used by the fixer and the other heavyweight nodes."""
    return _env("LLM_MODEL", DEFAULT_LLM_MODEL)


def triage_model() -> str:
    """Cheaper model for the triage and review nodes.

    Falls back to LLM_MODEL rather than to the built-in default, so setting
    only LLM_MODEL moves every node instead of leaving these two behind on
    something the operator never chose.
    """
    return _env("TRIAGE_MODEL", llm_model())


def pr_base_branch() -> str:
    """Branch the auto-fix pull request is opened against.

    Set it to the target repo's default branch. A wrong value does not open a
    PR somewhere unexpected (GitHub rejects the request), but it does throw
    away a fix that had already gone green.
    """
    return _env("PR_BASE_BRANCH", DEFAULT_PR_BASE_BRANCH)
