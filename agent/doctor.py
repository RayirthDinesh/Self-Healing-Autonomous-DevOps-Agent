"""Preflight check: is this machine actually able to run the agent?

Every prerequisite here is one that otherwise stays invisible until a run is
already underway - and the symptom is usually misleading when it does surface.
A dead model slug looks like three bad fixes. A stopped Docker daemon looks
like a red suite. A token missing one scope looks like a fix that worked and
then quietly published nothing.

    python agent/doctor.py                       # everything
    python agent/doctor.py --offline             # no network calls
    python agent/doctor.py --repo owner/name     # also vet a target repo

Exit status is 0 when nothing failed (warnings are fine) and 1 otherwise, so
this can gate a deploy.

Deliberately dependency-free: it imports nothing from the agent's own
requirements at module level, because "the dependencies are broken" is one of
the things it has to be able to report.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

MIN_PYTHON = (3, 11)

# Import name -> the distribution people would install, where they differ.
REQUIRED_IMPORTS = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("requests", "requests"),
    ("tree_sitter", "tree-sitter"),
    ("tree_sitter_python", "tree-sitter-python"),
    ("rank_bm25", "rank-bm25"),
    ("fastembed", "fastembed"),
    ("numpy", "numpy"),
    ("flake8", "flake8"),
    ("langgraph", "langgraph"),
    ("langchain_core", "langchain-core"),
    ("langchain_openai", "langchain-openai"),
]


class Result:
    def __init__(self, name, status, detail="", hint=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.hint = hint


def _get(url, headers=None, timeout=15):
    """GET returning (status, body-text). Network errors become a status of 0."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return 0, str(e), {}


# --- Checks ---

def check_python():
    v = sys.version_info
    current = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return Result("Python", FAIL, current,
                      f"Needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+. Several pinned "
                      "dependencies (numpy among them) publish no wheels below it.")
    return Result("Python", OK, current)


def check_dependencies():
    missing = [dist for mod, dist in REQUIRED_IMPORTS
               if importlib.util.find_spec(mod) is None]
    if missing:
        return Result("Dependencies", FAIL, f"{len(missing)} missing: {', '.join(missing)}",
                      "pip install -r agent/requirements.txt")
    return Result("Dependencies", OK, f"all {len(REQUIRED_IMPORTS)} present")


def check_docker_binary():
    if not shutil.which("docker"):
        return Result("Docker CLI", FAIL, "not on PATH",
                      "The validator runs the suite in a container. Install Docker.")
    try:
        out = subprocess.run(["docker", "--version"], capture_output=True,
                             text=True, timeout=20)
        return Result("Docker CLI", OK, out.stdout.strip() or "present")
    except Exception as e:
        return Result("Docker CLI", FAIL, str(e))


def check_docker_daemon():
    if not shutil.which("docker"):
        return Result("Docker daemon", SKIP, "no CLI to ask")
    try:
        out = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                             capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return Result("Docker daemon", FAIL, "timed out", "Is Docker Desktop starting?")
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        return Result("Docker daemon", FAIL, detail[-1][:120] if detail else "unreachable",
                      "Start Docker. Without a daemon every fix reports red "
                      "despite never having been tested.")
    return Result("Docker daemon", OK, f"server {out.stdout.strip()}")


def check_validator_image(offline):
    import config
    image = config.validator_image()
    if not shutil.which("docker"):
        return Result("Validator image", SKIP, image)
    present = subprocess.run(["docker", "image", "inspect", image],
                             capture_output=True, text=True, timeout=45).returncode == 0
    if present:
        return Result("Validator image", OK, f"{image} present locally")
    if offline:
        return Result("Validator image", WARN, f"{image} not pulled yet",
                      "The first run will pull it, which needs network.")
    return Result("Validator image", WARN, f"{image} not pulled yet",
                  f"Pre-pull it so the first run is not slow: docker pull {image}")


