"""Repository operations: clone, apply fixes, run tests, push."""

import logging
import os
import subprocess

import config

logger = logging.getLogger("sre-agent-webhook")


def clone_branch(repo: str, branch: str, dest: str):
    """Clone a single branch into dest.

    With GITHUB_TOKEN set the clone authenticates, so private repos work too.
    That puts the token in the URL, which is why this goes through _git rather
    than raising CalledProcessError: that exception carries the whole command,
    and from there the token reaches the server log and the console.
    """
    token = os.getenv("GITHUB_TOKEN", "").strip()
    url = (f"https://x-access-token:{token}@github.com/{repo}.git" if token
           else f"https://github.com/{repo}.git")
    _git(["git", "clone", "--branch", branch, "--depth", "1", url, dest],
         cwd=None, secret=token)
    if token:
        # git records the clone URL in .git/config. The repo map walks this
        # tree and the diff is rendered in the console, so drop the credential
        # rather than leave it sitting in the working copy.
        _git(["git", "remote", "set-url", "origin",
              f"https://github.com/{repo}.git"], cwd=dest, secret=token)
    logger.info("Cloned %s@%s into %s", repo, branch, dest)


def apply_fixes(repo_path: str, fixes: list):
    """Apply each fix to the cloned repo.

    Two formats are accepted from the model:

    search/replace  Find an exact block and swap only those lines, leaving the
                    rest of the file untouched. Preferred.
    content         Full file overwrite. Fallback for new files and rewrites.
    """
    for fix in fixes:
        filepath = os.path.join(repo_path, fix["filename"])
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if "search" in fix and "replace" in fix:
            try:
                with open(filepath, encoding="utf-8") as f:
                    original = f.read()
                search_text = fix["search"]
                if search_text not in original:
                    logger.error("apply_fixes: search block not found in %s, skipping",
                                 fix["filename"])
                    continue
                updated = original.replace(search_text, fix["replace"], 1)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(updated)
                logger.info("Applied search/replace fix to %s", fix["filename"])
            except FileNotFoundError:
                logger.error("apply_fixes: file not found: %s", fix["filename"])
        elif "content" in fix:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(fix["content"])
            logger.info("Applied full-file fix to %s", fix["filename"])
        else:
            logger.warning("apply_fixes: fix for %s carries neither search/replace "
                           "nor content, skipping", fix["filename"])


class ValidatorUnavailable(RuntimeError):
    """The validator could not run at all, so nothing was actually tested.

    Distinct from "the suite failed". A missing Docker daemon fails the same
    way on every attempt, and reporting that as a red suite is worse than
    useless: the log then says "Tests still FAILING after fix", which reads as
    a bad patch when in fact no test ever executed.
    """


# Substrings that mean the daemon is unreachable rather than the suite red.
# Linux and Windows word this differently, and the npipe error does not
# mention the daemon at all.
_DAEMON_DOWN = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "failed to connect to the docker api",
    "error during connect",
    "the system cannot find the file specified",
    "docker daemon is not running",
)


def _daemon_unreachable(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _DAEMON_DOWN)


def reset_to_head(repo_path: str):
    """Restore the clone to pristine HEAD, discarding an applied fix.

    Deleting the tree and re-cloning looks simpler but is not reliable. rmtree
    cannot always remove a .git directory (read-only pack files on Windows,
    plus the validator container chmods the whole tree), and a partial delete
    leaves the directory non-empty, at which point `git clone` refuses with
    "destination path already exists and is not an empty directory". Every
    remaining attempt then fails on "file not found" rather than on the merits
    of its fix.

    core.fileMode is disabled because the container's `chmod -R 777` is not
    part of the fix.
    """
    _git(["git", "-c", "core.fileMode=false", "reset", "--hard", "HEAD"], repo_path)
    _git(["git", "-c", "core.fileMode=false", "clean", "-fdx"], repo_path)
    logger.info("Workdir reset to pristine HEAD")


