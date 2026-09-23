#!/bin/bash
# Deploy the newest master, but only once CI has passed on it, and roll back if
# the bot does not come back up. Run by barnabus-update.timer every 5 minutes.
set -uo pipefail
APP=/opt/barnabus/app
VENV=/opt/barnabus/venv
REPO=Raw-Darkness/barnabus-bot
SVC=barnabusbot
STATE=/var/lib/barnabus-update
mkdir -p "$STATE"
[ -f /etc/barnabus/update.env ] && . /etc/barnabus/update.env   # optional: ALERT_WEBHOOK=<discord webhook url>

log() { echo "update: $*"; }
alert() {
    log "$*"
    [ -n "${ALERT_WEBHOOK:-}" ] || return 0
    body=$(python3 -c 'import json,sys; print(json.dumps({"content": "🔨 Barnabus deploy: " + sys.argv[1]}))' "$*")
    curl -fsS -m 10 -H 'Content-Type: application/json' -d "$body" "$ALERT_WEBHOOK" >/dev/null || true
}
as_app() { runuser -u barnabus -- "$@"; }

cd "$APP" || exit 1
as_app git fetch -q origin master || { log "fetch failed, will retry"; exit 0; }
OLD=$(as_app git rev-parse HEAD)
NEW=$(as_app git rev-parse origin/master)
[ "$OLD" = "$NEW" ] && exit 0
if grep -qx "$NEW" "$STATE/rejected" 2>/dev/null; then exit 0; fi
SHORT=${NEW:0:7}
SUBJECT=$(as_app git log -1 --format=%s "$NEW")

# --- CI gate --------------------------------------------------------------
ci=$(curl -fsS -m 15 -H 'Accept: application/vnd.github+json' \
      "https://api.github.com/repos/$REPO/commits/$NEW/check-runs" | python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs or any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "skipped", "neutral") for r in runs):
    print("success")
else:
    print("failure")' 2>/dev/null)
case "$ci" in
    success) ;;
    failure) echo "$NEW" >> "$STATE/rejected"; alert "CI failed on $SHORT ($SUBJECT) — not deploying."; exit 0 ;;
    *) exit 0 ;;   # still running, or GitHub unreachable: try again next tick
esac

REQ_CHANGED=$(as_app git diff --name-only "$OLD" "$NEW" -- requirements.txt)

rollback() {
    as_app git reset -q --hard "$OLD"
    [ -n "$REQ_CHANGED" ] && as_app "$VENV/bin/pip" install -q -r requirements.txt
    systemctl restart "$SVC"
    echo "$NEW" >> "$STATE/rejected"
    alert "$SHORT ($SUBJECT) failed: $1. Rolled back to ${OLD:0:7}."
    exit 1
}

log "deploying ${OLD:0:7} -> $SHORT: $SUBJECT"
as_app git reset -q --hard "$NEW"
if [ -n "$REQ_CHANGED" ]; then
    as_app "$VENV/bin/pip" install -q -r requirements.txt || rollback "dependency install"
fi

# Import the package against the example config before touching the live bot.
TMP=$(as_app mktemp -d)
( cd "$TMP" && BOT_CONFIG="$APP/Config.example.json" PYTHONPATH="$APP" \
    runuser -u barnabus -- "$VENV/bin/python" -c "import barnabus.app" ) >/dev/null 2>&1 || { rm -rf "$TMP"; rollback "import smoke test"; }
rm -rf "$TMP"

SINCE=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart "$SVC"
for _ in $(seq 1 45); do
    sleep 2
    if journalctl -u "$SVC" --since "$SINCE" --no-pager -q | grep -q "READY as"; then
        log "deployed $SHORT"
        [ -n "${ANNOUNCE_DEPLOYS:-}" ] && alert "deployed $SHORT ($SUBJECT)"
        exit 0
    fi
done
rollback "bot did not reach READY within 90s"
