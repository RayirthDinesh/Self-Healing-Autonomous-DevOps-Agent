"""Tests for the shared prompt fragments the graph nodes build on."""

from llm_client import _incidents_section


def _incident():
    return {
        "error_class": "name-error",
        "diagnosis": "typo in variable name",
        "files_fixed": ["src/aggregator.py"],
        "fix_diff": "-    return totl\n+    return total",
    }


def test_no_incidents_no_section():
    assert _incidents_section(None) == ""
    assert _incidents_section([]) == ""


def test_incidents_block_with_guard_sentence():
    section = _incidents_section([_incident()])
    assert "## Past incidents in this repo" in section
    assert "historical hints" in section          # guard sentence present
    assert "typo in variable name" in section
    assert "src/aggregator.py" in section         # which files that fix touched
    assert "+    return total" in section
