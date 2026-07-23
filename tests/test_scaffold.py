# SPDX-License-Identifier: Apache-2.0
"""Scaffold smoke tests: the package imports and the CLI stub answers."""

import pytest

from costgate import __version__
from costgate.cli import main


def test_version_is_three_part_semver():
    major, minor, patch = __version__.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


def test_bare_invocation_prints_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "cost gate" in capsys.readouterr().out


def test_check_is_declared_but_not_implemented():
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    assert excinfo.value.code == 2
