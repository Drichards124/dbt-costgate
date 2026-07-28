#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Print the versions the GitHub Marketplace listing publishes, one per line.
#
# The listing is published per release, by hand, from the release editor. It does
# not follow new releases: it stays at whichever release last had the box ticked,
# so it can advertise old code indefinitely with nothing reporting it. No REST or
# GraphQL field exposes this — checked, not assumed — so reading the page is the
# only way to know.
#
# READ `releases[]`, NEVER `latestRelease`. The page embeds both and they mean
# different things:
#
#   "latestRelease" : {"tagName":"v0.8.0"}          <- mirrors the REPO's latest release
#   "releases"      : [{"tagName":"v0.7.1"}, ...]   <- what is actually PUBLISHED here
#
# The first version of this check read `latestRelease` and compared it against
# the repo's latest release. Those are the same value by construction, so it
# compared a value against itself and passed unconditionally — green every week
# while the listing sat a release behind, and verified against a listing known to
# be stale without noticing.
#
# Lives here rather than inline because two workflows need it. Inline in both, the
# careful part above is the part that gets copied once and then updated once.
#
# Usage: scripts/marketplace_published.sh
# Env:   MARKETPLACE_LISTING — override the listing URL (defaults to ours)
# Exits: 0 with one version per line on stdout; 1 if the listing cannot be read.
set -euo pipefail

LISTING="${MARKETPLACE_LISTING:-https://github.com/marketplace/actions/dbt-costgate}"

# `[^]]*` is safe because the array holds flat objects with no nested brackets.
# Pull the value out of `tagName` specifically: a bare version regex also matches
# the version inside each release's *name*, so a release tagged v0.7.1 but titled
# "... v0.8.0" would satisfy a membership test while publishing neither.
published="$(curl -sL --max-time 30 "$LISTING" |
  grep -oE '"releases":\[[^]]*\]' |
  head -1 |
  grep -oE '"tagName":"[^"]+"' |
  sed -E 's/.*:"([^"]+)"/\1/' || true)"

if [ -z "$published" ]; then
  echo "Could not read any published version from $LISTING." >&2
  echo "The page structure has probably changed, so this can no longer tell a" >&2
  echo "stale listing from a current one. Failing loudly rather than passing" >&2
  echo "silently — update the extraction in this script." >&2
  exit 1
fi

printf '%s\n' "$published"
