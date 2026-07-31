# SPDX-License-Identifier: Apache-2.0
"""What the published package says about itself.

PyPI renders the trove classifiers on the listing page, above the README. For
anyone who finds this through `pip search`, a dependency scanner, or the package
page rather than through GitHub, they are the first — sometimes the only —
description of the project they read.

Nothing in the build or the suite checked them, and they went stale exactly the
way an unchecked claim does: `Development Status :: 2 - Pre-Alpha` shipped
through every 1.x release, so five releases' worth of visitors were told that a
feature-complete, live-verified cost gate was pre-alpha software. Nothing failed,
no test went red, and the only way to notice was for a person to look at the
listing.

The two claims below are the ones that rot: a maturity level nobody revisits
after the first commit, and a supported-Python range that has to keep step with
the versions CI actually runs. Both are derived or bounded here rather than
restated, so the next drift fails a test instead of reaching PyPI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dbt_costgate import __version__

ROOT = Path(__file__).resolve().parents[1]
# Parsed with a regex rather than tomllib, for the reason test_pre_commit_hooks
# gives for doing the same: this project supports Python 3.9, and tomllib only
# arrived in 3.11.
PYPROJECT = (ROOT / "pyproject.toml").read_text("utf-8")
CI_WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")


def _classifiers() -> list[str]:
    block = re.search(r"classifiers\s*=\s*\[(.*?)\n\]", PYPROJECT, re.DOTALL)
    assert block, "pyproject.toml has no classifiers list to check"
    return re.findall(r'"([^"]+)"', block.group(1))


def _classifier_value(prefix: str) -> str:
    """The single classifier under `prefix`, asserting there is exactly one.

    Two Development Status lines is not a hypothetical: it is what you get by
    adding the new one and forgetting to delete the old, and PyPI displays both
    without complaint.
    """
    found = [c[len(prefix) :] for c in _classifiers() if c.startswith(prefix)]
    assert len(found) == 1, f"expected exactly one `{prefix}` classifier, found {found}"
    return found[0]


def _ci_pythons() -> list[str]:
    """The Python versions CI actually runs the suite on."""
    matrix = re.search(r"python:\s*\[([^\]]+)\]", CI_WORKFLOW)
    assert matrix, "could not find the Python matrix in ci.yml"
    versions = re.findall(r'"([\d.]+)"', matrix.group(1))
    assert versions, "the CI Python matrix parsed as empty"
    return versions


# Trove's Development Status values, lowest to highest.
_TROVE_STATUS = (
    "1 - Planning",
    "2 - Pre-Alpha",
    "3 - Alpha",
    "4 - Beta",
    "5 - Production/Stable",
    "6 - Mature",
    "7 - Inactive",
)
# What a released 1.x may not claim about itself. Anything below Beta says the
# software is not yet usable, which shipping a 1.x under semver contradicts
# outright — and this is the exact claim that went stale.
_PRE_RELEASE_STATUS = frozenset(_TROVE_STATUS[:3])

_STATUS_PREFIX = "Development Status :: "
_PYTHON_PREFIX = "Programming Language :: Python :: "


def test_the_development_status_is_a_real_trove_value():
    """A misspelled classifier is not rejected by the build — PyPI drops it, and
    the listing simply shows no status at all."""
    assert _classifier_value(_STATUS_PREFIX) in _TROVE_STATUS


def test_a_released_major_version_does_not_call_itself_pre_release():
    """The guard for the defect this file was written for.

    Deliberately a floor rather than an exact value: whether this project is Beta
    or Production/Stable is a judgement its maintainer makes, and pinning the
    exact string would make every honest promotion a test failure. Claiming to be
    pre-alpha while shipping a 1.x is not a judgement, it is a contradiction.
    """
    major = int(__version__.split(".")[0])
    if major < 1:
        pytest.skip("pre-1.0 releases may legitimately claim any early status")
    status = _classifier_value(_STATUS_PREFIX)
    assert status not in _PRE_RELEASE_STATUS, (
        f"version {__version__} ships as a released major version, but PyPI is "
        f"told `{_STATUS_PREFIX}{status}`. That is the first thing shown on the "
        f"listing page, above the README."
    )


def test_every_python_ci_runs_is_declared_to_pypi():
    """Derived from the CI matrix rather than listed again here.

    A hand-kept copy is how the range goes stale: support for a new Python is
    added to the matrix, the suite goes green on it, and the classifier list —
    which is what PyPI's version filter reads — never hears about it.
    """
    declared = {c for c in _classifiers() if c.startswith(_PYTHON_PREFIX)}
    missing = [
        f"{_PYTHON_PREFIX}{version}"
        for version in _ci_pythons()
        if f"{_PYTHON_PREFIX}{version}" not in declared
    ]
    assert not missing, "CI runs these, PyPI is not told about them: " + ", ".join(missing)


def test_no_python_is_declared_that_ci_never_runs():
    """The other direction, which matters more: a version listed here is a
    promise to anyone filtering PyPI by it, and nothing is testing that promise.
    `:: 3` is the bare family marker and is not a version claim."""
    tested = set(_ci_pythons())
    overclaimed = [
        c
        for c in _classifiers()
        if c.startswith(_PYTHON_PREFIX) and c[len(_PYTHON_PREFIX) :] not in tested | {"3"}
    ]
    assert not overclaimed, "declared to PyPI but never run in CI: " + ", ".join(overclaimed)


def test_requires_python_agrees_with_the_lowest_version_ci_runs():
    floor = re.search(r'requires-python\s*=\s*">=([\d.]+)"', PYPROJECT)
    assert floor, "pyproject.toml has no `requires-python` lower bound"
    lowest = min(_ci_pythons(), key=lambda v: tuple(int(p) for p in v.split(".")))
    assert floor.group(1) == lowest, (
        f"requires-python says >={floor.group(1)} but the oldest Python CI runs is {lowest}"
    )
