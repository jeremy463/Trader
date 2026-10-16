#!/usr/bin/env bash
# Online-safe SQLite backup with integrity check, retention and an optional off-site hook.
# Env: APEX_CONFIG (default /etc/apex/web.json) APEX_BACKUP_DIR (/var/backups/apex) APEX_BACKUP_KEEP (14) PYTHON (/opt/apex_trader/.venv/bin/python)
#      APEX_OFFSITE_CMD  optional command run with the new file path as $1, e.g. 'rclone copy "$1" remote:apex-backups/'
set -euo pipefail
CONFIG="${APEX_CONFIG:-/etc/apex/web.json}"; DEST="${APEX_BACKUP_DIR:-/var/backups/apex}"; KEEP="${APEX_BACKUP_KEEP:-14}"; PY="${PYTHON:-/opt/apex_trader/.venv/bin/python}"
umask 077; mkdir -p "$DEST"
OUT="$DEST/apex-$(date -u +%Y%m%dT%H%M%SZ).db"
(cd "${APEX_HOME:-/opt/apex_trader}" && "$PY" -m apex.cli --config "$CONFIG" web-backup --out "$OUT")
"$PY" - "$OUT" <<'PYEOF' || { echo "backup failed its integrity check; removed" >&2; rm -f "$OUT"; exit 1; }
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
sys.exit(0 if r == "ok" else 1)
PYEOF
ls -1t "$DEST"/apex-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm --
[ -n "${APEX_OFFSITE_CMD:-}" ] && bash -c "$APEX_OFFSITE_CMD" _ "$OUT"
echo "backup ok: $OUT"
