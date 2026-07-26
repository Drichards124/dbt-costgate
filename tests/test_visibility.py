# SPDX-License-Identifier: Apache-2.0
"""Changes the tool used to be blind to.

Four different shapes of the same failure: something real happened to the
project's cost and the report said nothing at all. A deleted model, an
ephemeral-only edit, a seed or snapshot branch, a manifest from the wrong
warehouse — each printed "No changed models to estimate" or a confident figure,
and each is indistinguishable from "nothing happened" unless the tool says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, git, init_repo, make_manifest, make_node, write_target
from dbt_costgate import artifacts
from dbt_costgate.cli import main
from dbt_costgate.models import TIB


def _baseline(tmp_path: Path, *specs) -> Path:
    path = tmp_path / "base.json"
    path.write_text(json.dumps(make_manifest(*specs)), "utf-8")
    return path


# --------------------------------------------------------------------------
# F18 — deletions.
# --------------------------------------------------------------------------


def test_a_deleted_model_is_a_priced_saving(tmp_path: Path, capsys):
    target = write_target(tmp_path, make_manifest(make_node("kept", compiled_code="KEPT")))
    baseline = _baseline(
        tmp_path,
        make_node("kept", compiled_code="KEPT"),
        make_node("dim_customers", compiled_code="DROPPED"),
    )
    code = main(
        ["check", "--current", str(target), "--baseline", str(baseline), "--format", "json"],
        runner=FakeDryRunner({"KEPT": TIB, "DROPPED": 4 * TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    dropped = next(m for m in payload["models"] if m["name"] == "dim_customers")
    assert code == 0
    assert dropped["is_deleted"] is True
    assert dropped["bytes_current"] == 0
    assert dropped["bytes_baseline"] == 4 * TIB
    assert dropped["usd_per_run_delta"] == pytest.approx(-4 * 6.25)
    # And it reaches the headline: removing a model is the most direct cost
    # reduction a change can make, and it used to report "Net change: none".
    assert payload["net"]["usd_per_run"] == pytest.approx(-4 * 6.25)


def test_a_deletion_is_never_gated(tmp_path: Path, capsys):
    """A removal cannot raise cost, so no threshold applies to it — and it must
    not become a "could not check" failure either."""
    target = write_target(tmp_path, make_manifest(make_node("kept", compiled_code="KEPT")))
    baseline = _baseline(
        tmp_path,
        make_node("kept", compiled_code="KEPT"),
        make_node("gone", compiled_code="GONE"),
    )
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--format",
            "json",
            "--max-usd-per-run",
            "0.01",
            "--max-tib-total",
            "0.001",
        ],
        runner=FakeDryRunner({"KEPT": 1, "GONE": 9 * TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    gone = next(m for m in payload["models"] if m["name"] == "gone")
    assert code == 0
    assert gone["gateable"] is False
    assert gone["skip_reason"] == "deleted"


def test_a_renamed_models_baseline_is_not_reported_as_a_deletion(tmp_path: Path, capsys):
    target = write_target(tmp_path, make_manifest(make_node("fct_new", compiled_code="CUR")))
    baseline = _baseline(tmp_path, make_node("fct_old", compiled_code="BASE"))
    (tmp_path / ".dbt-costgate.yml").write_text("renames:\n  fct_new: fct_old\n", "utf-8")
    main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
            "--format",
            "json",
        ],
        runner=FakeDryRunner({"CUR": 2 * TIB, "BASE": TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["models"]] == ["fct_new"]


# --------------------------------------------------------------------------
# F3 — an ephemeral-only change.
# --------------------------------------------------------------------------


def _ephemeral_project(tmp_path: Path) -> Path:
    """An ephemeral model inlined by one downstream model, as dbt writes it."""
    eph_uid, eph = make_node("int_order_items", materialized="ephemeral")
    eph["original_file_path"] = "models/intermediate/int_order_items.sql"
    fct_uid, fct = make_node("fct_orders_daily", compiled_code="SELECT ...")
    fct["original_file_path"] = "models/marts/fct_orders_daily.sql"
    fct["depends_on"] = {"nodes": [eph_uid], "macros": []}
    return write_target(tmp_path, {"nodes": {eph_uid: eph, fct_uid: fct}, "macros": {}})


def test_an_ephemeral_only_change_selects_the_models_that_inline_it(tmp_path: Path, capsys):
    """The path the pre-commit hook runs. The ephemeral's SQL ends up inside
    `fct_orders_daily`, so widening a filter there is a real cost change — and
    locally it used to select nothing at all."""
    init_repo(tmp_path)
    target = _ephemeral_project(tmp_path)
    eph_file = tmp_path / "models" / "intermediate" / "int_order_items.sql"
    eph_file.parent.mkdir(parents=True)
    eph_file.write_text("select 1\n", "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "init")
    git(tmp_path, "checkout", "-b", "widen")
    eph_file.write_text("select 1, 2\n", "utf-8")
    git(tmp_path, "commit", "-am", "widen the window")

    code = main(
        ["check", "--current", str(target), "--base", "main", "--format", "json"],
        runner=FakeDryRunner({"SELECT ...": TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [m["name"] for m in payload["models"]] == ["fct_orders_daily"]
    assert "an ephemeral model it inlines changed" in " ".join(payload["models"][0]["warnings"])


def test_the_closure_follows_a_chain_of_ephemerals():
    a_uid, a = make_node("int_a", materialized="ephemeral")
    a["original_file_path"] = "models/int_a.sql"
    b_uid, b = make_node("int_b", materialized="ephemeral")
    b["depends_on"] = {"nodes": [a_uid], "macros": []}
    m_uid, m = make_node("fct", compiled_code="SQL")
    m["depends_on"] = {"nodes": [b_uid], "macros": []}
    manifest = {"nodes": {a_uid: a, b_uid: b, m_uid: m}, "macros": {}}

    nodes = artifacts.model_nodes(manifest)
    selected, notes = artifacts.select_by_paths(
        nodes, ["models/int_a.sql"], None, artifacts.ephemeral_index(manifest)
    )
    assert selected == [m_uid]
    assert m_uid in notes


def test_an_unrelated_ephemeral_change_selects_nothing():
    a_uid, a = make_node("int_a", materialized="ephemeral")
    a["original_file_path"] = "models/int_a.sql"
    m_uid, m = make_node("fct", compiled_code="SQL")
    manifest = {"nodes": {a_uid: a, m_uid: m}, "macros": {}}
    nodes = artifacts.model_nodes(manifest)
    selected, _ = artifacts.select_by_paths(
        nodes, ["models/int_a.sql"], None, artifacts.ephemeral_index(manifest)
    )
    assert selected == []


# --------------------------------------------------------------------------
# F2 — nodes dropped by type.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"resource_type": "seed"}, "seeds are not priced"),
        ({"resource_type": "snapshot"}, "snapshots are not priced"),
        ({"language": "python"}, "Python models are not priced"),
        ({"materialized": "ephemeral"}, "ephemeral models have no relation of their own"),
    ],
)
def test_a_change_to_an_unpriced_node_says_so_rather_than_nothing(
    tmp_path: Path, capsys, spec, expected
):
    current = make_manifest(
        make_node("kept", compiled_code="KEPT", checksum="same"),
        make_node("other", checksum="new", **spec),
    )
    baseline = _baseline(
        tmp_path,
        make_node("kept", compiled_code="KEPT", checksum="same"),
        make_node("other", checksum="old", **spec),
    )
    target = write_target(tmp_path, current)
    code = main(
        ["check", "--current", str(target), "--baseline", str(baseline)],
        runner=FakeDryRunner({"KEPT": TIB}),
    )
    err = capsys.readouterr().err
    assert code == 0
    assert "other changed but is not priced" in err
    assert expected in err


def test_an_unpriced_node_is_named_even_when_models_were_estimated_too(tmp_path: Path, capsys):
    """A change that touches three models and a snapshot still has an unpriced
    snapshot in it. Said on stderr, so it never reaches a pull-request comment
    looking like a figure."""
    current = make_manifest(
        make_node("kept", compiled_code="KEPT", checksum="new"),
        make_node("orders_snapshot", resource_type="snapshot", checksum="new"),
    )
    baseline = _baseline(
        tmp_path,
        make_node("kept", compiled_code="KEPT", checksum="old"),
        make_node("orders_snapshot", resource_type="snapshot", checksum="old"),
    )
    target = write_target(tmp_path, current)
    code = main(
        ["check", "--current", str(target), "--baseline", str(baseline), "--format", "json"],
        runner=FakeDryRunner({"KEPT": TIB}),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert [m["name"] for m in json.loads(captured.out)["models"]] == ["kept"]
    assert "orders_snapshot changed but is not priced" in captured.err


def test_a_run_with_nothing_at_all_changed_stays_quiet(tmp_path: Path, capsys):
    manifest = make_manifest(make_node("kept", compiled_code="KEPT", checksum="same"))
    target = write_target(tmp_path, manifest)
    baseline = _baseline(tmp_path, make_node("kept", compiled_code="KEPT", checksum="same"))
    main(
        ["check", "--current", str(target), "--baseline", str(baseline)],
        runner=FakeDryRunner({"KEPT": TIB}),
    )
    captured = capsys.readouterr()
    assert "not priced" not in captured.err
    assert "No changed models" in captured.out


# --------------------------------------------------------------------------
# F17 — the wrong warehouse.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ["duckdb", "snowflake", "postgres"])
def test_a_manifest_from_another_warehouse_is_refused(tmp_path: Path, capsys, adapter: str):
    """Pricing a Snowflake project at BigQuery on-demand rates is the worst kind
    of wrong answer, because it looks exactly like a right one."""
    manifest = make_manifest(make_node("m", compiled_code="SQL"))
    manifest["metadata"] = {"adapter_type": adapter}
    target = write_target(tmp_path, manifest)
    code = main(
        ["check", "--current", str(target), "--select", "m"], runner=FakeDryRunner({"SQL": TIB})
    )
    err = capsys.readouterr().err
    assert code == 2
    assert adapter in err
    assert "BigQuery" in err


def test_a_bigquery_manifest_runs(tmp_path: Path):
    manifest = make_manifest(make_node("m", compiled_code="SQL"))
    manifest["metadata"] = {"adapter_type": "bigquery"}
    target = write_target(tmp_path, manifest)
    assert (
        main(
            ["check", "--current", str(target), "--select", "m"], runner=FakeDryRunner({"SQL": TIB})
        )
        == 0
    )


def test_a_manifest_with_no_metadata_still_runs(tmp_path: Path):
    """Older manifests do not carry `adapter_type`, and refusing to run on one
    would be a regression for no gain."""
    target = write_target(tmp_path, make_manifest(make_node("m", compiled_code="SQL")))
    assert (
        main(
            ["check", "--current", str(target), "--select", "m"], runner=FakeDryRunner({"SQL": TIB})
        )
        == 0
    )
