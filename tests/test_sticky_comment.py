# SPDX-License-Identifier: Apache-2.0
"""Offline tests for scripts/sticky_comment.sh.

A fake `gh` on PATH stands in for the real CLI: for the list call it returns a
canned set of comment ids; for POST/PATCH it records the method, URL, and the
body it was handed. No network, no GitHub token — just the upsert branching.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# sticky_comment.sh is a bash script that only ever runs on the action's Linux
# runner (action.yml uses `shell: bash`). It is not meaningful on Windows, whose
# bash lacks the POSIX PATH these tests build.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="sticky_comment.sh targets POSIX CI runners; bash unavailable here",
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sticky_comment.sh"
MARKER = "<!-- costgate-sticky -->"

# A fake `gh`: the list call (recognised by `--jq`) prints $FAKE_GH_IDS; a
# POST/PATCH records one tab-separated line per call to $FAKE_GH_LOG.
FAKE_GH = r"""#!/usr/bin/env bash
method=""; url=""; body=""; is_list=0
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    --jq) is_list=1 ;;
    --method) i=$((i+1)); method="${args[$i]}" ;;
    -F) i=$((i+1)); f="${args[$i]}"; case "$f" in body=@*) body="${f#body=@}" ;; esac ;;
    repos/*) url="$a" ;;
  esac
  i=$((i+1))
done
if [ "$is_list" = "1" ]; then
  [ -n "$FAKE_GH_IDS" ] && printf '%s\n' "$FAKE_GH_IDS"
  exit 0
fi
len=0; marker=no
if [ -n "$body" ] && [ -f "$body" ]; then
  len=$(wc -c <"$body" | tr -d '[:space:]')
  grep -qF '<!-- costgate-sticky -->' "$body" && marker=yes
fi
printf '%s\t%s\tlen=%s\tmarker=%s\n' "$method" "$url" "$len" "$marker" >>"$FAKE_GH_LOG"
exit 0
"""


def run(tmp_path: Path, *, ids: str, body: str) -> list[str]:
    """Run the script with a fake gh returning `ids`; return logged POST/PATCH lines."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    log = tmp_path / "gh.log"
    log.write_text("")
    body_file = tmp_path / "body.md"
    body_file.write_text(body)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_REPOSITORY": "acme/warehouse",
        "GH_TOKEN": "x",
        "FAKE_GH_IDS": ids,
        "FAKE_GH_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "42", MARKER, str(body_file)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in log.read_text().splitlines() if ln]


def test_posts_when_no_existing_comment(tmp_path: Path):
    lines = run(tmp_path, ids="", body="### costgate\ncost table")
    assert len(lines) == 1
    method, url, *_rest = lines[0].split("\t")
    assert method == "POST"
    assert url == "repos/acme/warehouse/issues/42/comments"
    # The script prepends the marker so a later run can find this comment.
    assert "marker=yes" in lines[0]


def test_patches_when_marker_present(tmp_path: Path):
    lines = run(tmp_path, ids="456", body="### costgate\ncost table")
    assert len(lines) == 1
    method, url, *_rest = lines[0].split("\t")
    assert method == "PATCH"
    assert url == "repos/acme/warehouse/issues/comments/456"


def test_takes_first_id_when_paginated_match_is_multiline(tmp_path: Path):
    # --paginate evaluates --jq per page, so a page-2 match (or a duplicate) can
    # yield several ids. The script must PATCH exactly the first, never twice.
    lines = run(tmp_path, ids="456\n789", body="### costgate\ncost table")
    assert len(lines) == 1
    method, url, *_rest = lines[0].split("\t")
    assert method == "PATCH"
    assert url == "repos/acme/warehouse/issues/comments/456"


def test_truncates_oversized_body(tmp_path: Path):
    huge = "x" * 70_000
    lines = run(tmp_path, ids="", body=huge)
    assert len(lines) == 1
    length = int(next(p for p in lines[0].split("\t") if p.startswith("len=")).removeprefix("len="))
    assert length <= 65536
    assert "marker=yes" in lines[0]
