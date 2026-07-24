# SPDX-License-Identifier: Apache-2.0
"""Auto-compile a base ref as the diff baseline, in an isolated git worktree.

The opt-in local convenience carved out by ADR-0006: unlike the pure core (which
only consumes compiled artifacts) and gitdiff.py (git-only), this edge needs
*both* git and dbt. It checks ``<ref>`` out into a throwaway worktree, runs
``dbt compile`` there, and hands the resulting manifest back as the baseline — so
a local before/after diff is one command with no manual ``--baseline``.

Assumes the dbt project sits at the git repo root (the common case, and where the
user runs costgate). Every failure degrades to an ``AgainstError`` with an
actionable message; the worktree is always removed, even on failure or Ctrl+C.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from costgate import artifacts
from costgate.models import ModelNode

# Third-party packages live here and are gitignored, so a fresh worktree checkout
# omits them. dbt_modules is the pre-1.0 name; both are handled for old projects.
_PACKAGE_DIRS = ("dbt_packages", "dbt_modules")


class AgainstError(Exception):
    """Producing the baseline via a worktree compile was not possible."""


def _git(project_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=project_dir, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AgainstError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc


def _resolve_dbt() -> str:
    """Find dbt without trusting a bare name on $PATH (shell aliases are invisible
    to subprocess; venvs may not export bin/ to child shells)."""
    found = shutil.which("dbt")
    if found:
        return found
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.is_file():
        return str(candidate)
    raise AgainstError(
        "dbt executable not found on PATH or active virtual environment. "
        "Install dbt, or use --baseline with a pre-compiled manifest."
    )


def _dbt_compile(worktree_dir: Path) -> None:
    """Default compiler: run `dbt compile` in the worktree. Injectable so tests
    exercise the whole flow without dbt, a warehouse, or credentials."""
    proc = subprocess.run(
        [_resolve_dbt(), "compile"], cwd=worktree_dir, capture_output=True, text=True
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:])
        raise AgainstError("dbt compile failed for the baseline ref:\n" + tail)


def _link_packages(src_dir: Path, worktree_dir: Path) -> None:
    """Make host-installed dbt packages visible to the worktree compile.

    A fresh checkout omits gitignored dbt_packages/, so a project using dbt_utils
    et al. would crash at compile on the first package macro. Symlink the host's
    copy in (fast, offline); copy where symlinks aren't available. These are the
    *current branch's* installed versions — good enough for a cost baseline.
    """
    for name in _PACKAGE_DIRS:
        src = (src_dir / name).resolve()
        if not src.is_dir():
            continue
        dest = worktree_dir / name
        if dest.exists() or dest.is_symlink():
            continue
        try:
            dest.symlink_to(src, target_is_directory=True)
        except OSError:
            shutil.copytree(src, dest)


def compiled_baseline(
    ref: str,
    project_dir: Path,
    *,
    compile_fn: Callable[[Path], None] | None = None,
) -> dict[str, ModelNode]:
    """Compile ``ref`` in a throwaway worktree; return its model nodes as the
    baseline. Raises AgainstError (never a stack trace) on any failure."""
    # Resolved at call time (not bound as a default) so the real compiler can be
    # monkeypatched in tests that drive the full worktree flow through the CLI.
    if compile_fn is None:
        compile_fn = _dbt_compile
    project_dir = project_dir.resolve()

    # Self-heal any worktree orphaned by a prior SIGKILL/power-loss — the one case
    # the finally-block below genuinely can't cover.
    _git(project_dir, "worktree", "prune", check=False)

    verify = _git(
        project_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
    )
    if verify.returncode != 0:
        raise AgainstError(
            f"couldn't resolve ref {ref!r} to compile as the baseline. Use an "
            f"existing branch/tag/SHA, or --baseline with a pre-compiled manifest."
        )

    # mkdtemp creates the parent; the worktree path itself must not exist yet, as
    # `git worktree add` creates it.
    tmp_parent = Path(tempfile.mkdtemp(prefix="costgate-against-"))
    worktree = tmp_parent / "wt"
    try:
        _git(project_dir, "worktree", "add", "--detach", "--quiet", str(worktree), ref)
        _link_packages(project_dir, worktree)
        compile_fn(worktree)
        manifest = artifacts.load_manifest(worktree / "target")
        return artifacts.model_nodes(manifest)
    except artifacts.ArtifactError as exc:
        raise AgainstError(f"the baseline compile produced no usable manifest: {exc}") from exc
    finally:
        _git(project_dir, "worktree", "remove", "--force", str(worktree), check=False)
        _git(project_dir, "worktree", "prune", check=False)
        shutil.rmtree(tmp_parent, ignore_errors=True)
