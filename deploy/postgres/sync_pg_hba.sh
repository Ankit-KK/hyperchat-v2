#!/usr/bin/env bash
# Print <pg_hba.conf> with its HyperChat line equal to the one in <template>. Changes no file.
#   sync_pg_hba.sh <template> <pg_hba.conf>
# An existing "map=hyperchat" line is replaced in place; without one, the line is inserted above the first
# generic "local all all" rule. Refuses (exit 1) when there is neither, or more than one HyperChat line.
set -euo pipefail

TEMPLATE="$1"; CURRENT="$2"
LINE="$(grep -v '^#' "$TEMPLATE" | grep -v '^[[:space:]]*$')"
[ "$(printf '%s\n' "$LINE" | wc -l)" -eq 1 ] || { echo "template must hold exactly one rule" >&2; exit 1; }

HBA='^[^#]*map=hyperchat([[:space:]]|$)'
COUNT="$(grep -Ec "$HBA" "$CURRENT" || true)"
[ "$COUNT" -le 1 ] || { echo "more than one HyperChat line in $CURRENT" >&2; exit 1; }

if [ "$COUNT" -eq 1 ]; then
  awk -v line="$LINE" -v re="$HBA" '$0 ~ re { print line; next } { print }' "$CURRENT"
elif grep -q '^local[[:space:]]\+all[[:space:]]\+all[[:space:]]' "$CURRENT"; then
  awk -v line="$LINE" '!done && /^local[[:space:]]+all[[:space:]]+all[[:space:]]/ { print line; done = 1 } { print }' \
      "$CURRENT"
else
  echo "no HyperChat line and no generic \"local all all\" rule in $CURRENT" >&2
  exit 1
fi
