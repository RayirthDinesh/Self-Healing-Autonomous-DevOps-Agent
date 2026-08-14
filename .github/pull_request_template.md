<!-- One concern per PR. See CONTRIBUTING.md. -->

## What this changes

<!-- And why. Link the issue if there is one. -->

## How it was verified

<!-- Say what you actually ran, and what it printed. -->

- [ ] `pytest agent/tests` passes
- [ ] `flake8 agent --select=E9,F63,F7,F82` is clean
- [ ] Ran a real end-to-end run (`scripts/try-it.sh`) — required if this touches
      the pipeline, retrieval, validation, or publishing

## Checklist

- [ ] New behaviour has a test; bug fixes include the test that fails without
      the fix
- [ ] New environment settings are in `agent/config.py` with a working default,
      and documented in `agent/.env.example` and `agent/README.md`
- [ ] Docs updated in this PR if setup or the run flow changed
- [ ] No token or secret can reach a log, an exception, or the console
- [ ] The fixer still cannot edit `tests/`, and an unvalidated fix still cannot
      be published
