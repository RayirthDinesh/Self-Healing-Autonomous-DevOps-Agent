#!/usr/bin/env bash
#
# Build a webhook payload from a real failing branch, without waiting for CI.
#
#   scripts/capture-payload.sh <owner/repo> <branch> [output.json]
#
# Example:
#   scripts/capture-payload.sh RayirthDinesh/sre-demo-app bug/logic-error
#   scripts/try-it.sh                    # after copying it over examples/
#
# The suite runs in the same container image the agent validates in, so the
# logs match what CI would have produced, and no local Python is needed.

set -euo pipefail

REPO="${1:-}"
BRANCH="${2:-}"
OUT="${3:-payload.json}"
IMAGE="${VALIDATOR_IMAGE:-python:3.11-slim}"

if [ -z "$REPO" ] || [ -z "$BRANCH" ]; then
  echo "usage: $0 <owner/repo> <branch> [output.json]" >&2
  exit 1
fi

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

# Find a real interpreter. The candidates are probed by running them, not just
# located: on Windows `python3` in PATH is usually the Microsoft Store alias
# stub, which exists, exits without doing anything, and would leave a
# zero-byte payload behind.
PY=""
for candidate in "${PYTHON:-}" python3 python py; do
  [ -n "$candidate" ] || continue
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c "import sys" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "no working python found (tried \$PYTHON, python3, python, py)" >&2
  echo "Point PYTHON at one, e.g.  PYTHON=~/miniconda3/python.exe $0 ..." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Cloning $REPO@$BRANCH ..."
git clone -q --branch "$BRANCH" --depth 1 "https://github.com/$REPO.git" "$WORK/repo"
SHA="$(git -C "$WORK/repo" rev-parse HEAD)"

echo "Running the suite in $IMAGE ..."
# Failure is the expected case here, so the non-zero exit must not abort us.
set +e
docker run --rm -v "$WORK/repo:/app" -w /app "$IMAGE" sh -c \
  'pip install -r requirements.txt -q --timeout 120 --retries 5 && python -m pytest -v --tb=long' \
  > "$WORK/run.log" 2>&1
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" -eq 0 ]; then
  STATUS="success"
  echo "Note: the suite PASSED. The agent only acts on failures."
else
  STATUS="failure"
fi

REPO="$REPO" BRANCH="$BRANCH" SHA="$SHA" STATUS="$STATUS" LOG="$WORK/run.log" \
"$PY" - > "$OUT" <<'PY'
import json
import os

with open(os.environ["LOG"], encoding="utf-8", errors="replace") as f:
    logs = f.read()

print(json.dumps({
    "repo": os.environ["REPO"],
    "branch": os.environ["BRANCH"],
    "commit_sha": os.environ["SHA"],
    "workflow_run_id": "0000000000",
    "test_logs": logs,
    "status": os.environ["STATUS"],
}, indent=2))
PY

echo "Wrote $OUT  (status=$STATUS, sha=${SHA:0:7})"
