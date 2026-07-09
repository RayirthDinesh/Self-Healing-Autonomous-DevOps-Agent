"""Tests for repo_map — tree-sitter symbol extraction, import graph, cache."""

import json
import textwrap

import pytest

from repo_map import build_map, get_repo_map


@pytest.fixture
def mini_repo(tmp_path):
    """Tiny mixed-language repo: python package + a JS file, with imports."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "core.py").write_text(textwrap.dedent('''
        """Core math helpers."""


        def add(a, b):
            """Return a plus b."""
            return a + b


        class Calculator:
            """Stateful calculator."""

            def total(self):
                return self.value
    '''))
    (tmp_path / "src" / "app.py").write_text(textwrap.dedent('''
        from src.core import add


        def run():
            return add(1, 2)
    '''))
    (tmp_path / "web.js").write_text(textwrap.dedent('''
        import { thing } from "./util.js";

        function render(data) {
            return thing(data);
        }
    '''))
    (tmp_path / "util.js").write_text("export function thing(x) { return x; }\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text(textwrap.dedent('''
        from src.core import add


        def test_add():
            assert add(1, 1) == 2
    '''))
    (tmp_path / "requirements.txt").write_text("pytest\n")
    return tmp_path


def test_build_extracts_python_symbols(mini_repo):
    m = build_map("o/r", "sha1", str(mini_repo))
    syms = {s["name"]: s for s in m["files"]["src/core.py"]["symbols"]}
    assert syms["add"]["kind"] == "function"
    assert "Return a plus b." in syms["add"]["doc"]
    assert syms["Calculator"]["kind"] == "class"
    assert syms["total"]["kind"] == "method"


def test_build_extracts_js_symbols(mini_repo):
    m = build_map("o/r", "sha1", str(mini_repo))
    names = [s["name"] for s in m["files"]["web.js"]["symbols"]]
    assert "render" in names


def test_import_edges_resolved_to_files(mini_repo):
    m = build_map("o/r", "sha1", str(mini_repo))
    edges = {tuple(e) for e in m["edges"]}
    assert ("src/app.py", "src/core.py") in edges
    assert ("web.js", "util.js") in edges
    assert ("tests/test_core.py", "src/core.py") in edges


def test_test_files_parsed_but_flagged(mini_repo):
    m = build_map("o/r", "sha1", str(mini_repo))
    assert m["files"]["tests/test_core.py"]["is_test"] is True
    assert m["files"]["src/core.py"]["is_test"] is False


def test_pagerank_favors_imported_file(mini_repo):
    m = build_map("o/r", "sha1", str(mini_repo))
    # core.py is imported by app.py and the test — most central file
    assert m["rank"]["src/core.py"] == max(m["rank"].values())


def test_cache_hit_and_sha_refresh(mini_repo, tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("REPOMAP_CACHE_DIR", str(cache_dir))

    m1 = get_repo_map("o/r", "sha1", str(mini_repo))
    cached = list(cache_dir.glob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text())["commit"] == "sha1"

    # Same sha -> served from cache even if the clone changed on disk
    (mini_repo / "src" / "core.py").write_text("def gone(): pass\n")
    m2 = get_repo_map("o/r", "sha1", str(mini_repo))
    assert m2 == m1

    # New sha -> rebuild reflects the new content
    m3 = get_repo_map("o/r", "sha2", str(mini_repo))
    names = [s["name"] for s in m3["files"]["src/core.py"]["symbols"]]
    assert names == ["gone"]
