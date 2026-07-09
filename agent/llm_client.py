"""LLM client — sends broken test output + source code to OpenRouter and gets a fix back."""

import json
import logging
import os

import requests

logger = logging.getLogger("sre-agent-webhook")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("LLM_MODEL", "tencent/hy3-preview")


_JSON_CONTRACT = """Respond with raw JSON only — no markdown, no explanation outside the JSON:
{
  "diagnosis": "one sentence describing the root cause of the failure",
  "fixes": [
    {
      "filename": "relative/path/to/file",
      "content": "the full corrected file content"
    }
  ]
}

Only include files that need to change. If requirements.txt is the problem, include that too."""


def _build_prompt(test_logs: str, context) -> str:
    """Build the prompt from a TieredContext, or a legacy {path: content} dict
    (used by the full-repo escalation fallback)."""
    header = (
        "You are an expert developer working on a self-healing CI pipeline.\n\n"
        "The following tests just failed. Your job is to find the bug in the "
        "source files and fix it.\n\n"
        f"## Failed Test Output\n```\n{test_logs[-6000:]}\n```\n"
    )

    if isinstance(context, dict):
        files_section = ""
        for filename, content in context.items():
            files_section += f"\n### {filename}\n```\n{content}\n```\n"
        return f"{header}\n## Source Files\n{files_section}\n{_JSON_CONTRACT}"

    overview = "\n".join(f"- {path}: {line}" for path, line in context.overview.items())
    full_section = "".join(
        f"\n### {path}\n```\n{content}\n```\n" for path, content in context.full.items()
    )
    sig_section = "".join(
        f"\n### {path} (signatures only)\n```\n{sigs}\n```\n"
        for path, sigs in context.signatures.items()
    )
    return (
        f"{header}\n"
        f"## Repo Map\nOther files in this repo (bodies not shown):\n{overview}\n\n"
        f"## Relevant Files\nFull content of the files most likely involved:\n{full_section}\n"
        f"## Related Signatures\nNeighboring files, signatures only:\n{sig_section}\n"
        "Fix using the Relevant Files. Only rewrite a signatures-only or map-only file "
        "if its full content is unambiguous from what you see; otherwise name the file "
        "you would need in the diagnosis.\n\n"
        f"{_JSON_CONTRACT}"
    )


def call_llm(test_logs: str, context) -> dict:
    """Call the LLM and return a parsed dict with 'diagnosis' and 'fixes'."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")

    prompt = _build_prompt(test_logs, context)

    logger.info("Calling LLM (%s) to diagnose failure...", MODEL)

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=90,
    )
    response.raise_for_status()

    raw = response.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if the model wrapped the JSON anyway
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw)
    logger.info("LLM diagnosis: %s", result.get("diagnosis"))
    return result
