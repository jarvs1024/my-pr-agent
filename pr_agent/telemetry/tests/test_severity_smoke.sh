#!/usr/bin/env bash
# Smoke test: /metrics/severity and severity_breakdown integration.
# - /api/v1/telemetry/metrics/severity returns 200 + severity rows
# - /api/v1/telemetry/metrics/overview embeds severity_breakdown
# - /api/v1/telemetry/mrs/{p}/{m}/suggestions attaches severity per item
# - /api/v1/telemetry/mrs/{p}/{m}/stats includes severity_counts
# - since filter does not 500 (regression check)
set -euo pipefail
BASE="${PR_AGENT_BASE:-http://127.0.0.1:5050}"
TOKEN="${PR_AGENT_TOKEN:-${REVIEW_TELEMETRY_HTTP_TOKEN:-}}"
[ -z "$TOKEN" ] && { echo "missing PR_AGENT_TOKEN / REVIEW_TELEMETRY_HTTP_TOKEN"; exit 2; }
SINCE="${SINCE:-2026-07-01T00:00:00+00:00}"

check_200() {
  local url="$1" name="$2"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "$url")
  echo "$code  $name"
  [ "$code" = "200" ] || { echo "FAIL: $name returned $code"; exit 1; }
}

check_field() {
  local url="$1" name="$2" jq_filter="$3"
  local val
  val=$(curl -s -H "Authorization: Bearer $TOKEN" "$url" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for f in '$jq_filter'.split('.'):
        d = d.get(f, {}) if isinstance(d, dict) else (d[int(f)] if isinstance(d, list) and f.isdigit() else None)
    print(d if d is not None else '<missing>')
except Exception as e:
    print('ERR', e)
")
  echo "  $name.$jq_filter = $val"
  [ "$val" != "<missing>" ] && [ "$val" != "None None" ] || { echo "FAIL: $name missing $jq_filter"; exit 1; }
}

echo "--- endpoints (no since) ---"
check_200 "$BASE/api/v1/telemetry/metrics/severity" "metrics/severity"
check_200 "$BASE/api/v1/telemetry/metrics/overview" "metrics/overview"
check_200 "$BASE/api/v1/telemetry/mrs/34/48/suggestions" "mrs/34/48/suggestions"
check_200 "$BASE/api/v1/telemetry/mrs/34/48/stats" "mrs/34/48/stats"

echo
echo "--- endpoints (since filter) ---"
check_200 "$BASE/api/v1/telemetry/metrics/severity?since=$SINCE" "metrics/severity?since"
check_200 "$BASE/api/v1/telemetry/metrics/overview?since=$SINCE" "metrics/overview?since"

echo
echo "--- payload structure ---"
check_field "$BASE/api/v1/telemetry/metrics/severity" "metrics/severity" "0.severity"
check_field "$BASE/api/v1/telemetry/metrics/overview" "metrics/overview" "severity_breakdown"
check_field "$BASE/api/v1/telemetry/mrs/34/48/suggestions" "mrs/34/48/suggestions" "0.severity"
check_field "$BASE/api/v1/telemetry/mrs/34/48/stats" "mrs/34/48/stats" "severity_counts"

echo
echo "--- NO-LOG-EXC must land in critical (pattern fallback) ---"
out=$(curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/telemetry/mrs/34/48/suggestions")
# Find a NO-LOG-EXC tagged suggestion and check its severity
result=$(echo "$out" | python3 -c "
import sys, json
items = json.load(sys.stdin)
hits = [s for s in items if 'ZLG-RULE-NO-LOG-EXC' in s.get('rule_keys', [])]
if not hits:
    print('NO_HIT')
else:
    sev = hits[0].get('severity', '?')
    src = hits[0].get('severity_source', '?')
    print(f'{sev}|{src}')
")
echo "NO-LOG-EXC suggestion: $result"
sev=${result%%|*}
[ "$sev" = "critical" ] || { echo "FAIL: NO-LOG-EXC expected critical, got $sev"; exit 1; }

echo
echo "OK"
