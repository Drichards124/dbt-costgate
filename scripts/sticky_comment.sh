#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Upsert a single "sticky" costgate comment on a pull request, using only the gh
# CLI (no third-party action). A hidden marker identifies costgate's own comment,
# so repeated runs edit that one comment instead of stacking new ones.
#
# Usage: sticky_comment.sh <pr-number> <marker> <body-file>
# Env:   GH_TOKEN (or GITHUB_TOKEN) — gh auth
#        GITHUB_REPOSITORY          — "owner/repo"
set -euo pipefail

PR="${1:?pr number required}"
MARKER="${2:?marker required}"
BODY_FILE="${3:?body file required}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"

# GitHub rejects issue comments longer than this many characters.
MAX=65536

# Build the body to send: guarantee the marker is present (so the next run can
# find this comment), then cap it at the size limit with a truncation note.
send="$(mktemp)"
trap 'rm -f "$send"' EXIT
if grep -qF "$MARKER" "$BODY_FILE"; then
  cat "$BODY_FILE" >"$send"
else
  {
    printf '%s\n\n' "$MARKER"
    cat "$BODY_FILE"
  } >"$send"
fi
if [ "$(wc -c <"$send")" -gt "$MAX" ]; then
  trunc="$(mktemp)"
  head -c $((MAX - 64)) "$send" >"$trunc"
  printf '\n\n_…report truncated._\n' >>"$trunc"
  mv "$trunc" "$send"
fi

# Find costgate's existing sticky, if any. --paginate evaluates --jq per page, so
# a match on a later page (or a stray duplicate) can emit several ids; take the
# first. pipefail is relaxed here so head closing the pipe early is not an error.
set +o pipefail
id="$(gh api --paginate "repos/${REPO}/issues/${PR}/comments" \
  --jq '.[] | select(.body | contains("'"$MARKER"'")) | .id' 2>/dev/null |
  head -n 1 | tr -d '[:space:]')"
set -o pipefail

if [ -n "$id" ]; then
  gh api --method PATCH "repos/${REPO}/issues/comments/${id}" -F body=@"$send" >/dev/null
  echo "costgate: updated sticky comment ${id}"
else
  gh api --method POST "repos/${REPO}/issues/${PR}/comments" -F body=@"$send" >/dev/null
  echo "costgate: posted a new sticky comment"
fi
