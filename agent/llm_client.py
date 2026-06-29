"""LLM client — sends broken test output + source code to OpenRouter and gets a fix back."""

import json
import logging
import os

import requests

logger = logging.getLogger("sre-agent-webhook")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("LLM_MODEL", "anthropic/claude-3.5-sonnet")


def _build_prompt(test_logs: str, source_files: dict) -> str:
    files_section = ""
    for filename, content in source_files.items():
        files_section += f"\n### {filename}\n```\n{content}\n```\n"

    return f"""You are an expert Python developer working on a self-healing CI pipeline.

The following tests just failed. Your job is to find the bug in the source files and fix it.

## Failed Test Output
```
{test_logs[-6000:]}
```

## Source Files
{files_section}

Respond with raw JSON only — no markdown, no explanation outside the JSON:
{{
  "diagnosis": "one sentence describing the root cause of the failure",
  "fixes": [
    {{
      "filename": "relative/path/to/file",
      "content": "the full corrected file content"
    }}
  ]
}}

Only include files that need to change. If requirements.txt is the problem, include that too."""


def call_llm(test_logs: str, source_files: dict) -> dict:
    """Call the LLM and return a parsed dict with 'diagnosis' and 'fixes'."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")

    prompt = _build_prompt(test_logs, source_files)

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
