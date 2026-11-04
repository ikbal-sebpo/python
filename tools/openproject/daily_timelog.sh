#!/usr/bin/env bash
# Daily OpenProject time log (systemd user timer — see docs/OPENPROJECT.md §4).
# Logs the last 7 days through yesterday from Orbis production, so a late
# punch sync or an approved timesheet correction is picked up on later runs.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$HOME/.config/orbis-openproject/daily.log"
mkdir -p "$(dirname "$LOG")"
cd "$REPO"
{ echo "=== $(date '+%F %T')"; "$REPO/venv/bin/python" tools/openproject/timelog.py --from-orbis --apply --quiet; } >>"$LOG" 2>&1
rc=$?
if [ "$rc" -eq 3 ]; then
  notify-send -u critical "Orbis time log" "OpenProject session expired. Run: venv/bin/python tools/openproject/sync.py --login, then: systemctl --user start orbis-timelog" || true
elif [ "$rc" -ne 0 ]; then
  notify-send -u critical "Orbis time log failed (exit $rc)" "See $LOG" || true
fi
exit "$rc"
