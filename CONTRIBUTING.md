# Contributing

Issues and pull requests are welcome. This is a small project - if you are
planning something large, open an issue first so we can agree on the shape
before you spend the time.

## Getting set up

```bash
git clone https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent.git
cd Self-Healing-Autonomous-DevOps-Agent
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r agent/requirements-dev.txt
```

Python 3.11 is what the validator container runs and what CI treats as the
version that must stay green. 3.12 is also tested.

## Running the tests

```bash
pytest agent/tests
```

One test - `test_run_tests_isolation.py` - needs a working Docker daemon,
because it proves the candidate fix executes in a container and not in your own
environment. CI skips it for speed; run it locally before touching anything in
`repo_ops.py`.

```bash
pytest agent/tests --deselect agent/tests/test_run_tests_isolation.py   # no Docker
```

Lint matches what CI enforces - real errors only, not style:

```bash
flake8 agent --count --select=E9,F63,F7,F82 --show-source --statistics
```

## Trying a change against a real failure

The tests stub the model. To see a whole run, post the canned CI failure in
`examples/` at a locally running server:

```bash
cp agent/.env.example agent/.env    # fill in OPENROUTER_API_KEY and WEBHOOK_SECRET
cd agent && python main.py          # terminal 1
scripts/try-it.sh                   # terminal 2
python agent/dashboard.py           # terminal 3 -> http://127.0.0.1:8001/ui
```

This spends model credits. Leave `GITHUB_TOKEN` unset and the run stops after
validation instead of trying to push. `scripts/capture-payload.sh` builds the
same kind of payload from any other repo and branch.

## House style

The code is deliberately plain: flat modules, standard library first,
`subprocess` over wrappers. Match what is around you rather than introducing a
new abstraction layer.

Comments explain **why**, not what. The existing ones are load-bearing - they
record failure modes that cost real debugging time (the BOM in `.env`, the
h11 `Expect: 100-continue` bug, why the token never reaches an exception).
Keep that bar: if a line looks odd and is odd for a reason, say the reason.

## Pull requests

- One concern per PR.
- Add a test for behaviour changes. Bug fixes should include the test that
  fails without the fix.
- Update the docs in the same PR when you change setup, environment variables,
  or anything in the run flow. `agent/README.md` is the setup guide;
  `agent/.env.example` is the environment reference and must stay complete.
- Say what you actually ran. "Tests pass" means you ran them.

## Things worth knowing before you change them

- **The fixer's guardrail** (`graph_nodes.py`) drops fixes touching `tests/`,
  `agent/`, `.git/`, `.github/`. That is the property that stops a failing
  assertion being "fixed" by deleting it. Do not relax it.
- **A fix is only published if the suite goes green.** Nothing should be able
  to shortcut validation - a timeout or an absent Docker daemon must read as
  failure, never as a pass.
- **The GitHub token must never reach output.** Git commands that carry it in
  a URL go through `repo_ops._git`, which redacts before raising. Anything
  bypassing that with `check=True` puts the token in the log and the console.
- **Environment defaults live in `agent/config.py`.** New knobs go there, with
  a default that works, and get an entry in `agent/.env.example`. Adapting the
  agent to a different target repo should never require a source edit.

## Security

Do not open a public issue for a vulnerability - see [SECURITY.md](SECURITY.md).

## Licence

Contributions are accepted under the [Apache License 2.0](LICENSE).
