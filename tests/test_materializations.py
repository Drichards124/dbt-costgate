# SPDX-License-Identifier: Apache-2.0
"""One test per dbt materialization, including the ones the engine never names.

`table`, `materialized_view` and custom materializations had no coverage at all:
`artifacts.model_nodes` defaults anything it doesn't recognise to `"view"`, so a
new materialization is handled by falling through rather than by decision, and
nothing failed when that was wrong. These tests pin what each type does today.

The tests below the divider were written against defects the QA pass found, so
each asserts the behaviour we wanted rather than the behaviour we had. They
carried `xfail(strict=True)` until the fixes landed and turned them red.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate.artifacts import detect_basis, model_nodes
from dbt_costgate.models import TIB, EstimateBasis

# Every materialization dbt ships, plus one a user invented. The value is what
# `ModelNode.materialized` must end up as.
SQL_MATERIALIZATIONS = [
    "table",
    "view",
    "incremental",
    "materialized_view",
    "snapshot",
    "my_custom_materialization",
]


def _check(target: Path, *args, runner=None):
    from dbt_costgate.cli import main

    return main(["check", "--current", str(target), *args], runner=runner)


@pytest.mark.parametrize("materialized", SQL_MATERIALIZATIONS)
def test_every_non_ephemeral_materialization_is_a_cost_bearing_node(materialized):
    manifest = make_manifest(make_node("m", materialized=materialized))
    nodes = model_nodes(manifest)
    assert list(nodes) == ["model.pkg.m"]
    assert nodes["model.pkg.m"].materialized == materialized


def test_ephemeral_is_the_only_materialization_dropped():
    manifest = make_manifest(
        make_node("keep", materialized="table"),
        make_node("drop", materialized="ephemeral"),
    )
    assert list(model_nodes(manifest)) == ["model.pkg.keep"]


def test_a_node_with_no_config_key_at_all_defaults_to_view():
    # dbt always writes `config`, but a hand-built or trimmed manifest may not,
    # and `model_nodes` reads it with `.get(...) or {}`.
    uid, node = make_node("m")
    del node["config"]
    nodes = model_nodes({"nodes": {uid: node}, "macros": {}})
    assert nodes[uid].materialized == "view"
    assert nodes[uid].is_incremental is False


@pytest.mark.parametrize("materialized", ["table", "view", "materialized_view", "custom"])
def test_non_incremental_materializations_get_the_direct_basis(materialized):
    node = model_nodes(make_manifest(make_node("m", materialized=materialized)))["model.pkg.m"]
    assert detect_basis(node, "select 1") is EstimateBasis.DIRECT


def test_incremental_basis_follows_the_compiled_shape_not_the_materialization():
    node = model_nodes(make_manifest(make_node("m", materialized="incremental")))["model.pkg.m"]
    fresh = "select * from src"
    against_the_table = f"{fresh} where ts > (select max(ts) from {node.relation_name})"
    assert detect_basis(node, fresh) is EstimateBasis.FULL_REFRESH
    assert detect_basis(node, against_the_table) is EstimateBasis.INCREMENTAL_FORM


def test_python_models_are_dropped_whatever_their_materialization():
    manifest = make_manifest(
        make_node("py", language="python", materialized="table"),
        make_node("sql", materialized="table"),
    )
    assert list(model_nodes(manifest)) == ["model.pkg.sql"]


def test_seed_and_snapshot_resource_types_are_dropped():
    manifest = make_manifest(
        make_node("s", resource_type="seed"),
        make_node("snap", resource_type="snapshot"),
        make_node("m"),
    )
    assert list(model_nodes(manifest)) == ["model.pkg.m"]


def test_a_materialized_view_is_priced_like_a_view_but_says_so(tmp_path: Path, capsys):
    """The figure really is a view's figure — one build, one scan. What changed is
    that the row now says the recurring cost is higher, which is what a reader of
    a materialized-view row is actually asking about."""
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("mv", materialized="materialized_view", compiled_code="MV"),
            make_node("v", materialized="view", compiled_code="V"),
        ),
    )
    code = _check(
        target,
        "--select",
        "mv,v",
        "--format",
        "json",
        runner=FakeDryRunner({"MV": TIB, "V": TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    by_name = {m["name"]: m for m in payload["models"]}
    assert by_name["mv"]["basis"] == by_name["v"]["basis"] == "direct"
    assert by_name["mv"]["usd_current"] == by_name["v"]["usd_current"]
    assert by_name["v"]["warnings"] == []
    assert "each automatic refresh" in " ".join(by_name["mv"]["warnings"])


# --------------------------------------------------------------------------
# Confirmed defects, asserted as the behaviour we want.
# --------------------------------------------------------------------------


def test_a_materialized_view_says_its_figure_is_not_the_recurring_cost(tmp_path: Path, capsys):
    target = write_target(
        tmp_path,
        make_manifest(make_node("mv", materialized="materialized_view", compiled_code="MV")),
    )
    _check(target, "--select", "mv", "--format", "json", runner=FakeDryRunner({"MV": TIB}))
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"][0]["warnings"], "a materialized view needs a refresh-cost caveat"


def test_selecting_an_ephemeral_model_says_why_it_cannot_be_priced(tmp_path: Path, capsys):
    """BUG-F02. Naming a model that exists and getting silence back — or worse,
    "no such model" — reads as a bug in the tool. It exists; it just has no
    relation of its own to scan.

    This used to also assert exit 2 and produce no report. Running against a
    realistic project showed what that costs: `keep` has a real answer, and
    `--select` from `dbt ls --resource-type model` includes ephemerals, so an
    ordinary CI line threw away every figure it had. The explanation this test
    exists for is unchanged and is what still matters; `keep` is now reported
    beside it. Naming *only* unpriced nodes is still exit 2 — see
    tests/test_visibility.py.
    """
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("eph", materialized="ephemeral", compiled_code="E"),
            make_node("keep", compiled_code="K"),
        ),
    )
    code = _check(target, "--select", "eph,keep", runner=FakeDryRunner({"K": TIB}))
    out, err = capsys.readouterr()
    assert code == 0
    assert "keep" in out
    assert "eph" in err
    assert "ephemeral models have no relation of their own" in err
    assert "no such model" not in err
