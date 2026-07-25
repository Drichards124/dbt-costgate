# SPDX-License-Identifier: Apache-2.0
"""The documented example reports must match what the renderers actually produce.

Hand-written examples rot silently, and this project has been bitten: an example
priced 412.50 MiB at $2.51 — about a thousand times too high — and survived
several releases because nothing compared it to real output. These tests make
that impossible: change a renderer without regenerating, and the suite fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import samples  # noqa: E402  (needs the path insert above)


def test_generated_blocks_in_the_docs_are_current():
    """Runs the generator's own --check, so the test and the tool cannot disagree
    about what 'current' means."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_samples.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Documented examples are out of date with the renderers.\n"
        "Regenerate with: python scripts/gen_samples.py\n\n" + result.stdout + result.stderr
    )


@pytest.mark.parametrize("name", sorted(samples.SAMPLES))
def test_every_sample_renders_without_error(name: str):
    out = samples.render(name)
    assert out.strip(), f"{name} rendered empty"


@pytest.mark.parametrize("name", sorted(samples.SAMPLES))
def test_no_sample_claims_a_currency_symbol(name: str):
    # amounts carry ISO codes; a stray `$` would mean a renderer regressed
    assert "$" not in samples.render(name), name


def test_the_samples_cover_the_pricing_cases_a_reader_must_choose_between():
    """Default table rate, a negotiated rate, and slots. Each is a different
    answer to 'what will this cost me', and a reader picks one."""
    assert "USD 6.25/TiB" in samples.render("diff-terminal")  # built-in table
    assert "USD 4.10/TiB" in samples.render("negotiated-terminal")  # user override
    slots = samples.render("slots-terminal")
    assert "bytes only" in slots and "USD" not in slots


def test_a_cost_reduction_is_shown_as_a_saving():
    out = samples.render("saving-terminal")
    assert "Net saving:" in out
    assert "GATE: PASS" in out


def test_the_pr_comment_sample_is_markdown_not_a_picture_of_one():
    out = samples.render("pr-comment")
    assert out.lstrip().startswith("###")
    assert "| Model |" in out


def test_a_sample_never_claims_a_rate_its_amounts_contradict():
    """Caught a real bug while writing these: the negotiated sample advertised
    USD 4.10/TiB in its header while its amounts had been priced at 6.25. Two
    samples describing different rates must produce different amounts."""
    default = samples.render("diff-terminal")
    negotiated = samples.render("negotiated-terminal")
    assert "USD 6.25/TiB" in default and "USD 4.10/TiB" in negotiated

    def per_run(text: str) -> str:
        line = next(ln for ln in text.splitlines() if "fct_orders_daily" in ln and "/run" in ln)
        return line.split("/run")[0].split()[-1]

    assert per_run(default) != per_run(negotiated), (
        "the negotiated sample shows the same amount as the default one, so its "
        "deltas were priced at the wrong rate"
    )


# --- documentation links ----------------------------------------------------

_DOCS = ["README.md", "CONTRIBUTING.md", "SECURITY.md", "docs/usage.md", "docs/explained.md"]
_LINK = __import__("re").compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    text = heading.lstrip("#").strip().lower()
    kept = [c for c in text if c.isalnum() or c in " -_"]
    return "".join(kept).replace(" ", "-")


@pytest.mark.parametrize("doc", _DOCS)
def test_every_relative_link_in_the_docs_resolves(doc: str):
    """A broken link in the docs is invisible until a reader hits it, and this
    change added a lot of cross-references between pages."""
    path = ROOT / doc
    text = path.read_text("utf-8")
    anchors = {_slug(ln) for ln in text.splitlines() if ln.startswith("#")}
    broken: list[str] = []

    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        if not file_part:  # same-page anchor
            if anchor not in anchors:
                broken.append(f"{target} (no such heading in {doc})")
            continue
        resolved = (path.parent / file_part).resolve()
        if not resolved.exists():
            broken.append(f"{target} (no such file)")
            continue
        if anchor:
            other = {
                _slug(ln) for ln in resolved.read_text("utf-8").splitlines() if ln.startswith("#")
            }
            if anchor not in other:
                broken.append(f"{target} (no such heading in {file_part})")

    assert not broken, f"broken links in {doc}:\n  " + "\n  ".join(broken)


# --- documented action pins -------------------------------------------------
#
# The CI recipe in usage.md is copied verbatim by users, so a stale pin there
# hands them a version we deliberately moved off. This drifted once already:
# the repo's own workflows went to checkout@v7 / setup-python@v7 to clear Node
# deprecation warnings, and the documented recipe sat at v4/v5 telling readers
# to use exactly what we had just abandoned. Nothing compared the two.

_USES = __import__("re").compile(r"uses:\s*([^@\s]+)@(\S+)")


def _pins(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for action, ref in _USES.findall(text):
        out.setdefault(action, set()).add(ref)
    return out


def _workflow_pins() -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for action, refs in _pins(wf.read_text("utf-8")).items():
            merged.setdefault(action, set()).update(refs)
    return merged


def test_documented_action_pins_match_the_ones_we_run():
    ours = _workflow_pins()
    documented = _pins((ROOT / "docs" / "usage.md").read_text("utf-8"))
    mismatched = [
        f"docs/usage.md pins {action}@{sorted(refs)} but our workflows use "
        f"{action}@{sorted(ours[action])}"
        for action, refs in documented.items()
        if action in ours and refs != ours[action]
    ]
    assert not mismatched, "\n  ".join([""] + mismatched)


def test_third_party_pins_we_cannot_check_are_named_not_silently_skipped():
    """The recipe also pins actions this repo does not itself run — the keyless
    BigQuery auth step. There is no in-repo ground truth for those, so this test
    exists to make that explicit rather than let the gap read as coverage. If the
    set changes, someone has to look at the new one by hand.
    """
    ours = _workflow_pins()
    documented = _pins((ROOT / "docs" / "usage.md").read_text("utf-8"))
    unverifiable = {a for a in documented if a not in ours and not a.startswith("Drichards124/")}
    assert unverifiable == {"google-github-actions/auth"}, (
        f"unverifiable documented actions changed: {sorted(unverifiable)} — check each "
        f"against its upstream latest major by hand, then update this test"
    )


def test_the_documented_action_pin_matches_the_version_we_ship():
    """Release prep bumps `__version__` and this pin together. Asserting they
    agree turns a hand-remembered release step into one that fails loudly."""
    from dbt_costgate import __version__

    documented = _pins((ROOT / "docs" / "usage.md").read_text("utf-8"))
    assert documented["Drichards124/dbt-costgate"] == {f"v{__version__}"}, (
        f"usage.md pins the Action at {documented['Drichards124/dbt-costgate']} but the "
        f"package version is {__version__} — release prep missed a site"
    )
