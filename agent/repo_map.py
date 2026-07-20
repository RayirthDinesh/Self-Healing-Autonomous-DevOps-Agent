"""Repo map — tree-sitter symbol graph over a cloned repo, cached per commit.

Built once per (repo, commit) and cached on disk, so the agent has structural
awareness of any repo it is pointed at without re-parsing on every failure.
"""

import json
import logging
import os
import re
import time

from tree_sitter import Language, Parser
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

logger = logging.getLogger("sre-agent-webhook")

_LANGUAGES = {
    ".py": ("python", Language(tree_sitter_python.language())),
    ".js": ("javascript", Language(tree_sitter_javascript.language())),
    ".jsx": ("javascript", Language(tree_sitter_javascript.language())),
    ".ts": ("typescript", Language(tree_sitter_typescript.language_typescript())),
    ".tsx": ("typescript", Language(tree_sitter_typescript.language_tsx())),
}

# Never parse these (same spirit as repo_ops._SKIP_PREFIXES, but tests ARE
# parsed here: they contribute import edges, they're just flagged).
_SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv", "agent"}

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$|_test\.[a-z]+$")


def _cache_dir():
    return os.environ.get(
        "REPOMAP_CACHE_DIR",
        os.path.join(os.path.expanduser("~"), ".sre-agent", "repomap"),
    )


def _cache_path(repo):
    return os.path.join(_cache_dir(), repo.replace("/", "__") + ".json")


def _node_text(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _py_docstring(body_node, src):
    for child in body_node.children:
        if child.type == "expression_statement" and child.children and child.children[0].type == "string":
            return _node_text(child.children[0], src).strip("\"' \n")
        if child.type not in ("comment",):
            break
    return ""


def _walk_python(tree, src):
    symbols, imports = [], []

    def visit(node, class_depth):
        for child in node.children:
            if child.type in ("import_statement", "import_from_statement"):
                mod = child.child_by_field_name("module_name")
                if mod is not None:
                    imports.append(_node_text(mod, src))
                else:
                    for name in child.children:
                        if name.type in ("dotted_name", "aliased_import"):
                            imports.append(_node_text(name, src).split(" ")[0])
            elif child.type == "function_definition":
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                symbols.append({
                    "name": _node_text(name, src),
                    "kind": "method" if class_depth else "function",
                    "sig": _node_text(child, src).split("\n")[0].strip(),
                    "doc": _py_docstring(body, src) if body else "",
                    "line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
                visit(child, class_depth)
            elif child.type == "class_definition":
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                symbols.append({
                    "name": _node_text(name, src),
                    "kind": "class",
                    "sig": _node_text(child, src).split("\n")[0].strip(),
                    "doc": _py_docstring(body, src) if body else "",
                    "line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
                visit(child, class_depth + 1)
            else:
                visit(child, class_depth)

    visit(tree.root_node, 0)
    return symbols, imports


def _walk_js(tree, src):
    symbols, imports = [], []

    def visit(node):
        for child in node.children:
            if child.type == "import_statement":
                source = child.child_by_field_name("source")
                if source is not None:
                    imports.append(_node_text(source, src).strip("\"'"))
            elif child.type in ("function_declaration", "generator_function_declaration"):
                name = child.child_by_field_name("name")
                if name is not None:
                    symbols.append({
                        "name": _node_text(name, src),
                        "kind": "function",
                        "sig": _node_text(child, src).split("\n")[0].strip(" {"),
                        "doc": "",
                        "line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })
            elif child.type == "class_declaration":
                name = child.child_by_field_name("name")
                if name is not None:
                    symbols.append({
                        "name": _node_text(name, src),
                        "kind": "class",
                        "sig": _node_text(child, src).split("\n")[0].strip(" {"),
                        "doc": "",
                        "line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })
            elif child.type == "method_definition":
                name = child.child_by_field_name("name")
                if name is not None:
                    symbols.append({
                        "name": _node_text(name, src),
                        "kind": "method",
                        "sig": _node_text(child, src).split("\n")[0].strip(" {"),
                        "doc": "",
                        "line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })
            visit(child)

    visit(tree.root_node)
    return symbols, imports


def _resolve_python_import(module, files):
    candidate = module.replace(".", "/")
    for path in (candidate + ".py", candidate + "/__init__.py"):
        if path in files:
            return path
    return None


def _resolve_js_import(spec, importer, files):
    if not spec.startswith("."):
        return None  # bare specifier = external package
    base = os.path.normpath(os.path.join(os.path.dirname(importer), spec)).replace("\\", "/")
    candidates = [base] + [base + ext for ext in (".js", ".jsx", ".ts", ".tsx")]
    candidates += [base + "/index" + ext for ext in (".js", ".ts")]
    for c in candidates:
        if c in files:
            return c
    return None


def _pagerank(nodes, edges, damping=0.85, iterations=20):
    if not nodes:
        return {}
    incoming = {n: [] for n in nodes}
    out_degree = {n: 0 for n in nodes}
    for src, dst in edges:
        if src in out_degree and dst in incoming:
            incoming[dst].append(src)
            out_degree[src] += 1
    rank = {n: 1.0 / len(nodes) for n in nodes}
    for _ in range(iterations):
        new = {}
        for n in nodes:
            share = sum(rank[s] / out_degree[s] for s in incoming[n] if out_degree[s])
            new[n] = (1 - damping) / len(nodes) + damping * share
        rank = new
    return rank


def build_map(repo, commit, clone_path):
    """Parse the clone and return the map dict (does not touch the cache)."""
    started = time.monotonic()
    files = {}
    raw_imports = {}

    for dirpath, dirnames, filenames in os.walk(clone_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, clone_path).replace("\\", "/")
            ext = os.path.splitext(fname)[1]
            if ext not in _LANGUAGES and fname != "requirements.txt":
                continue
            entry = {
                "lang": _LANGUAGES[ext][0] if ext in _LANGUAGES else "text",
                "size": os.path.getsize(full),
                "symbols": [],
                "imports": [],
                "is_test": bool(_TEST_PATH_RE.search(rel)),
            }
            if ext in _LANGUAGES:
                lang_name, language = _LANGUAGES[ext]
                with open(full, "rb") as f:
                    src = f.read()
                tree = Parser(language).parse(src)
                walker = _walk_python if lang_name == "python" else _walk_js
                entry["symbols"], raw_imports[rel] = walker(tree, src)
            files[rel] = entry

    edges = []
    for importer, modules in raw_imports.items():
        for module in modules:
            if files[importer]["lang"] == "python":
                target = _resolve_python_import(module, files)
            else:
                target = _resolve_js_import(module, importer, files)
            if target and target != importer:
                edges.append([importer, target])
                files[importer]["imports"].append(target)

    result = {
        "repo": repo,
        "commit": commit,
        "built_at": time.time(),
        "files": files,
        "edges": edges,
        "rank": _pagerank(list(files), edges),
    }
    logger.info(
        "Repo map built: %d files, %d edges in %dms",
        len(files), len(edges), (time.monotonic() - started) * 1000,
    )
    return result


def get_repo_map(repo, commit, clone_path):
    """Cached map for (repo, commit); builds and caches on miss or sha change."""
    path = _cache_path(repo)
    try:
        with open(path) as f:
            cached = json.load(f)
        if cached.get("commit") == commit:
            logger.info("Repo map cache hit for %s@%s", repo, commit[:7])
            return cached
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    result = build_map(repo, commit, clone_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f)
    return result
