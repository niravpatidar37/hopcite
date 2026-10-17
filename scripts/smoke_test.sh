#!/usr/bin/env bash
# Post-deploy smoke test for the Cloud Run API (used by .github/workflows/deploy.yml; also handy by hand).
#
#   scripts/smoke_test.sh https://kgrag-staging-api-xxxx.a.run.app [ID_TOKEN]
#
# Checks: /healthz 200, /readyz 200 with neo4j+postgres true (retried while a revision warms up), and
# POST /ask without an API key is refused with 401 (auth is enforced on the deployed revision).
# Sends no API key and makes no LLM call, so it is free and safe to run against prod.
set -euo pipefail

url="${1:?usage: smoke_test.sh BASE_URL [ID_TOKEN]}"
url="${url%/}"
token="${2:-}"
attempts="${SMOKE_ATTEMPTS:-12}"
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

auth=()
if [ -n "$token" ]; then
  auth=(-H "Authorization: Bearer ${token}")
fi

call() { # call METHOD PATH [curl args...] -> prints HTTP status, body in $body
  local method="$1" path="$2"
  shift 2
  # curl prints 000 via -w when the connection fails; `|| true` keeps set -e from aborting the retry loop.
  curl -sS -o "$body" -w '%{http_code}' --max-time 15 -X "$method" "${auth[@]}" "$@" "${url}${path}" 2>/dev/null || true
}

fail() {
  echo "SMOKE FAIL: $*" >&2
  head -c 500 "$body" >&2 || true
  echo >&2
  exit 1
}

for i in $(seq 1 "$attempts"); do
  code="$(call GET /healthz)"
  if [ "$code" = "200" ]; then
    echo "ok   /healthz 200 (attempt $i)"
    break
  fi
  [ "$i" = "$attempts" ] && fail "/healthz returned $code after $attempts attempts"
  sleep 5
done

for i in $(seq 1 "$attempts"); do
  code="$(call GET /readyz)"
  if [ "$code" = "200" ] && grep -q '"neo4j":true' "$body" && grep -q '"postgres":true' "$body"; then
    echo "ok   /readyz 200 (attempt $i)"
    break
  fi
  [ "$i" = "$attempts" ] && fail "/readyz not ready after $attempts attempts (last $code)"
  sleep 5
done

code="$(call POST /ask -H 'content-type: application/json' -d '{"question":"smoke test: who supplies TSMC?"}')"
[ "$code" = "401" ] || fail "POST /ask without an API key returned $code, expected 401"
echo "ok   POST /ask without key -> 401"

echo "SMOKE PASS ${url}"
