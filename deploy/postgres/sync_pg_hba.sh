#!/usr/bin/env bash
# Print <pg_hba.conf> with the HyperChat rule from <template> placed where it takes effect. Changes no file.
#   sync_pg_hba.sh <template> <pg_hba.conf>
#
# PostgreSQL uses the FIRST matching rule, so the HyperChat rule must precede every "local" rule that could
# match one of its roles: a user column of "all", a "+group" or "@file" reference, or a HyperChat role name.
# An existing HyperChat rule ("map=hyperchat") that already precedes all of them is replaced in place; one below
# such a rule is moved directly above the first of them; without one, the rule is inserted there. Every other
# line (rules and comments) is kept as it is. Fails closed (exit 1, nothing printed) on more than one HyperChat
# rule, include directives or line continuations (rules this script cannot order), or when there is neither a
# HyperChat rule nor a local rule to place it above.
set -euo pipefail

TEMPLATE="$1"; CURRENT="$2"
LINE="$(grep -v '^#' "$TEMPLATE" | grep -v '^[[:space:]]*$')"
[ "$(printf '%s\n' "$LINE" | wc -l)" -eq 1 ] || { echo "template must hold exactly one rule" >&2; exit 1; }

awk -v line="$LINE" '
  function competes(users,   list, k, m, u) {  # could this user column match one of the HyperChat roles?
    m = split(users, list, ",")
    for (k = 1; k <= m; k++) {
      u = list[k]; gsub(/"/, "", u)
      if (u == "all" || u ~ /^[+@]/ || u in want) return 1
    }
    return 0
  }
  function die(msg) { print "pg_hba.conf: " msg > "/dev/stderr"; failed = 1; exit 1 }
  BEGIN { split(line, lf); m = split(lf[3], lr, ","); for (k = 1; k <= m; k++) want[lr[k]] = 1 }
  { row[NR] = $0; t = $0; sub(/#.*/, "", t) }
  t ~ /^[[:space:]]*$/ { next }
  { c = t; sub(/[[:space:]]+$/, "", c) }
  substr(c, length(c)) == "\\" { die("line " NR ": continuation lines are not supported") }
  { split(t, f) }
  f[1] ~ /^include/ { die("line " NR ": include directives are not supported") }
  t ~ /map=hyperchat([[:space:]]|$)/ {
    if (hc) die("more than one HyperChat rule (lines " hc " and " NR ")")
    hc = NR; next
  }
  f[1] == "local" && !first && competes(f[3]) { first = NR }
  END {
    if (failed) exit 1
    if (!hc && !first) { print "pg_hba.conf: no HyperChat rule and no local rule to place it above" > "/dev/stderr"; exit 1 }
    in_place = hc && (!first || hc < first)
    for (i = 1; i <= NR; i++) {
      if (i == hc) { if (in_place) print line; continue }
      if (i == first && !in_place) print line
      print row[i]
    }
  }
' "$CURRENT"
