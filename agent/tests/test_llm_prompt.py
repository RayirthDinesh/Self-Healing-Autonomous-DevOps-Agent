"""Tests for the tiered prompt construction in llm_client."""

from llm_client import _build_prompt
from retrieval import TieredContext


def _ctx():
    return TieredContext(
        full={"src/aggregator.py": "def max_value(tx):\n    return sorted(tx)[0]\n"},
        signatures={"src/reporter.py": "def report(tx):  # Format a report"},
        overview={"src/ingestion.py": "load, parse_transactions"},
        metrics={},
    )


def test_tiered_prompt_sections():
    prompt = _build_prompt("E   assert 100.0 == 500.0", _ctx())
    assert "## Repo Map" in prompt
    assert "## Relevant Files" in prompt
    assert "## Related Signatures" in prompt
    assert "sorted(tx)[0]" in prompt                      # full body present
    assert "def report(tx):" in prompt                    # signature present
    assert "src/ingestion.py" in prompt                   # overview line present
    assert '"diagnosis"' in prompt                        # JSON contract intact


def test_legacy_dict_context_still_works():
    prompt = _build_prompt("boom", {"src/a.py": "x = 1\n"})
    assert "x = 1" in prompt
    assert '"diagnosis"' in prompt


def test_no_incidents_no_section():
    assert "Past incidents" not in _build_prompt("boom", _ctx())
    assert "Past incidents" not in _build_prompt("boom", _ctx(), incidents=[])


def test_incidents_block_with_guard_sentence():
    incidents = [{
        "error_class": "name-error",
        "diagnosis": "typo in variable name",
        "files_fixed": ["src/aggregator.py"],
        "fix_diff": "-    return totl\n+    return total",
    }]
    prompt = _build_prompt("E  NameError: name 'totl'", _ctx(), incidents=incidents)
    assert "## Past incidents in this repo" in prompt
    assert "historical hints" in prompt                    # guard sentence present
    assert "typo in variable name" in prompt
    assert "+    return total" in prompt
    # incidents come before the repo map so the hint frames the code, not vice versa
    assert prompt.index("Past incidents") < prompt.index("## Repo Map")
