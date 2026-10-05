#!/usr/bin/env bash
# Nightly logical backup of the HyperChat production database (cluster 17/hyperchat).
# Custom format, verified with pg_restore -l, 14-day retention. Off-site copy (R2) is added later.
set -euo pipefail

DB="${HYPERCHAT_BACKUP_DB:-hyperchat}"
PORT="${HYPERCHAT_BACKUP_PORT:-5432}"
DIR="${HYPERCHAT_BACKUP_DIR:-/var/backups/hyperchat}"
KEEP_DAYS="${HYPERCHAT_BACKUP_KEEP_DAYS:-14}"
BIN="/usr/lib/postgresql/17/bin"

install -d -m 700 -o root -g root "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/${DB}_${STAMP}.dump"
TMP="$OUT.partial"

umask 077
sudo -u postgres "$BIN/pg_dump" -h /var/run/postgresql -p "$PORT" -Fc -Z 9 "$DB" > "$TMP"
ENTRIES="$("$BIN/pg_restore" -l "$TMP" | grep -vc '^;')"
mv "$TMP" "$OUT"
chmod 600 "$OUT"
SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"
echo "backup ok db=$DB file=$(basename "$OUT") bytes=$(stat -c %s "$OUT") toc_entries=$ENTRIES sha256=$SHA"

find "$DIR" -maxdepth 1 -type f \( -name "${DB}_*.dump" -o -name "${DB}_*.dump.sha256" \) -mtime +"$KEEP_DAYS" -print -delete \
  | sed 's/^/pruned /'
