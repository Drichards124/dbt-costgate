# SPDX-License-Identifier: Apache-2.0
"""--against: auto-compile a base ref as the diff baseline in an isolated
worktree. dbt itself is never invoked — compile_fn is faked, so these stay
hermetic (no warehouse, no creds), the same way DryRunner is faked elsewhere."""

import subprocess
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from costgate import against
from costgate.against import AgainstError
from costgate.cli import main
from costgate.models import TIB


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "dbt_project.yml").write_text("name: t\n", "utf-8")
    (repo / "models").mkdir()
    (repo / "models" / "m.sql").write_text("select 1", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")


def _worktree_count(repo: Path) -> int:
    # `git worktree list` always includes the main worktree; extras are leaks.
    return len([ln for ln in _git(repo, "worktree", "list").splitlines() if ln.strip()])


def _fake_compiler(manifest: dict):
    """Return a compile_fn that writes `manifest` into <worktree>/target."""

    def compile_fn(worktree: Path) -> None:
        write_target(worktree, manifest)

    return compile_fn


def test_worktree_removed_on_success(tmp_path: Path):
    _init_repo(tmp_path)
    baseline = make_manifest(make_node("m", compiled_code="BASE_m", checksum="old"))
    nodes = against.compiled_baseline("main", tmp_path, compile_fn=_fake_compiler(baseline))
    assert "model.pkg.m" in nodes
    assert nodes["model.pkg.m"].compiled_code == "BASE_m"
    assert _worktree_count(tmp_path) == 1


def test_worktree_removed_on_compile_failure(tmp_path: Path):
    _init_repo(tmp_path)

    def boom(_worktree: Path) -> None:
        raise AgainstError("dbt compile failed")

    with pytest.raises(AgainstError, match="dbt compile failed"):
        against.compiled_baseline("main", tmp_path, compile_fn=boom)
    assert _worktree_count(tmp_path) == 1


def test_packages_symlinked_into_worktree(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "dbt_packages").mkdir()
    (tmp_path / "dbt_packages" / "dbt_utils").mkdir()
    seen: dict[str, bool] = {}

    def compile_fn(worktree: Path) -> None:
        seen["linked"] = (worktree / "dbt_packages" / "dbt_utils").is_dir()
        write_target(worktree, make_manifest(make_node("m", compiled_code="BASE_m")))

    against.compiled_baseline("main", tmp_path, compile_fn=compile_fn)
    assert seen["linked"] is True


def test_bad_ref_raises_and_leaves_no_worktree(tmp_path: Path):
    _init_repo(tmp_path)
    with pytest.raises(AgainstError, match="couldn't resolve ref"):
        against.compiled_baseline("does-not-exist", tmp_path, compile_fn=_fake_compiler({}))
    assert _worktree_count(tmp_path) == 1


def test_empty_compile_output_is_actionable(tmp_path: Path):
    _init_repo(tmp_path)

    def compile_nothing(_worktree: Path) -> None:
        return  # produces no target/manifest.json

    with pytest.raises(AgainstError, match="no usable manifest"):
        against.compiled_baseline("main", tmp_path, compile_fn=compile_nothing)
    assert _worktree_count(tmp_path) == 1


def test_resolve_dbt_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(against.shutil, "which", lambda _name: None)
    monkeypatch.setattr(against.sys, "executable", str(tmp_path / "bin" / "python"))
    with pytest.raises(AgainstError, match="dbt executable not found"):
        against._resolve_dbt()


# --- CLI wiring -------------------------------------------------------------


def test_against_and_baseline_mutually_exclusive(tmp_path: Path, capsys):
    target = write_target(tmp_path, make_manifest(make_node("m", compiled_code="CUR_m")))
    code = main(
        ["check", "--current", str(target), "--against", "main", "--baseline", "b.json"],
        runner=FakeDryRunner({}),
    )
    assert code == 2
    assert "not both" in capsys.readouterr().err


def test_against_preflight_requires_compiled_current(tmp_path: Path, capsys):
    uid, node = make_node("m")
    node["compiled_code"] = None  # parse-only current target
    target = write_target(tmp_path, make_manifest((uid, node)))
    code = main(
        ["check", "--current", str(target), "--against", "main"], runner=FakeDryRunner({})
    )
    assert code == 2
    assert "dbt compile" in capsys.readouterr().err


def test_against_end_to_end_diff_mode(tmp_path: Path, monkeypatch, capsys):
    _init_repo(tmp_path)
    # Current side: the user compiled their branch (checksum differs from baseline).
    write_target(tmp_path, make_manifest(make_node("m", compiled_code="CUR_m", checksum="new")))
    # Baseline side: what `dbt compile` on `main` would produce, faked.
    baseline = make_manifest(make_node("m", compiled_code="BASE_m", checksum="old"))
    monkeypatch.setattr(against, "_dbt_compile", _fake_compiler(baseline))

    runner = FakeDryRunner({"CUR_m": 3 * TIB, "BASE_m": TIB})
    code = main(
        ["check", "--current", str(tmp_path / "target"), "--against", "main"], runner=runner
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "m" in out
    assert _worktree_count(tmp_path) == 1
