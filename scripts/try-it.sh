#!/usr/bin/env bash
#
# Drive one real agent run against a canned CI failure.
#
#   scripts/try-it.sh                      # against a local server on :8000
#   scripts/try-it.sh http://1.2.3.4:8000  # against a deployed one
#
# The payload in examples/ is a genuine pytest failure from the demo repo, so
# this exercises the whole production path. Nothing is simulated.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAYLOAD="$ROOT/examples/sample-payload.json"
BASE="${1:-http://127.0.0.1:8000}"

[ -f "$PAYLOAD" ] || { echo "missing $PAYLOAD" >&2; exit 1; }

# The secret has to match the server's. Read it from the same .env the server
# loads, unless it is already exported.
if [ -z "${WEBHOOK_SECRET:-}" ] && [ -f "$ROOT/agent/.env" ]; then
  # Strip a UTF-8 BOM, CR line endings and surrounding quotes. All three turn
  # up in hand-written .env files and all three break the header.
  WEBHOOK_SECRET="$(sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$ROOT/agent/.env" \
    | grep -E '^WEBHOOK_SECRET=' | head -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//')"
fi
WEBHOOK_SECRET="$(printf %s "${WEBHOOK_SECRET:-}" | tr -d '[:space:]')"

if [ -z "$WEBHOOK_SECRET" ]; then
  cat >&2 <<'EOF'
WEBHOOK_SECRET is not set.

Put it in agent/.env (the server reads the same file) or export it:
    export WEBHOOK_SECRET=...

Generate one with:
    python -c "import secrets; print(secrets.token_urlsafe(32))"
EOF
  exit 1
fi

echo "Checking $BASE/health ..."
if ! curl -fsS --max-time 10 "$BASE/health" >/dev/null 2>&1; then
  echo "No server answering at $BASE." >&2
  echo "Start one with:  cd agent && python main.py" >&2
  echo "            or:  docker compose up --build" >&2
  exit 1
fi

echo "Posting examples/sample-payload.json ..."
BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

# The server answers immediately and runs the pipeline in the background, so a
# 200 here means "accepted", not "fixed".
HTTP_CODE="$(curl -sS -o "$BODY" -w '%{http_code}' \
  -X POST "$BASE/webhook" \
  -H 'Content-Type: application/json' \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  --data-binary @"$PAYLOAD")"
RESPONSE="$(cat "$BODY")"

echo "HTTP $HTTP_CODE: $RESPONSE"
case "$HTTP_CODE" in
  200|202)
    cat <<EOF

Accepted. The run takes roughly 60-90 seconds.

Watch it:   python agent/dashboard.py     ->  http://127.0.0.1:8001/ui
Or logs:    the terminal running the server
EOF
    ;;
  401)
    echo >&2
    echo "Rejected: the secret does not match the server's WEBHOOK_SECRET." >&2
    echo "A byte order mark in agent/.env is the usual cause; see agent/.env.example." >&2
    exit 1
    ;;
  *)
    echo "Unexpected response." >&2
    exit 1
    ;;
esac
