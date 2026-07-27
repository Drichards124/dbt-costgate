# SPDX-License-Identifier: Apache-2.0
"""The security ruleset has to stay armed.

`S` (flake8-bandit) is noisy on this codebase — every `subprocess` call trips
S603 — so it ships with suppressions. Suppressions are how a security linter
quietly stops linting: widen one far enough and the ruleset is decorative while
still looking enabled. These tests pin the parts that must never be silenced.

S602 is the one that matters. S603 flags *every* subprocess call whether or not
it is dangerous; S602 fires only on `shell=True`, which is the thing that
actually creates an injection vector here.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text("utf-8")
# Configuration only. Comments in pyproject.toml discuss these rule codes by
# name, and a check that cannot tell prose from settings would fire on its own
# documentation.
PYPROJECT_SETTINGS = "\n".join(
    ln for ln in PYPROJECT.splitlines() if not ln.lstrip().startswith("#")
)

# Rules that may never be suppressed, anywhere, by any mechanism.
NEVER_SUPPRESSED = ("S602",)


def test_the_bandit_ruleset_is_enabled():
    select = next(ln for ln in PYPROJECT.splitlines() if ln.startswith("select = "))
    assert '"S"' in select, f"flake8-bandit is no longer selected: {select}"


def test_the_rules_that_matter_are_not_ignored_in_config():
    # Covers `ignore`, `per-file-ignores`, and any future list: the code simply
    # must not appear in the lint configuration at all.
    for code in NEVER_SUPPRESSED:
        assert code not in PYPROJECT_SETTINGS, (
            f"{code} appears in pyproject.toml. It flags `shell=True`, which is the "
            f"injection vector this ruleset exists to catch — it must never be ignored."
        )


def test_the_rules_that_matter_are_not_suppressed_at_a_call_site():
    offenders = []
    # scripts/ is in scope because that is where the shelling-out lives: the
    # sample generator and the live-BigQuery harness both spawn subprocesses.
    searched = [(ROOT / d).rglob("*.py") for d in ("src", "tests", "scripts")]
    for path in [p for group in searched for p in group]:
        if path.name == Path(__file__).name:
            continue
        for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if "noqa" in line and any(code in line for code in NEVER_SUPPRESSED):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, "shell=True suppressed with a noqa at:\n  " + "\n  ".join(offenders)


def test_every_source_suppression_states_a_reason():
    # In src/ a suppression is a claim that the finding is wrong. An unexplained
    # one is indistinguishable from someone silencing a real finding to get green.
    unexplained = []
    for path in (ROOT / "src").rglob("*.py"):
        lines = path.read_text("utf-8").splitlines()
        for n, line in enumerate(lines, 1):
            if "# noqa: S" not in line:
                continue
            _, _, after = line.partition("# noqa:")
            inline_reason = "—" in after or "#" in after
            nearby = " ".join(lines[max(0, n - 4) : n - 1])
            if not inline_reason and "S60" not in nearby and "S10" not in nearby:
                unexplained.append(f"{path.relative_to(ROOT)}:{n}")
    assert not unexplained, "security suppression with no stated reason at:\n  " + "\n  ".join(
        unexplained
    )
