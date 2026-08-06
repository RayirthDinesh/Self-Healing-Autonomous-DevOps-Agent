"""Repository operations - clone, apply fixes, run tests, push."""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("sre-agent-webhook")


def clone_branch(repo: str, branch: str, dest: str):
    """Clone a single branch of a public GitHub repo into dest."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "clone", "--branch", branch, "--depth", "1", url, dest],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Cloned %s@%s into %s", repo, branch, dest)


def apply_fixes(repo_path: str, fixes: list):
    """Apply each fix to the cloned repo.

    Supports two formats from the LLM:
      search/replace - find an exact block and swap only those lines (preferred).
                       Leaves the rest of the file completely untouched.
      content        - full file overwrite (fallback for new files or total rewrites).
    """
    for fix in fixes:
        filepath = os.path.join(repo_path, fix["filename"])
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if "search" in fix and "replace" in fix:
            # Targeted edit - only touch the lines that need to change
            try:
                with open(filepath, encoding="utf-8") as f:
                    original = f.read()
                search_text = fix["search"]
                if search_text not in original:
                    logger.error(
                        "apply_fixes: search block not found in %s - skipping",
                        fix["filename"],
                    )
                    continue
                updated = original.replace(search_text, fix["replace"], 1)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(updated)
                logger.info("Applied search/replace fix to %s", fix["filename"])
            except FileNotFoundError:
                logger.error("apply_fixes: file not found: %s", fix["filename"])
        elif "content" in fix:
            # Full file overwrite (new file or total rewrite)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(fix["content"])
            logger.info("Applied full-file fix to %s", fix["filename"])
        else:
            logger.warning("apply_fixes: fix for %s has neither search/replace nor content - skipping", fix["filename"])


def run_tests(repo_path: str) -> tuple:
    logger.info("Running tests inside Docker container (python:3.11-slim)...")
    """Run pytest inside a throwaway Docker container.

    The container gets the cloned repo mounted in, installs dependencies,
    runs the suite, then is automatically deleted (--rm). Nothing the fix
    does can touch the VM or bleed into the next run.

    Returns (passed: bool, output: str).
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{repo_path}:/app",
            "-w", "/app",
            "python:3.11-slim",
            "sh", "-c",
            "pip install -r requirements.txt -q --timeout 120 --retries 5 && "
            "python -m pytest -v --tb=long; "
            "PYTEST_EXIT=$?; "
            "chmod -R 777 /app 2>/dev/null || true; "
            "exit $PYTEST_EXIT",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0

    if passed:
        logger.info("Tests PASSED after fix")
    else:
        logger.warning("Tests still FAILING after fix:\n%s", output[-3000:])

    return passed, output


def get_diff(repo_path: str) -> str:
    """Diff of the applied fix against the cloned HEAD (for incident memory)."""
    result = subprocess.run(
        # fileMode off: the Docker test run chmods the tree, which is not part of the fix
        ["git", "-c", "core.fileMode=false", "diff", "--no-color"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _git(args: list, cwd: str, secret: str = "") -> subprocess.CompletedProcess:
    """Run git, and never let the token reach an exception, a log or the console.

    The push URL carries the PAT, and CalledProcessError puts the whole command
    in its message - which then lands in the server log, in journalctl and in
    the publisher event the dashboard serves.
    """
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = _redact((result.stderr or result.stdout or "").strip(), secret)
        raise RuntimeError(f"git {args[1]} failed: {detail[-500:]}")
    return result


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _unique_branch(auth_url: str, fix_branch: str, repo_path: str, token: str) -> str:
    """Pick a branch name that does not exist on the remote yet.

    The name is derived from the failing branch and its commit, so re-running
    the same failure - a retried CI job, a second push of the same commit -
    produces the same name and git rejects it as a non-fast-forward. A green
    fix would then never reach a PR. Suffix instead of force-pushing: the
    existing branch may have an open PR a human is already reviewing.
    """
    try:
        listing = _git(["git", "ls-remote", "--heads", auth_url,
                        f"refs/heads/{fix_branch}*"], repo_path, token).stdout
    except RuntimeError as e:
        logger.warning("Could not list remote branches (%s) - using %s as is", e, fix_branch)
        return fix_branch
    taken = {line.rsplit("refs/heads/", 1)[-1] for line in listing.splitlines() if line.strip()}
    if fix_branch not in taken:
        return fix_branch
    for n in range(2, 100):
        candidate = f"{fix_branch}-{n}"
        if candidate not in taken:
            logger.info("%s already on the remote - pushing %s instead", fix_branch, candidate)
            return candidate
    raise RuntimeError(f"{fix_branch} and 98 suffixed variants all exist on the remote")


def commit_and_push(repo_path: str, fix_branch: str, github_token: str, repo: str) -> str:
    """Create a branch in the clone, commit the fix, push it. Returns the branch
    actually pushed, which may carry a suffix if the first choice was taken."""
    _git(["git", "config", "user.email", "sre-agent@auto.fix"], repo_path)
    _git(["git", "config", "user.name", "SRE Agent"], repo_path)
    _git(["git", "config", "core.fileMode", "false"], repo_path)

    # Embed the token in the remote URL so git can authenticate without a prompt
    auth_url = f"https://x-access-token:{github_token}@github.com/{repo}.git"
    fix_branch = _unique_branch(auth_url, fix_branch, repo_path, github_token)

    _git(["git", "checkout", "-b", fix_branch], repo_path)
    _git(["git", "add", "-A"], repo_path)
    _git(["git", "commit", "-m", f"fix: auto-fix applied by SRE Agent on {fix_branch}"],
         repo_path)
    _git(["git", "push", auth_url, fix_branch], repo_path, github_token)
    logger.info("Pushed fix branch %s to GitHub", fix_branch)
    return fix_branch
