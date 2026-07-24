# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from conftest import make_manifest, make_node, write_target
from costgate import artifacts
from costgate.artifacts import ArtifactError
from costgate.models import EstimateBasis


def test_node_filtering_excludes_noncost_and_python_and_ephemeral():
    manifest = make_manifest(
        make_node("fct_orders"),
        make_node("py_model", language="python"),
        make_node("eph", materialized="ephemeral"),
        make_node("my_seed", resource_type="seed"),
        make_node("my_test", resource_type="test"),
        make_node("snap", resource_type="snapshot"),
    )
    nodes = artifacts.model_nodes(manifest)
    assert set(n.name for n in nodes.values()) == {"fct_orders"}


def test_select_changed_detects_added_and_modified_not_unchanged():
    baseline = artifacts.model_nodes(
        make_manifest(
            make_node("unchanged", checksum="a"),
            make_node("modified", checksum="old"),
        )
    )
    current = artifacts.model_nodes(
        make_manifest(
            make_node("unchanged", checksum="a"),
            make_node("modified", checksum="new"),
            make_node("added", checksum="z"),
        )
    )
    changed = {current[uid].name for uid in artifacts.select_changed(baseline, current)}
    assert changed == {"modified", "added"}


def test_resolve_compiled_sql_prefers_file_then_falls_back(tmp_path: Path):
    manifest = make_manifest(
        make_node(
            "fct",
            compiled_code="INLINE FALLBACK",
            compiled_path="compiled/pkg/models/fct.sql",
        )
    )
    target = write_target(tmp_path, manifest, {"compiled/pkg/models/fct.sql": "FILE WINS"})
    node = next(iter(artifacts.model_nodes(manifest).values()))
    assert artifacts.resolve_compiled_sql(node, target) == "FILE WINS"
    assert artifacts.resolve_compiled_sql(node, None) == "INLINE FALLBACK"


def test_detect_basis_incremental_self_reference_vs_full_refresh():
    uid, node_dict = make_node("inc", materialized="incremental", relation_name="`p`.`s`.`inc`")
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]
    assert artifacts.detect_basis(node, "select * from x where 1") == EstimateBasis.FULL_REFRESH
    incr_sql = "select * from x where ts > (select max(ts) from `p`.`s`.`inc`)"
    assert artifacts.detect_basis(node, incr_sql) == EstimateBasis.INCREMENTAL_FORM


def test_sql_warnings_flags_dynamic_multistatement_and_incremental():
    uid, node_dict = make_node("inc", materialized="incremental")
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]
    warnings = artifacts.sql_warnings(node, "select * from t where d >= CURRENT_DATE()")
    joined = " ".join(warnings)
    assert "dynamic filter" in joined
    assert "full-refresh" in joined

    uid2, nd2 = make_node("multi")
    node2 = artifacts.model_nodes(make_manifest((uid2, nd2)))[uid2]
    assert any("multi-statement" in w for w in artifacts.sql_warnings(node2, "DECLARE x INT64;"))


def test_load_manifest_missing_raises_actionable_error(tmp_path: Path):
    with pytest.raises(ArtifactError, match="dbt compile"):
        artifacts.load_manifest(tmp_path / "target")


# --- rename map (F1) ---


def _bl_cur(baseline_specs, current_specs):
    return (
        artifacts.model_nodes(make_manifest(*baseline_specs)),
        artifacts.model_nodes(make_manifest(*current_specs)),
    )


def test_resolve_renames_bare_name_both_sides():
    baseline, current = _bl_cur([make_node("fct_orders_monthly")], [make_node("fct_orders_daily")])
    resolved = artifacts.resolve_renames(
        {"fct_orders_daily": "fct_orders_monthly"}, current, baseline
    )
    assert resolved == {"model.pkg.fct_orders_daily": "model.pkg.fct_orders_monthly"}


def test_resolve_renames_accepts_full_unique_id():
    baseline, current = _bl_cur([make_node("fct_orders_monthly")], [make_node("fct_orders_daily")])
    resolved = artifacts.resolve_renames(
        {"model.pkg.fct_orders_daily": "model.pkg.fct_orders_monthly"}, current, baseline
    )
    assert resolved == {"model.pkg.fct_orders_daily": "model.pkg.fct_orders_monthly"}


def test_resolve_renames_missing_current_ref_raises():
    baseline, current = _bl_cur([make_node("mon")], [make_node("day")])
    with pytest.raises(ArtifactError, match="nope"):
        artifacts.resolve_renames({"nope": "mon"}, current, baseline)


def test_resolve_renames_missing_baseline_ref_raises():
    baseline, current = _bl_cur([make_node("mon")], [make_node("day")])
    with pytest.raises(ArtifactError, match="ghost"):
        artifacts.resolve_renames({"day": "ghost"}, current, baseline)


def test_resolve_renames_ambiguous_bare_name_tells_user_to_qualify():
    # same bare name in two packages on the current side
    baseline, current = _bl_cur(
        [make_node("mon")],
        [make_node("dupe", package="pkg_a"), make_node("dupe", package="pkg_b")],
    )
    with pytest.raises(ArtifactError, match="unique_id"):
        artifacts.resolve_renames({"dupe": "mon"}, current, baseline)


def test_resolve_renames_rejects_two_current_to_one_baseline():
    baseline, current = _bl_cur(
        [make_node("mon")],
        [make_node("daily"), make_node("weekly")],
    )
    with pytest.raises(ArtifactError, match="same baseline"):
        artifacts.resolve_renames({"daily": "mon", "weekly": "mon"}, current, baseline)


def test_select_changed_always_selects_a_declared_rename():
    # identical checksum would normally be "unchanged"; a declared rename is still shown
    baseline = artifacts.model_nodes(make_manifest(make_node("mon", checksum="same")))
    current = artifacts.model_nodes(make_manifest(make_node("day", checksum="same")))
    renames = {"model.pkg.day": "model.pkg.mon"}
    changed = {current[uid].name for uid in artifacts.select_changed(baseline, current, renames)}
    assert changed == {"day"}
