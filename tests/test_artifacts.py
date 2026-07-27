# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from conftest import make_macro, make_manifest, make_node, write_target
from dbt_costgate import artifacts
from dbt_costgate.artifacts import ArtifactError
from dbt_costgate.models import BASIS_LABELS, EstimateBasis


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


def test_select_changed_detects_a_compiled_sql_change_with_an_unchanged_body():
    # An upstream macro (or a config change) rewrites the compiled SQL while the
    # model's own .sql file — and therefore its checksum — is untouched.
    baseline = artifacts.model_nodes(
        make_manifest(make_node("fct", checksum="same", compiled_code="select a from t"))
    )
    current = artifacts.model_nodes(
        make_manifest(make_node("fct", checksum="same", compiled_code="select a, b from t"))
    )
    assert artifacts.select_changed(baseline, current) == ["model.pkg.fct"]


def test_select_changed_ignores_compiled_sql_when_either_side_is_missing():
    # A parse-only manifest has no compiled_code; that must not select the project.
    baseline = artifacts.model_nodes(
        make_manifest(make_node("fct", checksum="same", compiled_code="select 1"))
    )
    uid, raw = make_node("fct", checksum="same")
    raw["compiled_code"] = None  # a `dbt parse` manifest carries no compiled SQL
    current = artifacts.model_nodes(make_manifest((uid, raw)))
    assert artifacts.select_changed(baseline, current) == []
    assert artifacts.indirect_changes(baseline, current) == set()


def test_indirect_changes_names_only_the_body_unchanged_models():
    baseline = artifacts.model_nodes(
        make_manifest(
            make_node("body", checksum="old", compiled_code="select 1"),
            make_node("macro_only", checksum="same", compiled_code="select 1"),
            make_node("untouched", checksum="same", compiled_code="select 1"),
        )
    )
    current = artifacts.model_nodes(
        make_manifest(
            make_node("body", checksum="new", compiled_code="select 2"),
            make_node("macro_only", checksum="same", compiled_code="select 2"),
            make_node("untouched", checksum="same", compiled_code="select 1"),
        )
    )
    assert artifacts.indirect_changes(baseline, current) == {"model.pkg.macro_only"}


def test_select_by_paths_matches_model_file_patch_file_and_macro():
    manifest = make_manifest(
        make_node("by_sql", original_file_path="models/by_sql.sql"),
        make_node("by_yml", patch_path="pkg://models/schema.yml"),
        make_node("by_macro", depends_on_macros=["macro.pkg.cents"]),
        make_node("untouched"),
        macros=(make_macro("cents", original_file_path="macros/cents.sql"),),
    )
    nodes = artifacts.model_nodes(manifest)
    selected, _ = artifacts.select_by_paths(
        nodes,
        ["models/by_sql.sql", "models/schema.yml", "macros/cents.sql"],
        artifacts.macro_index(manifest),
    )
    assert {nodes[uid].name for uid in selected} == {"by_sql", "by_yml", "by_macro"}


def test_select_by_paths_follows_a_macro_chain():
    # dbt treats a macro that calls a changed macro as changed too; so must we.
    manifest = make_manifest(
        make_node("fct", depends_on_macros=["macro.pkg.outer"]),
        macros=(
            make_macro("outer", depends_on_macros=["macro.pkg.inner"]),
            make_macro("inner", original_file_path="macros/inner.sql"),
        ),
    )
    nodes = artifacts.model_nodes(manifest)
    selected, _ = artifacts.select_by_paths(
        nodes, ["macros/inner.sql"], artifacts.macro_index(manifest)
    )
    assert selected == ["model.pkg.fct"]


def test_select_by_paths_ignores_an_unrelated_macro():
    manifest = make_manifest(
        make_node("fct", depends_on_macros=["macro.pkg.used"]),
        macros=(
            make_macro("used", original_file_path="macros/used.sql"),
            make_macro("unused", original_file_path="macros/unused.sql"),
        ),
    )
    nodes = artifacts.model_nodes(manifest)
    assert (
        artifacts.select_by_paths(nodes, ["macros/unused.sql"], artifacts.macro_index(manifest))[0]
        == []
    )


