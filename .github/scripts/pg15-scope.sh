#!/usr/bin/env bash
# Decide whether the PostgreSQL 15 compatibility job has work to do: writes run=true|false to $GITHUB_OUTPUT.
#
#   .github/scripts/pg15-scope.sh <base-sha>   (the PR's base, or the previous main commit on push)
#
# Runs on any change to migrations, roles, the database tools, CI, or the backend code and tests that issue role- or
# permission-sensitive SQL. Fails OPEN: without a usable base (manual run, first push, a base it cannot fetch) it runs.
set -euo pipefail

RELEVANT='^(database/|deploy/postgres/|\.github/|backend/app/(db|auth|kyc|stepup|admin|backup)/|backend/tests/(auth|kyc|stepup|admin|backup)/|backend/requirements)'
out="${GITHUB_OUTPUT:-/dev/stdout}"
base="${1:-}"

if [[ ! "$base" =~ ^[0-9a-f]{40}$ || "$base" =~ ^0+$ ]] \
   || ! { git cat-file -e "$base^{commit}" 2>/dev/null || git fetch -q --depth=1 origin "$base"; }; then
  echo "no usable base: running"
  echo "run=true" >> "$out"
  exit 0
fi
changed="$(git diff --name-only "$base" HEAD)"
if grep -qE "$RELEVANT" <<<"$changed"; then
  echo "relevant changes:"; grep -E "$RELEVANT" <<<"$changed" | head -20 | sed 's/^/  /'
  echo "run=true" >> "$out"
else
  echo "no database, role, auth, KYC, step-up, admin, backup or CI change: nothing to run on PostgreSQL 15"
  echo "run=false" >> "$out"
fi
