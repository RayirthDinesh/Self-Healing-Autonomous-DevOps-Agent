"""Tests for retrieval — log parsing, graph walk, BM25 ranking, tier assembly."""

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
        "from src.aggregator import max_value\n\ndef test_max():\n    assert max_value([1, 5]) == 5\n"
    )
    (tmp_path / "requirements.txt").write_text("pytest\n")
    return tmp_path


def test_buggy_file_lands_in_full_tier(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    assert "src/aggregator.py" in ctx.full
    # full tier carries real file content
    assert "sorted(transactions)[0]" in ctx.full["src/aggregator.py"]


def test_test_files_never_in_context(demo_like_repo):
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG, m, str(demo_like_repo))
    assert not any(p.startswith("tests") for p in ctx.full)
    assert not any(p.startswith("tests") for p in ctx.signatures)


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
    monkeypatch.setenv("CONTEXT_FULL_MAX", "1")
    m = build_map("o/r", "s", str(demo_like_repo))
    ctx = select_context(PYTEST_LOG + IMPORT_LOG, m, str(demo_like_repo))
    assert len(ctx.full) <= 1


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