def test_touches_project_config_spots_only_this_projects_dbt_project_yml():
    # Paths are project-relative, so a sibling project's config is not ours to warn about.
    assert artifacts.touches_project_config(["dbt_project.yml"])
    assert not artifacts.touches_project_config(["other_project/dbt_project.yml"])
    assert not artifacts.touches_project_config(["models/dbt_project_notes.md"])


def test_the_warning_describes_the_basis_that_was_measured():
    """An incremental model compiled against its existing table was dry-run in
    incremental form, so its figure is one run and not a rebuild. Deriving the
    warning from `node.is_incremental` told it the opposite: true of the model,
    false of the number beside it, and wrong in the direction that matters — an
    incremental figure read as rebuild cost badly understates a rebuild.
    """
    uid, node_dict = make_node("inc", materialized="incremental", relation_name="`p`.`s`.`inc`")
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]

    fresh = artifacts.sql_warnings(node, "select * from x where 1")
    incremental = artifacts.sql_warnings(
        node, "select * from x where ts > (select max(ts) from `p`.`s`.`inc`)"
    )
    assert BASIS_LABELS[EstimateBasis.FULL_REFRESH].warning in fresh
    assert BASIS_LABELS[EstimateBasis.INCREMENTAL_FORM].warning in incremental
    # Neither may carry the other's: the two make opposite claims about the figure.
    assert BASIS_LABELS[EstimateBasis.INCREMENTAL_FORM].warning not in fresh
    assert BASIS_LABELS[EstimateBasis.FULL_REFRESH].warning not in incremental


def test_the_warning_and_the_basis_are_one_answer_to_one_question():
    """`sql_warnings` and `detect_basis` are called separately — the first by the
    estimator for the model, the second for the row's label. Left to compute the
    basis independently they could disagree about the same SQL, which is exactly
    how the row tag and the warning came apart. Asserted across both shapes."""
    uid, node_dict = make_node("inc", materialized="incremental", relation_name="`p`.`s`.`inc`")
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]
    for sql in ("select * from x where 1", "select * from `p`.`s`.`inc`"):
        basis = artifacts.detect_basis(node, sql)
        assert BASIS_LABELS[basis].warning in artifacts.sql_warnings(node, sql)


def test_a_non_incremental_model_gets_no_basis_warning():
    """DIRECT is absent from the registry because a table or a view compiles one
    way — there is no basis to disambiguate and so nothing to say."""
    uid, node_dict = make_node("plain")
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]
    warnings = artifacts.sql_warnings(node, "select * from x")
    assert not any(w == label.warning for label in BASIS_LABELS.values() for w in warnings)
    assert EstimateBasis.DIRECT not in BASIS_LABELS


def test_every_basis_that_needs_a_label_has_a_complete_one():
    """The tag, the warning and the footnote are three renderings of one fact and
    live in one entry, so a new basis cannot be added while leaving a renderer to
    guess. DIRECT is the only exemption, and it is asserted rather than assumed —
    otherwise this passes by finding nothing to check."""
    labelled = set(BASIS_LABELS)
    assert labelled == set(EstimateBasis) - {EstimateBasis.DIRECT}
    for basis, label in BASIS_LABELS.items():
        assert label.tag and label.warning and label.footnote, basis


@pytest.mark.parametrize("materialized", ["incremental", "view"])
def test_no_sql_yields_no_basis_whatever_the_materialization(materialized):
    """A basis describes a measurement. With nothing compiled there is nothing to
    describe, and the answer is absence rather than a default — including for a
    view, where `direct` would be a true statement about the model and still a
    claim about SQL that was never read."""
    uid, node_dict = make_node("m", materialized=materialized)
    node = artifacts.model_nodes(make_manifest((uid, node_dict)))[uid]
    assert artifacts.detect_basis(node, None) is None
    assert artifacts.detect_basis(node, "") is None
    assert artifacts.detect_basis(node, "select 1") is not None
