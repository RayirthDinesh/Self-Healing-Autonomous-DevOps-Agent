"""Function-level chunking for retrieval.

Files are split into function and class chunks along the symbol boundaries
recorded in the repo map, rather than embedded whole. Each chunk is then a
focused unit of meaning, which makes similarity against an error log far more
precise than a file-level average.
"""


def extract_chunks(path: str, content: str, symbols: list) -> list:
    """Return one chunk dict per function/class in the file.

    Each chunk contains just the lines of that symbol so the embedding
    captures its specific logic, not the noise of the surrounding file.
    Falls back to a single whole-file chunk when there are no symbols
    (e.g. requirements.txt, __init__.py).
    """
    if not symbols:
        return [{"file": path, "name": "<module>", "kind": "module",
                 "text": content, "start_line": 1, "end_line": content.count("\n") + 1}]

    lines = content.splitlines()
    chunks = []
    for sym in sorted(symbols, key=lambda s: s["line"]):
        start = sym["line"] - 1                    # symbol lines are 1-indexed
        end = sym.get("end_line", start + 80)      # inclusive
        chunks.append({
            "file": path,
            "name": sym["name"],
            "kind": sym["kind"],
            "text": "\n".join(lines[start:end]),
            "start_line": sym["line"],
            "end_line": end,
        })
    return chunks


def chunks_for_repo(source_files: dict, repo_map_files: dict) -> dict:
    """Build {path: [chunk, ...]} for every source file.

    source_files is {path: content} as loaded by retrieval. repo_map_files is
    the files sub-dict of get_repo_map, which carries the symbol boundaries.
    """
    result = {}
    for path, content in source_files.items():
        symbols = repo_map_files.get(path, {}).get("symbols", [])
        result[path] = extract_chunks(path, content, symbols)
    return result
