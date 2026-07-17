"""Tool belt for the localizer agent, bound to one cloned workdir.

Deliberately model-agnostic: the localizer speaks a JSON tool protocol instead
of a provider function-calling API, so any OpenRouter model can drive these.
Tests and agent internals stay invisible, same guardrail as the legacy path.
"""

import os
import re

_HIDDEN_PREFIXES = ("tests", "agent", ".git", ".github")
_READ_CAP_CHARS = 8000
_SEARCH_MAX_HITS = 20


def _visible(path: str, repo_map: dict) -> bool:
    entry = repo_map["files"].get(path)
    if entry is None or entry.get("is_test"):
        return False
    return not any(path.startswith(p) for p in _HIDDEN_PREFIXES)


def make_tools(workdir: str, repo_map: dict) -> dict:
    """Return {name: callable(args_dict) -> str} for the localizer loop."""

    def _source_paths():
        return [p for p in repo_map["files"] if _visible(p, repo_map)]

    def search_repo(args):
        query = str(args.get("query", "")).strip()
        if not query:
            return "error: query required"
        try:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        except re.error:
            return "error: bad query"
        hits = []
        for path in _source_paths():
            try:
                with open(os.path.join(workdir, path), encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            hits.append(f"{path}:{lineno}: {line.strip()[:160]}")
                            if len(hits) >= _SEARCH_MAX_HITS:
                                return "\n".join(hits)
            except OSError:
                continue
        return "\n".join(hits) or "no matches"

    def read_file(args):
        path = str(args.get("path", "")).replace("\\", "/").lstrip("./")
        if not _visible(path, repo_map):
            return f"error: {path} is not a readable source file"
        try:
            with open(os.path.join(workdir, path), encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            return f"error: {e}"
        return content[:_READ_CAP_CHARS]

    def get_signatures(args):
        path = str(args.get("path", "")).replace("\\", "/").lstrip("./")
        entry = repo_map["files"].get(path)
        if entry is None:
            return f"error: unknown file {path}"
        lines = [f'{s["sig"]}  # line {s["line"]}' for s in entry["symbols"]]
        return "\n".join(lines) or "(no symbols)"

    def get_importers(args):
        path = str(args.get("path", "")).replace("\\", "/").lstrip("./")
        importers = [src for src, dst in repo_map.get("edges", []) if dst == path]
        return "\n".join(importers) or "(no importers)"

    return {
        "search_repo": search_repo,
        "read_file": read_file,
        "get_signatures": get_signatures,
        "get_importers": get_importers,
    }


TOOL_DESCRIPTIONS = """Available tools:
- search_repo(query): case-insensitive literal search across source files -> "path:line: text"
- read_file(path): full content of one source file
- get_signatures(path): function/class signatures of one file
- get_importers(path): which files import the given file"""
