#!/usr/bin/env bash
# Prints a ready-to-paste crontab line for this install.
# Usage:
#   ./cron-setup.sh          # daily at 6 AM (default)
#   ./cron-setup.sh hourly   # every hour
#   ./cron-setup.sh weekly   # Mondays at 6 AM
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/venv/bin/python"
LOG="$DIR/data/logs/cron.log"

if [ ! -x "$PY" ]; then
  echo "WARNING: $PY not found. Run ./setup.sh first." >&2
fi

FREQ="${1:-daily}"
case "$FREQ" in
  hourly) SCHED="0 * * * *" ;;
  daily)  SCHED="0 6 * * *" ;;
  weekly) SCHED="0 6 * * 1" ;;
  *)
    echo "Unknown frequency '$FREQ'. Use: hourly | daily | weekly" >&2
    exit 1
    ;;
esac

LINE="$SCHED cd $DIR && $PY $DIR/main.py >> $LOG 2>&1"

cat <<EOF
Add this line to your crontab ($FREQ):

  $LINE

To install it:

  crontab -e
  # paste the line above, save, exit

Verify with:  crontab -l
Watch logs:   tail -f $LOG
EOF
