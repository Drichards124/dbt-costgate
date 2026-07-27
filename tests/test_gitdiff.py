# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from conftest import git as _git
from conftest import init_repo as _init_repo
from conftest import make_manifest, make_node
from dbt_costgate import artifacts, gitdiff
from dbt_costgate.gitdiff import GitDiffError


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
    changed = {nodes[uid].name for uid in artifacts.select_by_paths(nodes, paths)[0]}
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


def test_changed_paths_are_project_relative_in_a_subdir_project(tmp_path: Path):
    # git reports paths from the repo root; a manifest's original_file_path is
    # relative to the dbt project. In a monorepo those differ, and an unadjusted
    # match selects nothing at all — a silent pass, not an error.
    repo = tmp_path
    _init_repo(repo)
    project = repo / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: p\n", "utf-8")
    (project / "models" / "a.sql").write_text("select 1", "utf-8")
    (repo / "unrelated.txt").write_text("x", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (project / "models" / "a.sql").write_text("select 2", "utf-8")
    (repo / "unrelated.txt").write_text("y", "utf-8")

    paths = gitdiff.changed_paths(project, "main")
    assert paths == ["models/a.sql"]  # not "analytics/models/a.sql"; sibling file excluded

    nodes = artifacts.model_nodes(make_manifest(make_node("a", original_file_path="models/a.sql")))
    assert artifacts.select_by_paths(nodes, paths)[0] == ["model.pkg.a"]


def test_changed_paths_unaffected_when_the_project_is_the_repo_root(tmp_path: Path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "models").mkdir()
    (repo / "models" / "a.sql").write_text("select 1", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / "models" / "a.sql").write_text("select 2", "utf-8")
    assert gitdiff.changed_paths(repo, "main") == ["models/a.sql"]
