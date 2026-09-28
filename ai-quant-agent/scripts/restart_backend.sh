#!/bin/bash
# ============================================
# AI Quant Agent - backend independent restart script (Evolution Tier2/3, E2/G9)
#
# Why a separate script?
#   The Evolution Agent (self_improver) runs inside the uvicorn process and
#   CANNOT kill itself. Tier2/3 (business logic / core framework) code changes
#   require a process restart - this script performs the restart from an
#   INDEPENDENT process (aligned with plans/11-进化Agent.md §8.3 E2/G9).
#
# Features:
#   - PID / port detection (reuses BACKEND_PATTERN from start_all.sh)
#   - SIGTERM graceful stop -> SIGKILL on timeout
#   - nohup restart + logs
#   - health check (GET /api/v1/system/health)
#   - on failure: rollback .bak then retry once (G9)
#   - avoid trading window by default; --force bypasses
#
# Usage:
#   bash scripts/restart_backend.sh             # restart backend (health check + rollback)
#   bash scripts/restart_backend.sh --check     # only check status (no restart)
#   bash scripts/restart_backend.sh --rollback  # rollback latest evolution change, then restart
#   bash scripts/restart_backend.sh --force     # ignore trading window
# ============================================
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

BACKEND_PATTERN="uvicorn app.main:app --host 0.0.0.0 --port 8000"
BACKEND_HEALTH="http://localhost:8000/api/v1/system/health"
BACKEND_LOG="logs/backend.log"
MAX_WAIT=40
GRACE=3

MODE="${1:-restart}"

info() { echo "[restart_backend] $*"; }
die()  { echo "[restart_backend] [X] $*" >&2; exit 1; }

# -- helpers -----------------------------------------------------------
find_backend_pid() { pgrep -f "$BACKEND_PATTERN" 2>/dev/null | tr '\n' ' '; }
health_ok() { curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; }

kill_backend() {
    pkill -f "$BACKEND_PATTERN" 2>/dev/null || true
    sleep "$GRACE"
    if pgrep -f "$BACKEND_PATTERN" > /dev/null 2>&1; then
        info "graceful stop timeout, SIGKILL..."
        pkill -9 -f "$BACKEND_PATTERN" 2>/dev/null || true
        sleep 1
    fi
}

start_backend() {
    cd backend || return 1
    nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > ../"$BACKEND_LOG" 2>&1 &
    local pid=$!
    cd ..
    echo "$pid"
}

wait_health() {
    local i
    for i in $(seq 1 "$MAX_WAIT"); do
        if health_ok; then return 0; fi
        sleep 1
    done
    return 1
}

# Clear the "pending restart" flag after a successful restart:
# the new process has already loaded the Tier2/3 code changes, so the flag
# (backend/data/evolution_restart_pending.json) is no longer meaningful.
# Without this, the Evolution Center shows "待重启代码" forever even though
# the code is already live.
clear_restart_flag() {
    local flag="$PROJECT_DIR/backend/data/evolution_restart_pending.json"
    if [ -f "$flag" ]; then
        rm -f "$flag"
        info "cleared pending-restart flag: $flag"
    fi
}

# Restore the most recent evolution change .bak (read self_improve_log.json audit)
restore_last_backup() {
    local py="$PROJECT_DIR/backend/venv/bin/python3"
    [ -x "$py" ] || py="python3"
    "$py" - "$PROJECT_DIR" <<'PYEOF'
import json, os, shutil, sys
root = sys.argv[1]
audit = os.path.join(root, "backend/data/self_improve_log.json")
if not os.path.exists(audit):
    print("no-audit")
    sys.exit(0)
try:
    with open(audit, encoding="utf-8") as f:
        records = json.load(f)
except Exception:
    print("audit-unreadable")
    sys.exit(0)
for r in reversed(records):
    if r.get("status") == "ok" and r.get("backup") and os.path.exists(r["backup"]):
        src = r["backup"][:-4] if r["backup"].endswith(".bak") else r["backup"]
        if src and os.path.exists(src):
            try:
                shutil.copy2(r["backup"], src)
                print("restored:" + src)
                sys.exit(0)
            except Exception as e:
                print("restore-failed:" + str(e))
                sys.exit(1)
print("no-backup")
PYEOF
}

# Avoid trading window: Mon-Fri 09:15-11:30 / 13:00-15:00 (A-share trading hours)
is_trade_window() {
    local dow hm
    dow=$(date "+%u")
    hm=$(date "+%H%M")
    if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ]; then
        if [ "$hm" -ge "0915" ] && [ "$hm" -le "1130" ]; then return 0; fi
        if [ "$hm" -ge "1300" ] && [ "$hm" -le "1500" ]; then return 0; fi
    fi
    return 1
}

# -- mode dispatch -----------------------------------------------------
case "$MODE" in
  --check)
    if health_ok; then
        echo "[restart_backend] backend healthy (PID: $(find_backend_pid))"
        exit 0
    else
        echo "[restart_backend] backend not ready (PID: $(find_backend_pid))"
        exit 1
    fi
    ;;
  --force)
    : # bypass trading window
    ;;
  --rollback)
    info "rolling back latest evolution change..."
    restore_last_backup
    ;;
  restart)
    if is_trade_window; then
        die "in trading window (Mon-Fri 09:15-11:30 / 13:00-15:00); use --force to override"
    fi
    ;;
  *)
    die "unknown arg: $MODE (restart | --check | --rollback | --force)"
    ;;
esac

# -- unified restart flow ---------------------------------------------
info "restarting backend..."
OLD_PIDS="$(find_backend_pid)"
if [ -n "$OLD_PIDS" ]; then
    info "stopping old PID: $OLD_PIDS"
    kill_backend
fi

# make sure port 8000 is free
if lsof -ti tcp:8000 -sTCP:LISTEN > /dev/null 2>&1; then
    info "port 8000 still busy, force killing residual process..."
    pkill -9 -f "$BACKEND_PATTERN" 2>/dev/null || true
    sleep 1
fi

NEW_PID="$(start_backend)"
info "new PID: ${NEW_PID:-unknown}"

info "waiting for health (max ${MAX_WAIT}s)..."
if wait_health; then
    info "backend restarted OK (PID: ${NEW_PID:-unknown})"
    clear_restart_flag
    exit 0
fi

# restart failed -> rollback .bak -> try once more
info "health check failed, rolling back .bak and retrying..."
restore_last_backup
kill_backend
NEW_PID="$(start_backend)"
if wait_health; then
    info "backend restarted OK after rollback (PID: ${NEW_PID:-unknown})"
    clear_restart_flag
    exit 0
fi

die "restart failed, check $BACKEND_LOG"