def check_openrouter(offline):
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return Result("OpenRouter key", FAIL, "OPENROUTER_API_KEY unset",
                      "Get one at https://openrouter.ai/keys and put it in agent/.env")
    if offline:
        return Result("OpenRouter key", SKIP, "set, not verified (--offline)")
    status, body, _ = _get("https://openrouter.ai/api/v1/key",
                           {"Authorization": f"Bearer {key}"})
    if status == 401:
        return Result("OpenRouter key", FAIL, "rejected (401)",
                      "The key is malformed or revoked.")
    if status == 0:
        return Result("OpenRouter key", WARN, f"could not reach OpenRouter ({body[:60]})")
    if status != 200:
        return Result("OpenRouter key", WARN, f"unexpected status {status}")
    try:
        data = json.loads(body).get("data", {})
        limit, usage = data.get("limit"), data.get("usage")
    except Exception:
        limit = usage = None
    if limit is not None and usage is not None and usage >= limit:
        return Result("OpenRouter key", WARN, f"credit exhausted ({usage}/{limit})",
                      "Paid models will 402. Use a ':free' LLM_MODEL or top up.")
    return Result("OpenRouter key", OK, "valid")


def check_model(offline):
    import config
    model = config.llm_model()
    if offline:
        return Result("Model", SKIP, f"{model} (--offline)")
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return Result("Model", SKIP, f"{model} (no key to check with)")

    # A real completion, because "the id is in the catalogue" is not the same
    # as "this account can call it": free slugs get retired, and paid ones 402
    # with no credit.
    #
    # max_tokens is deliberately omitted so the provider reserves the model's
    # full output budget, exactly as a real run does. OpenRouter checks
    # affordability against the reservation rather than actual usage, so
    # capping it here would let a credit-starved account pass this check and
    # then 402 on the first genuine call. The prompt is one word, so the
    # tokens actually billed stay negligible.
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            resp.read()
        return Result("Model", OK, f"{model} answered")
    except urllib.error.HTTPError as e:
        # 400 is included deliberately. OpenRouter answers an unknown model id
        # with 400, not 404, and this request is otherwise minimal and known
        # good - so a 400 here means the id, which is the single most common
        # way this breaks.
        bad_model = ("LLM_MODEL is not a model this account can call. Check the "
                     "spelling, and note that OpenRouter retires ':free' slugs "
                     "without notice - list the current ones at "
                     "https://openrouter.ai/api/v1/models")
        hints = {
            400: bad_model,
            401: "OPENROUTER_API_KEY is malformed or revoked.",
            402: "No credit. Set LLM_MODEL to a ':free' slug or top up.",
            403: "This account may not use that model.",
            404: bad_model,
        }
        status = FAIL if e.code in hints else WARN
        return Result("Model", status, f"{model} -> HTTP {e.code}",
                      hints.get(e.code, "Transient, most likely - retry."))
    except Exception as e:
        return Result("Model", WARN, f"{model} unreachable ({str(e)[:60]})")


def check_webhook_secret():
    secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    raw = os.getenv("WEBHOOK_SECRET") or ""
    if not secret:
        return Result("Webhook secret", WARN, "WEBHOOK_SECRET unset",
                      "Needed before GitHub Actions can post a CI result. "
                      'python -c "import secrets; print(secrets.token_urlsafe(32))"')
    if raw != secret:
        return Result("Webhook secret", WARN, "has surrounding whitespace",
                      "A trailing newline becomes an embedded newline in the "
                      "header, and the request is rejected before it is parsed.")
    if len(secret) < 16:
        return Result("Webhook secret", WARN, f"only {len(secret)} characters",
                      "It guards an endpoint that spends model credits and can "
                      "push branches. Use 32+.")
    return Result("Webhook secret", OK, f"set ({len(secret)} chars)")


