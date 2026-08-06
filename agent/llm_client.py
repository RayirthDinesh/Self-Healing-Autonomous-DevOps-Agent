"""Prompt fragments shared by the LLM-calling graph nodes.

This module used to own a call_llm() and its whole-prompt builder, for the
single-shot pipeline. That pipeline is gone and graph_nodes._chat is the only
path to a model now, so what remains is the JSON contract every node answers
in and the past-incidents block memory feeds it.
"""

_JSON_CONTRACT = """Respond with raw JSON only - no markdown, no explanation outside the JSON:
{
  "diagnosis": "one sentence describing the root cause of the failure",
  "fixes": [
    {
      "filename": "relative/path/to/file",
      "search": "exact lines from the file to replace - copy verbatim, including indentation",
      "replace": "the corrected lines that replace the search block"
    }
  ]
}

Rules for fixes:
- Use "search" + "replace" for targeted edits. Copy the search lines EXACTLY as they appear
  in the file - same indentation, same whitespace. The search must match uniquely.
- Only include files that actually need to change.
- If requirements.txt is the problem, include that too."""


def _incidents_section(incidents) -> str:
    """Few-shot block built from past merged/green fixes in this repo."""
    if not incidents:
        return ""
    parts = [
        "\n## Past incidents in this repo\n"
        "Past incidents are historical hints - the current bug may differ. "
        "Verify against the code shown.\n"
    ]
    for inc in incidents:
        files = ", ".join(inc.get("files_fixed", []))
        parts.append(
            f"\n### {inc.get('error_class', 'incident')} - fixed {files}\n"
            f"Diagnosis: {inc.get('diagnosis', '')}\n"
            f"```diff\n{inc.get('fix_diff', '')}\n```\n"
        )
    return "".join(parts)
