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
