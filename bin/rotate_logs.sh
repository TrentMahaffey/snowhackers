#!/usr/bin/env bash
# Weekly log housekeeping (cron, Sunday 05:00): rotate the appending cron logs and drop old
# per-run logs. Per-run update/backfill logs older than 60 days are deleted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
/usr/sbin/logrotate -s "$HERE/logs/.logrotate.state" "$HERE/logrotate.conf"
find "$HERE/logs" -maxdepth 1 -type f \( -name 'update_*.log' -o -name 'backfill_*.log' \) -mtime +60 -delete
echo "[$(date +%F\ %T)] logs: $(du -sh "$HERE/logs" | cut -f1)"
