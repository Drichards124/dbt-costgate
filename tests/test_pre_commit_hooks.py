# SPDX-License-Identifier: Apache-2.0
"""The pre-commit hook definition must stay wired to the CLI it claims to run.

`.pre-commit-hooks.yaml` is consumed by other people's repositories, never by
this one, so nothing here exercises it in the normal course of work. Rename the
console script or the subcommand and the hook keeps loading happily — it fails
in a stranger's push, at the moment they can least afford it. These tests hold
the two ends together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from dbt_costgate.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
HOOKS = yaml.safe_load((ROOT / ".pre-commit-hooks.yaml").read_text("utf-8"))


def _console_scripts() -> set[str]:
    """The [project.scripts] names, read from pyproject.

    Parsed with a regex rather than tomllib: this project supports Python 3.9,
    which has no stdlib TOML reader.
    """
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    section = re.search(r"^\[project\.scripts\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert section, "pyproject has no [project.scripts] section"
    return set(re.findall(r"^([\w.-]+)\s*=", section.group(1), re.M))


@pytest.mark.parametrize("hook", HOOKS, ids=lambda h: h["id"])
def test_the_hook_runs_a_console_script_this_package_installs(hook: dict):
    executable = hook["entry"].split()[0]
    assert executable in _console_scripts(), (
        f"hook {hook['id']} runs `{executable}`, which is not one of the console "
        f"scripts this package installs ({sorted(_console_scripts())})"
    )


@pytest.mark.parametrize("hook", HOOKS, ids=lambda h: h["id"])
def test_the_hook_runs_a_subcommand_the_cli_accepts(hook: dict):
    args = hook["entry"].split()[1:]
    parsed = build_parser().parse_args(args)  # SystemExit here means the hook is broken
    assert parsed.command == args[0]


@pytest.mark.parametrize("hook", HOOKS, ids=lambda h: h["id"])
def test_the_hook_stays_on_pre_push(hook: dict):
    """Moving this to `pre-commit` would run a compile-dependent, network-bound
    check on every commit — the reliable way to get a hook disabled."""
    assert hook["stages"] == ["pre-push"]


@pytest.mark.parametrize("hook", HOOKS, ids=lambda h: h["id"])
def test_the_hook_passes_no_filenames_because_the_cli_takes_none(hook: dict):
    assert hook["pass_filenames"] is False
    with pytest.raises(SystemExit):
        build_parser().parse_args(["check", "models/some_model.sql"])
