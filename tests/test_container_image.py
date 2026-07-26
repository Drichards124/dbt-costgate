# SPDX-License-Identifier: Apache-2.0
"""The image's runtime must be one the test suite actually covers.

Building the image is not part of `ci.yml` — the container smoke test runs on
pushes to `ple`. So a pull request that changes the base image gets a full set of
green Python jobs that tested the *source* on runtimes the image does not use.
That is a check reading as coverage while measuring nothing, and a dependency
bot proposing a new base image is exactly the case it fails to catch.

This ties the two together: bump the base to a version the matrix does not test
and the suite fails, on the pull request, with the reason. Add that version to
the matrix and it passes on its own — the constraint retires itself rather than
becoming a rule someone has to remember.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

_BASE = re.compile(r"^FROM python:(\d+\.\d+)-", re.M)


def _dockerfile_pythons() -> set[str]:
    return set(_BASE.findall((ROOT / "Dockerfile").read_text("utf-8")))


def _ci_matrix_pythons() -> set[str]:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    return {str(v) for v in ci["jobs"]["python"]["strategy"]["matrix"]["python"]}


def test_the_dockerfile_declares_a_base_image():
    # A rename or a rewrite that this regex stops matching would make every
    # assertion below vacuously true.
    assert _dockerfile_pythons(), "no `FROM python:X.Y-` line found in the Dockerfile"


def test_the_image_runs_a_python_the_matrix_tests():
    untested = _dockerfile_pythons() - _ci_matrix_pythons()
    assert not untested, (
        f"the Dockerfile builds on Python {sorted(untested)}, which ci.yml does not "
        f"test (it runs {sorted(_ci_matrix_pythons())}). Add the version to the CI "
        f"matrix before shipping an image on it."
    )


def test_every_stage_of_the_dockerfile_uses_the_same_python():
    """The wheel is built in one stage and installed in another. Building on one
    version and running on another is how a wheel that cannot be imported gets
    shipped."""
    versions = _dockerfile_pythons()
    assert len(versions) == 1, f"mixed base images across stages: {sorted(versions)}"