def run_tests(repo_path: str) -> tuple:
    """Run pytest inside a throwaway Docker container.

    The container gets the cloned repo mounted in, installs dependencies, runs
    the suite, then is deleted (--rm). Nothing the fix does can touch the host
    or bleed into the next run.

    The image comes from VALIDATOR_IMAGE, so a suite needing system packages
    or a different Python is a config change rather than a fork.

    Returns (passed, output).
    """
    image = config.validator_image()
    timeout = config.validator_timeout()
    logger.info("Running tests inside Docker container (%s)...", image)
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{repo_path}:/app",
                "-w", "/app",
                image,
                "sh", "-c",
                "pip install -r requirements.txt -q --timeout 120 --retries 5 && "
                "python -m pytest -v --tb=long; "
                "PYTEST_EXIT=$?; "
                "chmod -R 777 /app 2>/dev/null || true; "
                "exit $PYTEST_EXIT",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A timeout counts as a failed attempt, never as a pass: the retry
        # loop gets another go and an unvalidated fix is never published.
        logger.warning("Validation timed out after %ss", timeout)
        return False, (f"Validation timed out after {timeout}s. Raise "
                       f"VALIDATOR_TIMEOUT if the suite legitimately runs longer.")
    except FileNotFoundError as e:
        # Docker missing is the most common setup failure of all.
        raise ValidatorUnavailable(
            "docker was not found on PATH. The validator runs the suite in a "
            "container; install Docker and start the daemon."
        ) from e
    output = result.stdout + result.stderr
    passed = result.returncode == 0

    if not passed and _daemon_unreachable(output):
        # The CLI ran but never reached a daemon, so no test executed. Retrying
        # cannot help, and calling it a red suite would blame the fix.
        raise ValidatorUnavailable(
            "the Docker CLI could not reach a daemon, so the suite never ran. "
            f"Start Docker and retry. Daemon error: {output.strip()[-300:]}"
        )

    if passed:
        logger.info("Tests PASSED after fix")
    else:
        logger.warning("Tests still FAILING after fix:\n%s", output[-3000:])

    return passed, output


def get_diff(repo_path: str) -> str:
    """Diff of the applied fix against the cloned HEAD, for incident memory."""
    result = subprocess.run(
        # fileMode off: the Docker run chmods the tree, which is not the fix.
        ["git", "-c", "core.fileMode=false", "diff", "--no-color"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _git(args: list, cwd: str | None, secret: str = "") -> subprocess.CompletedProcess:
    """Run git without letting the token reach an exception, log or console.

    The push URL carries the PAT, and CalledProcessError puts the whole
    command in its message, which then lands in the server log, in journalctl
    and in the publisher event the dashboard serves.
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
    the same failure (a retried CI job, a second push of the same commit)
    produces the same name and git rejects it as a non-fast-forward, losing a
    fix that had already gone green. The name is suffixed rather than
    force-pushed because the existing branch may have an open PR that someone
    is already reviewing.
    """
    try:
        listing = _git(["git", "ls-remote", "--heads", auth_url,
                        f"refs/heads/{fix_branch}*"], repo_path, token).stdout
    except RuntimeError as e:
        logger.warning("Could not list remote branches (%s), using %s as is",
                       e, fix_branch)
        return fix_branch
    taken = {line.rsplit("refs/heads/", 1)[-1]
             for line in listing.splitlines() if line.strip()}
    if fix_branch not in taken:
        return fix_branch
    for n in range(2, 100):
        candidate = f"{fix_branch}-{n}"
        if candidate not in taken:
            logger.info("%s already on the remote, pushing %s instead",
                        fix_branch, candidate)
            return candidate
    raise RuntimeError(f"{fix_branch} and 98 suffixed variants all exist on the remote")


def commit_and_push(repo_path: str, fix_branch: str, github_token: str, repo: str) -> str:
    """Create a branch in the clone, commit the fix and push it.

    Returns the branch actually pushed, which may carry a numeric suffix if
    the first choice of name was already taken on the remote.
    """
    _git(["git", "config", "user.email", "sre-agent@auto.fix"], repo_path)
    _git(["git", "config", "user.name", "SRE Agent"], repo_path)
    _git(["git", "config", "core.fileMode", "false"], repo_path)

    # Token embedded in the remote URL so git authenticates without a prompt.
    auth_url = f"https://x-access-token:{github_token}@github.com/{repo}.git"
    fix_branch = _unique_branch(auth_url, fix_branch, repo_path, github_token)

    _git(["git", "checkout", "-b", fix_branch], repo_path)
    _git(["git", "add", "-A"], repo_path)
    _git(["git", "commit", "-m", f"fix: auto-fix applied by SRE Agent on {fix_branch}"],
         repo_path)
    _git(["git", "push", auth_url, fix_branch], repo_path, github_token)
    logger.info("Pushed fix branch %s to GitHub", fix_branch)
    return fix_branch
