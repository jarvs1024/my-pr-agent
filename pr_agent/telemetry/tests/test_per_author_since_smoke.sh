#!/usr/bin/env bash
# Smoke test: /metrics/authors?since=... must return 200, not 500.
# Regression check for the sug_params NameError bug in per_author_stats().
set -euo pipefail
BASE="${PR_AGENT_BASE:-http://127.0.0.1:5050}"
TOKEN="${PR_AGENT_TOKEN:-${REVIEW_TELEMETRY_HTTP_TOKEN:-}}"
[ -z "$TOKEN" ] && { echo "missing PR_AGENT_TOKEN / REVIEW_TELEMETRY_HTTP_TOKEN"; exit 2; }
SINCE="${SINCE:-2026-07-01T00:00:00+00:00}"
for ep in metrics/overview metrics/rules metrics/authors; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/telemetry/$ep?since=$SINCE")
  echo "$code  $ep?since=$SINCE"
  [ "$code" = "200" ] || { echo "FAIL: $ep returned $code"; exit 1; }
done
echo "OK"
