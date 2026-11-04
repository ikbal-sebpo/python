#!/usr/bin/env bash
# Orbis MySQL backup: dump -> verify -> gzip -> encrypt (age, public key) -> upload -> prune.
#
# Runs from the `ubuntu` crontab on the production VPS (an OS-level job, not an
# app job — Celery beat is for manage.py commands only, see CLAUDE.md). Replaces
# the 2026-06 script that embedded the orbis_user password, dumped plaintext,
# uploaded it unencrypted and kept exactly one copy on Google Drive.
#
# Install (deploy/README.md "MySQL backups"):
#   sudo apt install age
#   sudo install -m 0755 deploy/mysql-backup.sh /home/ubuntu/mysql_backup/mysql-backup.sh
#   /home/ubuntu/mysql_backup/backup.conf        (0600)  [client] user=orbis_backup password=...
#   /home/ubuntu/mysql_backup/backup-recipient.txt (0644) age1... public key(s), one per line
#   crontab: 1 0 * * * /home/ubuntu/mysql_backup/mysql-backup.sh >> /home/ubuntu/mysql_backup/backup.log 2>&1
#
# Restore (needs the age PRIVATE key, held offline — never on this host):
#   age -d -i backup-key.txt 2026-10-05_000101-orbis_db.sql.gz.age | gunzip | mysql --defaults-extra-file=... orbis_db
#
# No secrets in this file. Everything sensitive is in backup.conf (MySQL creds,
# mode 0600) and the age private key, which does not exist on the server at all —
# the server can encrypt backups but cannot read them back.
set -euo pipefail
umask 077

BASE="${BACKUP_BASE:-/home/ubuntu/mysql_backup}"
CONF="$BASE/backup.conf"                     # mysql --defaults-extra-file
RECIPIENTS="$BASE/backup-recipient.txt"     # age public keys, one per line
BACKUP_PATH="$BASE/backup"
DB="${BACKUP_DB:-orbis_db}"
GDRIVE="${GDRIVE_BIN:-/usr/bin/gdrive}"
GDRIVE_FOLDER_ID="${GDRIVE_FOLDER_ID:-1jigU3k8GszHCYGpg6bSZDwH766OPnTjA}"
LOCAL_KEEP_DAYS="${LOCAL_KEEP_DAYS:-7}"     # encrypted copies kept on the VPS
DRIVE_KEEP="${DRIVE_KEEP:-30}"              # encrypted copies kept on Google Drive (newest N)
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"      # optional: pinged on success, /fail on failure

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() {
  log "FAILED: $*"
  [ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL/fail" >/dev/null 2>&1 || true
  exit 1
}

for f in "$CONF" "$RECIPIENTS"; do [ -r "$f" ] || fail "missing $f"; done
[ "$(stat -c %a "$CONF")" = "600" ] || fail "$CONF must be mode 0600"
command -v age >/dev/null || fail "age is not installed (sudo apt install age)"
mkdir -p "$BACKUP_PATH"

stamp="$(date '+%Y-%m-%d_%H%M%S')"
plain="$BACKUP_PATH/$stamp-$DB.sql.gz"       # exists only for the few seconds between dump and encrypt
final="$plain.age"
trap 'rm -f "$plain"' EXIT

# --- 1. Dump ---------------------------------------------------------------
# --single-transaction: consistent InnoDB snapshot without locking the app out.
# --no-tablespaces: avoids needing the PROCESS privilege (MySQL 8.0.21+).
# --set-gtid-purged=OFF: the dump must restore onto any server, not just a replica.
log "dumping $DB"
mysqldump --defaults-extra-file="$CONF" \
  --single-transaction --quick --routines --triggers --events \
  --hex-blob --no-tablespaces --set-gtid-purged=OFF \
  --databases "$DB" | gzip -6 > "$plain"
# `pipefail` makes a mysqldump error fail the pipeline, but verify the artefact too:
gzip -t "$plain" || fail "gzip integrity check failed"
zcat "$plain" | tail -n 1 | grep -q '^-- Dump completed' || fail "dump is truncated (no 'Dump completed' trailer)"
size=$(stat -c %s "$plain")
[ "$size" -gt 1048576 ] || fail "dump suspiciously small ($size bytes)"

# --- 2. Encrypt (public key only on this host) ------------------------------
age -R "$RECIPIENTS" -o "$final" "$plain" || fail "encryption failed"
rm -f "$plain"
log "wrote $(basename "$final") ($(du -h "$final" | cut -f1))"

# --- 3. Upload ---------------------------------------------------------------
upload_output=$("$GDRIVE" files upload --parent "$GDRIVE_FOLDER_ID" "$final" 2>&1) \
  || fail "gdrive upload failed: $upload_output"
echo "$upload_output" | grep -q "$(basename "$final")" || fail "gdrive upload not confirmed: $upload_output"
log "uploaded to Google Drive folder $GDRIVE_FOLDER_ID"

# --- 4. Prune: newest $DRIVE_KEEP on Drive, $LOCAL_KEEP_DAYS days locally -----
# File names start with the timestamp, so a lexical sort is a chronological sort.
# Only runs after a confirmed upload, so a failed run never shrinks the set.
"$GDRIVE" files list --query "'$GDRIVE_FOLDER_ID' in parents" --skip-header \
  | awk '{print $1, $2}' | sort -k2 | head -n -"$DRIVE_KEEP" | awk '{print $1}' \
  | while read -r file_id; do
      [ -n "$file_id" ] && "$GDRIVE" files delete "$file_id" >/dev/null && log "pruned Drive file $file_id"
    done
find "$BACKUP_PATH" -type f -name "*-$DB.sql.gz.age" -mtime +"$LOCAL_KEEP_DAYS" -print -delete | sed 's/^/pruned local /'
# Any plaintext .sql left over from the old script is a liability — remove it.
find "$BACKUP_PATH" -type f \( -name '*.sql' -o -name '*.sql.gz' \) -print -delete | sed 's/^/removed plaintext /'

[ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" >/dev/null 2>&1 || true
log "done"
