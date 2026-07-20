"""Function-level chunking for RAG.

Instead of embedding whole files, we split each file into individual
function/class chunks using the symbol boundaries from repo_map. This gives
the embedding model a focused unit of meaning per chunk, so cosine similarity
to the error log is much more precise than file-level averaging.
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
        start = sym["line"] - 1                          # 0-indexed
        end = sym.get("end_line", start + 80)            # end_line is inclusive
        chunk_lines = lines[start:end]
        chunks.append({
            "file": path,
            "name": sym["name"],
            "kind": sym["kind"],
            "text": "\n".join(chunk_lines),
            "start_line": sym["line"],
            "end_line": end,
        })
    return chunks


def chunks_for_repo(source_files: dict, repo_map_files: dict) -> dict:
    """Build {path: [chunk, ...]} for every source file.

    source_files  — {path: content} from retrieval
    repo_map_files — files sub-dict from get_repo_map(), carries symbols
    """
    result = {}
    for path, content in source_files.items():
        symbols = repo_map_files.get(path, {}).get("symbols", [])
        result[path] = extract_chunks(path, content, symbols)
    return result
