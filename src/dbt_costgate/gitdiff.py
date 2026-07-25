# SPDX-License-Identifier: Apache-2.0
"""Lightweight local change detection via `git diff`.

The one git-touching edge (no dbt here). Used only for the zero-setup local
default: report which files the working branch changed, leaving the mapping from
paths to models to `artifacts`. Degrades cleanly — a shallow clone, a missing
ref, or a non-git directory returns a reason, never a stack trace.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitDiffError(Exception):
    """Selection via git was not possible; message explains the fallback."""


@dataclass
class _Git:
    project_dir: Path

    def run(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitDiffError(proc.stderr.strip() or f"git {' '.join(args)} failed")
        return proc.stdout


def _resolve_base(git: _Git, base: str | None) -> str:
    """Find a usable base ref, trying the caller's choice then common defaults."""
    candidates = [base] if base else ["origin/main", "main", "origin/master", "master"]
    for ref in candidates:
        try:
            git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            return ref
        except GitDiffError:
            continue
    raise GitDiffError(
        f"couldn't resolve a base ref ({', '.join(c for c in candidates if c)}). "
        f"Shallow clone or missing branch — use --select or --baseline instead."
    )


def changed_paths(project_dir: Path, base: str | None = None) -> list[str]:
    """Paths changed between the merge-base of `base` and the working tree
    (committed + uncommitted), relative to `project_dir` — the same basis a dbt
    manifest uses, so they can be matched against it directly."""
    git = _Git(project_dir)
    if not (project_dir / ".git").exists():
        # Could still be a subdir of a repo; let git decide, but give a clear error.
        try:
            git.run("rev-parse", "--is-inside-work-tree")
        except GitDiffError as exc:
            raise GitDiffError(
                "not a git repository — use --select or --baseline instead."
            ) from exc
    ref = _resolve_base(git, base)
    try:
        merge_base = git.run("merge-base", "HEAD", ref).strip()
    except GitDiffError:
        merge_base = ref
    # --relative reports paths relative to project_dir rather than the repo root,
    # which is the basis a manifest's original_file_path uses. Without it, a dbt
    # project in a subdirectory matches nothing at all. It also drops changes
    # outside the project, which belong to a sibling project, not this one.
    out = git.run("diff", "--name-only", "--relative", merge_base)
    return [line.strip() for line in out.splitlines() if line.strip()]
