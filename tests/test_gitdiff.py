# SPDX-License-Identifier: Apache-2.0
import subprocess
from pathlib import Path

import pytest

from conftest import make_manifest, make_node
from costgate import artifacts, gitdiff
from costgate.gitdiff import GitDiffError


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path):
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def test_select_by_git_finds_changed_model(tmp_path: Path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "models").mkdir()
    (repo / "models" / "a.sql").write_text("select 1", "utf-8")
    (repo / "models" / "b.sql").write_text("select 1", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "feature")
    (repo / "models" / "a.sql").write_text("select 2", "utf-8")
    _git(repo, "commit", "-am", "change a")

    nodes = artifacts.model_nodes(
        make_manifest(
            make_node("a", original_file_path="models/a.sql"),
            make_node("b", original_file_path="models/b.sql"),
        )
    )
    changed = {nodes[uid].name for uid in gitdiff.select_by_git(nodes, repo, "main")}
    assert changed == {"a"}


def test_non_git_directory_raises_clean_error(tmp_path: Path):
    with pytest.raises(GitDiffError, match="not a git repository|base ref"):
        gitdiff.changed_paths(tmp_path, base=None)


def test_missing_ref_raises_actionable_error(tmp_path: Path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "f.txt").write_text("x", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    with pytest.raises(GitDiffError, match="couldn't resolve a base ref|--select"):
        gitdiff.changed_paths(repo, base="does-not-exist")
