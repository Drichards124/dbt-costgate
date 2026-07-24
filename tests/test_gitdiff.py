# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from conftest import git as _git
from conftest import init_repo as _init_repo
from conftest import make_manifest, make_node
from costgate import artifacts, gitdiff
from costgate.gitdiff import GitDiffError


def test_changed_paths_map_to_the_changed_model(tmp_path: Path):
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
    paths = gitdiff.changed_paths(repo, "main")
    changed = {nodes[uid].name for uid in artifacts.select_by_paths(nodes, paths)}
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
