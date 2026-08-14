"""Tests for retrieval - log parsing, graph walk, BM25 ranking, tier assembly."""

import textwrap

import pytest

from repo_map import build_map
from retrieval import parse_failure_log, select_context


PYTEST_LOG = textwrap.dedent('''
    tests/test_aggregator.py::test_max_value_correct FAILED           [ 21%]

    ================================== FAILURES ===================================
    ____________________________ test_max_value_correct ___________________________

        def test_max_value_correct():
    >       assert max_value(SAMPLE) == 500.0
    E       assert 100.0 == 500.0

    tests\\test_aggregator.py:36:
    _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

        def max_value(transactions):
    >       return sorted(transactions)[0]

    src\\aggregator.py:24: AssertionError
    =========================== short test summary info ===========================
    FAILED tests/test_aggregator.py::test_max_value_correct - assert 100.0 == 500.0
''')

PIP_LOG = textwrap.dedent('''
    Collecting pandas==0.24.0 (from -r requirements.txt (line 1))
      ERROR: Could not find a version that satisfies the requirement pandas==0.24.0
    ERROR: No matching distribution found for pandas==0.24.0
''')

IMPORT_LOG = textwrap.dedent('''
    ==================================== ERRORS ====================================
    _____________________ ERROR collecting tests/test_app.py ______________________
    ImportError while importing test module 'tests/test_app.py'.
    tests/test_app.py:1: in <module>
        from src.core import add
    E   ImportError: cannot import name 'add' from 'src.core' (src/core.py)
''')


def test_parse_pytest_traceback_paths():
    hits = parse_failure_log(PYTEST_LOG)
    assert "src/aggregator.py" in hits.files
    assert "tests/test_aggregator.py" in hits.files
    assert hits.install_failure is False


def test_parse_pip_failure_flags_requirements():
    hits = parse_failure_log(PIP_LOG)
    assert hits.install_failure is True


def test_parse_import_error():
    hits = parse_failure_log(IMPORT_LOG)
    assert "src/core.py" in hits.files


@pytest.fixture
def demo_like_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "aggregator.py").write_text(textwrap.dedent('''
        """Aggregation functions."""


        def max_value(transactions):
            """Return the largest transaction amount."""
            return sorted(transactions)[0]
    '''))
    (tmp_path / "src" / "reporter.py").write_text(textwrap.dedent('''
        """Report formatting."""
        from src.aggregator import max_value


        def report(tx):
            return str(max_value(tx))
    '''))
    (tmp_path / "src" / "ingestion.py").write_text(textwrap.dedent('''
        """CSV loading, unrelated to aggregation."""


        def load(path):
            return [1.0]
    '''))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_aggregator.py").write_text(
        "from src.aggregator import max_value\n\n"
        "def test_max():\n    assert max_value([1, 5]) == 5\n"
    )
    (tmp_path / "requirements.txt").write_text("pytest\n")
    return tmp_path


def test_buggy_file_lands_in_full_tier(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    assert "src/aggregator.py" in ctx.full
    # full tier carries real file content
    assert "sorted(transactions)[0]" in ctx.full["src/aggregator.py"]


def test_only_the_failing_test_file_is_shown(demo_like_repo):
    """The fixer sees the assertions it has to satisfy, and nothing more.

    Showing the failing test is safe because the fixer may not write to it -
    the guardrail in graph_nodes drops any fix touching tests/ (covered by
    test_fixer_guardrail_drops_protected_paths). What must not happen is the
    rest of the suite leaking in as general context.
    """
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))

    shown = [p for p in ctx.full if m["files"][p]["is_test"]]
    assert shown == ["tests/test_aggregator.py"], shown

    # The ranked tiers are built from source files only - a test file has no
    # business being suggested as a place to look or edit.
    assert not any(m["files"][p]["is_test"] for p in ctx.signatures)
    assert not any(m["files"][p]["is_test"] for p in ctx.overview)


def test_unimplicated_test_files_stay_out(demo_like_repo):
    """A test file the failure never mentions is not context."""
    (demo_like_repo / "tests" / "test_ingestion.py").write_text(
        "from src.ingestion import load\n\ndef test_load():\n    assert load('x')\n"
    )
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))

    everything = set(ctx.full) | set(ctx.signatures) | set(ctx.overview)
    assert "tests/test_ingestion.py" not in everything


def test_graph_neighbor_gets_signature_tier(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    # reporter.py imports aggregator.py -> 1-hop dependent -> signatures
    assert "src/reporter.py" in ctx.signatures


def test_overview_covers_remaining_files(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    everything = set(ctx.full) | set(ctx.signatures) | set(ctx.overview)
    assert "src/ingestion.py" in everything


def test_install_failure_puts_requirements_full(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PIP_LOG, m, str(demo_like_repo))
    assert "requirements.txt" in ctx.full


def test_full_tier_cap_respected(demo_like_repo, monkeypatch):
    """CONTEXT_FULL_MAX caps the source files sent at full content.

    Failing test files ride a separate, smaller budget, so they are excluded
    from this count - see test_failing_test_files_are_capped.
    """
    monkeypatch.setenv("CONTEXT_FULL_MAX", "1")
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG + IMPORT_LOG, m, str(demo_like_repo))
    source_full = [p for p in ctx.full if not m["files"][p]["is_test"]]
    assert len(source_full) <= 1, source_full


def test_failing_test_files_are_capped(demo_like_repo):
    """A failure naming many test files must not blow the context budget."""
    log = PYTEST_LOG
    for n in range(4):
        name = f"test_extra{n}.py"
        (demo_like_repo / "tests" / name).write_text(
            "from src.aggregator import max_value\n\n"
            f"def test_e{n}():\n    assert max_value([1]) == 1\n"
        )
        log += f"\nFAILED tests/{name}::test_e{n} - assert 0 == 1\n"

    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(log, m, str(demo_like_repo))

    shown = [p for p in ctx.full if m["files"][p]["is_test"]]
    assert len(shown) <= 2, shown


def test_bm25_ranks_named_file_first(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    # error text says max_value -> aggregator must be the top full file
    assert next(iter(ctx.full)) == "src/aggregator.py"


def test_blame_prior_boosts_ranking(demo_like_repo, monkeypatch):
    # log with no path hits and no token overlap: BM25 and seeds contribute
    # nothing, so ranking is decided by the blame prior alone
    monkeypatch.setattr("retrieval._EMBEDDINGS_AVAILABLE", False)
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context("boom kaput", m, str(demo_like_repo),
                         blame={"src/ingestion.py": 1.0})
    assert next(iter(ctx.signatures)) == "src/ingestion.py"


def test_blame_none_keeps_current_behavior(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    a = select_context(PYTEST_LOG, m, str(demo_like_repo))
    b = select_context(PYTEST_LOG, m, str(demo_like_repo), blame=None)
    assert list(a.full) == list(b.full)
    assert list(a.signatures) == list(b.signatures)


def test_metrics_reported(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    assert ctx.metrics["files_total"] >= 4
    assert 0 < ctx.metrics["tokens_sent"] < ctx.metrics["tokens_full_repo"]
    assert ctx.metrics["retrieval_ms"] >= 0
