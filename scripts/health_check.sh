#!/usr/bin/env bash
# ============================================================================
# CLaaS — health check
# Verifies the project is intact and the matching engine actually works.
# Safe to run any time. From anywhere:
#
#     bash scripts/health_check.sh
#
# What it checks:
#   1. every required file exists and is non-empty
#   2. all Python modules compile
#   3. frontend wiring: every element id app.js uses exists in index.html,
#      CSS/JS braces balance (catches truncated or mangled edits)
#   4. LIVE test: boots a private copy of the engine on a scratch port
#      (simulators off, so it's deterministic) and reproduces the spec's
#      partial-fill scenario over HTTP: bid 100 vs ask 50 -> fill 50, rest 50.
#
# Exits 0 if everything passed, 1 otherwise. Override the scratch port with:
#     HEALTH_PORT=9911 bash scripts/health_check.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
PORT="${HEALTH_PORT:-8877}"
PASS=0; FAIL=0

ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "=================================================================="
echo " CLaaS health check — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================================="

# ---- 1) required files ------------------------------------------------------
echo "[1] required files"
for f in engine.py api.py simulators.py dashboard.py requirements.txt \
         run.sh static/index.html static/styles.css static/app.js \
         scripts/real_prices.py; do
  if [ -s "$f" ]; then ok "$f"; else bad "$f is missing or empty"; fi
done

# ---- 2) python compiles -----------------------------------------------------
echo "[2] python modules compile"
if "$PY" -m py_compile engine.py api.py simulators.py dashboard.py 2>/dev/null; then
  ok "engine.py api.py simulators.py dashboard.py"
else
  bad "python compile failed — a module has a syntax error"
fi

# ---- 3) frontend wiring -----------------------------------------------------
echo "[3] frontend wiring (html <-> js <-> css)"
if "$PY" - <<'EOF'
import re
html = open("static/index.html").read()
js   = open("static/app.js").read()
css  = open("static/styles.css").read()

# braces balanced (catches truncated files / mangled edits)
for name, src in (("styles.css", css), ("app.js", js)):
    assert src.count("{") == src.count("}"), f"{name}: unbalanced braces"
    assert src.count("(") == src.count(")"), f"{name}: unbalanced parens"

# every id app.js references must exist in the html
js_ids = set(re.findall(r'\$\("([^"]+)"\)', js)) | {"bidSubmit", "askSubmit"}
html_ids = set(re.findall(r'id="([^"]+)"', html))
missing = js_ids - html_ids
assert not missing, f"ids referenced by app.js but missing from index.html: {missing}"
print(f"    ({len(js_ids)} ids wired, braces balanced)")
EOF
then ok "frontend wiring intact"; else bad "frontend wiring broken — see message above"; fi

# ---- 4) live engine test ----------------------------------------------------
echo "[4] live matching-engine test (scratch port $PORT, bots off)"
if "$PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  # env -u keeps the test hermetic: no Supabase mirroring, no simulator bots,
  # so the health check never touches or mutates a live database.
  env -u SUPABASE_URL -u SUPABASE_KEY -u CLAAS_DB_BOOTSTRAP \
      RUN_SIMULATORS=0 "$PY" -m uvicorn api:app --port "$PORT" >/tmp/claas_health.log 2>&1 &
  SRV=$!
  for i in $(seq 1 40); do
    curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
  done

  if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
    ok "engine boots, GET /health responds"

    # the spec's core scenario: ask 50 @ $2.00, then bid 100 @ $2.50
    curl -s -X POST "http://localhost:$PORT/ask" \
         -H 'Content-Type: application/json' \
         -d '{"provider":"HealthCheck","hardware":"H100","price":2.00,"quantity":50}' >/dev/null
    RES=$(curl -s -X POST "http://localhost:$PORT/bid" \
          -H 'Content-Type: application/json' \
          -d '{"startup":"HealthCheck","price":2.50,"quantity":100}')

    if echo "$RES" | "$PY" -c '
import json, sys
j = json.load(sys.stdin)
assert j["result"] == "partially_filled", j["result"]
assert j["remaining_quantity"] == 50, j["remaining_quantity"]
assert j["trades"][0]["quantity"] == 50 and j["trades"][0]["price"] == 2.00
assert j["spot_price"] == 2.00
' 2>/dev/null; then
      ok "partial fill over HTTP: bid 100 vs ask 50 -> 50 filled, 50 resting, spot \$2.00"
    else
      bad "partial-fill scenario returned wrong result — engine logic broken"
    fi

    # order tracking endpoint
    OID=$(echo "$RES" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["order"]["order_id"])' 2>/dev/null)
    if [ -n "${OID:-}" ] && curl -sf "http://localhost:$PORT/order/$OID" >/dev/null 2>&1; then
      ok "order tracking: GET /order/{id} responds"
    else
      bad "order tracking endpoint failed"
    fi
  else
    bad "engine did not start — check /tmp/claas_health.log"
  fi

  kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
else
  echo "  ⏭️  skipped (dependencies not installed yet) — run ./run.sh once, then re-run this"
fi

# ---- summary -----------------------------------------------------------------
echo "------------------------------------------------------------------"
echo " result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo " status: ALL GOOD 🟢" || echo " status: NEEDS ATTENTION 🔴"
echo "------------------------------------------------------------------"
exit $(( FAIL > 0 ? 1 : 0 ))