def check_github_token(offline):
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if not token:
        return Result("GitHub token", WARN, "GITHUB_TOKEN unset",
                      "Without it the agent still diagnoses, fixes and validates, "
                      "then stops before pushing. Private repos will not clone.")
    if offline:
        return Result("GitHub token", SKIP, "set, not verified (--offline)")
    status, body, headers = _get("https://api.github.com/user",
                                 {"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
    if status == 401:
        return Result("GitHub token", FAIL, "rejected (401)", "Expired or revoked.")
    if status == 0:
        return Result("GitHub token", WARN, f"could not reach GitHub ({body[:60]})")
    if status != 200:
        return Result("GitHub token", WARN, f"unexpected status {status}")
    try:
        login = json.loads(body).get("login", "?")
    except Exception:
        login = "?"
    scopes = (headers.get("X-OAuth-Scopes") or "").strip()
    if not scopes:
        # Fine-grained tokens report no scopes at all. Per-repo permissions can
        # only be seen against a specific repo, so --repo covers that.
        return Result("GitHub token", OK, f"{login} (fine-grained)")
    if "repo" not in scopes:
        return Result("GitHub token", FAIL, f"{login}, scopes: {scopes}",
                      "A classic token needs the 'repo' scope to push and open PRs.")
    return Result("GitHub token", OK, f"{login}, scopes: {scopes}")


def check_memory_db():
    import config  # noqa: F401  - ensures the flat-import path is sane
    import run_tracker
    path = run_tracker._db_path()
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        probe = os.path.join(directory, ".doctor-write-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        return Result("Run history DB", FAIL, f"{path} not writable ({e})",
                      "Set MEMORY_DB to somewhere writable.")
    exists = "exists" if os.path.exists(path) else "will be created"
    return Result("Run history DB", OK, f"{path} ({exists})")


def check_target_repo(repo, offline):
    """Vet a target repo against what the agent actually requires of it."""
    results = []
    if offline:
        return [Result(f"Target {repo}", SKIP, "--offline")]

    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, body, _ = _get(f"https://api.github.com/repos/{repo}", headers)
    if status == 404:
        results.append(Result(f"Target {repo}", FAIL, "not found",
                              "Private repos need GITHUB_TOKEN with access to them."))
        return results
    if status != 200:
        results.append(Result(f"Target {repo}", WARN, f"HTTP {status}"))
        return results

    info = json.loads(body)
    private = info.get("private", False)
    default_branch = info.get("default_branch", "")
    results.append(Result(f"Target {repo}", OK,
                          f"{'private' if private else 'public'}, "
                          f"default branch {default_branch}"))

    if private and not token:
        results.append(Result("  clone access", FAIL, "private, no token",
                              "The clone is anonymous without GITHUB_TOKEN."))

    import config
    base = config.pr_base_branch()
    if default_branch and base != default_branch:
        results.append(Result("  PR base branch", WARN,
                              f"PR_BASE_BRANCH={base}, default is {default_branch}",
                              "GitHub rejects the PR, discarding a fix that already "
                              f"went green. Set PR_BASE_BRANCH={default_branch}."))
    else:
        results.append(Result("  PR base branch", OK, base))

    # requirements.txt at the root is not a style preference: the validator
    # runs `pip install -r requirements.txt` before the suite and cannot
    # proceed without it.
    status, _, _ = _get(
        f"https://api.github.com/repos/{repo}/contents/requirements.txt", headers)
    if status == 200:
        results.append(Result("  requirements.txt", OK, "present at repo root"))
    else:
        results.append(Result("  requirements.txt", FAIL, "missing at repo root",
                              "The validator installs from it before running "
                              "the suite. No file, no run."))
    return results


# --- Runner ---

def run_checks(offline=False, repo=None):
    results = [
        check_python(),
        check_dependencies(),
        check_docker_binary(),
        check_docker_daemon(),
    ]
    # Everything past here imports config or the agent's own modules, which is
    # only safe once the dependency check has passed.
    if results[1].status == OK:
        results.append(check_validator_image(offline))
        results.append(check_openrouter(offline))
        results.append(check_model(offline))
        results.append(check_webhook_secret())
        results.append(check_github_token(offline))
        results.append(check_memory_db())
        if repo:
            results.extend(check_target_repo(repo, offline))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check whether this machine can run the SRE agent.")
    parser.add_argument("--offline", action="store_true",
                        help="skip every network call")
    parser.add_argument("--repo", metavar="owner/name",
                        help="also check a target repo against the agent's requirements")
    args = parser.parse_args(argv)

    # The server loads this itself; doctor has to see the same values or it
    # would happily pass while the real process fails.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass

    results = run_checks(offline=args.offline, repo=args.repo)

    width = max(len(r.name) for r in results)
    print()
    for r in results:
        print(f"  [{r.status:<4}] {r.name:<{width}}  {r.detail}")
        if r.hint and r.status in (FAIL, WARN):
            print(f"         {' ' * width}  -> {r.hint}")
    print()

    failed = [r for r in results if r.status == FAIL]
    warned = [r for r in results if r.status == WARN]
    if failed:
        print(f"{len(failed)} blocking problem(s). The agent will not "
              f"complete a run until they are fixed.")
        return 1
    if warned:
        print(f"Ready, with {len(warned)} warning(s) above.")
        return 0
    print("Ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
