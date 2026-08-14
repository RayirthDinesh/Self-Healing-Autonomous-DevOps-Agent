# Security

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent/security/advisories/new)
(Security tab, "Report a vulnerability"). Please do not open a public issue for
anything exploitable.

Include what you did, what happened, and the version or commit. Expect a first
response within a week. This is a personal project, not a funded one, so there
is no bounty.

## What this thing actually does

Read this part before deploying. The agent is not a linter; it is a service
that executes model-written code and holds a credential that can write to your
repository.

**It runs untrusted code.** Every candidate fix is executed as part of the
target repo's test suite. The code was written by an LLM reasoning over a CI
log, and the suite it runs inside is whatever the target repo defines. Treat
every run as running an unreviewed pull request from a stranger.

**Isolation is one container deep.** The suite runs in a throwaway
`docker run --rm` container with the clone bind-mounted at `/app`. That
protects the host filesystem and keeps runs from bleeding into each other. It
is *not* a sandbox against a determined attacker:

- the container has full network access (needed for `pip install`)
- no CPU, memory, or PID limits are applied
- it runs as root inside the container, on the host's Docker daemon

**The server's Docker socket is root on the host.** Under `docker compose` the
agent container gets `/var/run/docker.sock`. Anyone who can execute code in
that container can start a privileged container and own the host. That is
inherent to letting a containerised agent start sibling containers.

**The console exposes source.** Run rows carry repo diffs, full CI logs and
complete model prompts. Access to `/ui` is access to the source of whatever
repo the agent watches.

## Deploying it without regret

**Scope the GitHub token.** Use a fine-grained PAT limited to the single target
repo, with Contents and Pull requests write. A classic `repo`-scope PAT reaches
every repository you can push to. The agent never needs admin, workflow, or
org scopes. Without a token it still diagnoses, fixes and validates - it just
stops before pushing.

**Do not point it at a repo that accepts untrusted pull requests.** A fork PR
can put arbitrary code in `conftest.py`, and the agent will run it. Watch
branches you control.

**Use a throwaway host.** The reference deployment is a free-tier VM that does
nothing else. Do not run this on a machine holding other credentials.

**Split the two secrets.** `WEBHOOK_SECRET` lets its holder POST a fabricated
CI failure - spending model credits and pushing a branch. `DASHBOARD_SECRET`
only reads. Set both, and hand out the second one.

**Put TLS in front, or do not expose it.** Both secrets travel as plain headers.
Over the public internet on plain HTTP they are readable in transit. Either
terminate TLS at a reverse proxy, or keep the console unexposed and reach it
through `ssh -L 8001:localhost:8001`.

**Harden the validator if the target is not fully trusted.** `VALIDATOR_IMAGE`
lets you supply an image with the tooling you want. For tighter limits, edit
the `docker run` arguments in `agent/repo_ops.py` to add `--memory`, `--cpus`,
`--pids-limit`, and a non-root `--user`. Dropping `--network` entirely breaks
`pip install`, so it is not the default.

## What is already defended

These are deliberate and covered by tests - please do report a way around any
of them:

- **The fixer cannot edit tests.** Fixes touching `tests/`, `agent/`, `.git/`,
  `.github/`, or any path containing `/test` are dropped before they are
  applied, so a failing assertion can never be "fixed" by deleting it
  (`test_fixer_guardrail_drops_protected_paths`).
- **No unverified fix is ever published.** The full suite must pass in the
  container before a branch is pushed. A timeout or a missing Docker daemon
  counts as failure, never as a pass.
- **Tokens are kept out of logs.** The push URL embeds the PAT, so git output
  is redacted before it reaches an exception, the server log, or the console
  (`repo_ops._git` / `_redact`). Authenticated clones also have the credential
  stripped from `.git/config` afterwards.
- **The console refuses to start unprotected.** Binding it to a routable
  address without a secret is a startup error, not a warning.
- **`?key=` does not linger.** A secret passed in the query string is moved
  into an `HttpOnly` cookie and redirected away, keeping it out of access logs,
  browser history, and `Referer` headers.
- **Existing branches are never force-pushed.** A repeated failure gets a
  suffixed branch name, so an open PR under human review is not overwritten.
