# SPDX-License-Identifier: Apache-2.0
"""Read dbt artifacts and select the cost-bearing models to estimate.

Pure functions over manifest JSON — no dbt or git invocation. The only I/O is
reading files the user already produced with ``dbt compile``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dbt_costgate.models import BASIS_LABELS, EstimateBasis, ModelNode

# Statically-unknowable filters make a BigQuery dry-run fall back to a full-table
# scan (see docs/architecture.md). We can't correct the number, only flag it.
_DYNAMIC_FILTER = re.compile(
    r"\b(current_date|current_timestamp|current_datetime)\b", re.IGNORECASE
)
_SUBQUERY_IN_PREDICATE = re.compile(r"\b(where|and|or)\b[^;]*\(\s*select\b", re.IGNORECASE)
_MULTI_STATEMENT = re.compile(r"\b(declare|begin)\b", re.IGNORECASE)


class ArtifactError(Exception):
    """A manifest could not be read or is missing information dbt-costgate needs."""


def manifest_path(path: Path) -> Path:
    """Accept either a manifest.json or a dbt ``target/`` directory."""
    if path.is_dir():
        return path / "manifest.json"
    return path


def load_manifest(path: Path) -> dict:
    mpath = manifest_path(path)
    if not mpath.is_file():
        raise ArtifactError(
            f"No manifest.json at {mpath}. Run `dbt compile` first, then point "
            f"--current at the target/ directory (or pass the manifest path)."
        )
    try:
        return json.loads(mpath.read_text("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ArtifactError(f"{mpath} is not valid JSON: {exc}") from exc


def model_nodes(manifest: dict) -> dict[str, ModelNode]:
    """Cost-bearing model nodes only, keyed by unique_id.

    Filters to SQL models: skips Python models (compiled_code is Python, not SQL),
    ephemeral models (no queryable relation), and every non-model resource type.
    """
    out: dict[str, ModelNode] = {}
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if node.get("resource_type") != "model":
            continue
        if node.get("language", "sql") != "sql":
            continue
        config = node.get("config") or {}
        if config.get("materialized") == "ephemeral":
            continue
        checksum = (node.get("checksum") or {}).get("checksum")
        out[unique_id] = ModelNode(
            unique_id=unique_id,
            name=node.get("name", unique_id),
            materialized=config.get("materialized", "view"),
            language=node.get("language", "sql"),
            database=node.get("database"),
            schema=node.get("schema"),
            relation_name=node.get("relation_name"),
            checksum=checksum,
            compiled_path=node.get("compiled_path"),
            compiled_code=node.get("compiled_code"),
            original_file_path=node.get("original_file_path"),
            patch_path=node.get("patch_path"),
            depends_on_macros=list((node.get("depends_on") or {}).get("macros") or []),
        )
    return out


def adapter_problem(manifest: dict) -> str | None:
    """A message when this manifest was not compiled for BigQuery, else None.

    Nothing read `metadata.adapter_type` at all, so pointing dbt-costgate at a
    Snowflake, Postgres or duckdb project produced confident BigQuery dollar
    figures for SQL BigQuery would never run — the worst kind of wrong answer,
    because it looks exactly like a right one.

    Absent metadata is allowed through: older manifests do not carry it, and
    refusing to run on one would be a regression for no gain.
    """
    adapter = (manifest.get("metadata") or {}).get("adapter_type")
    if not adapter or adapter == "bigquery":
        return None
    return (
        f"this manifest was compiled for the {adapter} adapter. dbt-costgate estimates "
        f"BigQuery costs from BigQuery dry-runs, so every figure it produced here would be "
        f"a BigQuery price for SQL BigQuery is not going to run."
    )


def out_of_scope_nodes(manifest: dict) -> dict[str, str]:
    """Nodes `model_nodes` drops, mapped to why — name -> reason.

    They are filtered out before anything can count them, so a branch that only
    touches a seed, a snapshot, an ephemeral or a Python model was
    indistinguishable from a branch that changed nothing: both printed "No
    changed models to estimate". Being out of scope is defensible; being
    indistinguishable from "nothing happened" is not, and a snapshot really does
    run a MERGE and really does scan bytes.
    """
    out: dict[str, str] = {}
    for unique_id, node in (manifest.get("nodes") or {}).items():
        name = node.get("name", unique_id)
        resource_type = node.get("resource_type")
        config = node.get("config") or {}
        if resource_type != "model":
            if resource_type in ("seed", "snapshot", "test", "analysis", "operation"):
                out[name] = f"{resource_type}s are not priced"
        elif node.get("language", "sql") != "sql":
            out[name] = "Python models are not priced — dbt-costgate estimates SQL scans"
        elif config.get("materialized") == "ephemeral":
            out[name] = (
                "ephemeral models have no relation of their own; their SQL is inlined into "
                "the models that select from them, and the cost shows up there"
            )
    return out


def changed_out_of_scope(
    current: dict, baseline: dict | None = None, paths: list[str] | None = None
) -> dict[str, str]:
    """Out-of-scope nodes this change actually touched — name -> reason.

    So that "nothing to estimate" and "the things you changed are not things I
    price" stop looking identical. A seed-only or snapshot-only branch printed
    the former in both local and baseline mode, and a snapshot really does run a
    MERGE and really does scan bytes.
    """
    scope = out_of_scope_nodes(current)
    if not scope:
        return {}
    changed_paths = set(paths or ())
    base_nodes = (baseline or {}).get("nodes") or {}
    touched: dict[str, str] = {}
    for unique_id, node in (current.get("nodes") or {}).items():
        name = node.get("name", unique_id)
        if name not in scope:
            continue
        if baseline is not None:
            base = base_nodes.get(unique_id)
            if base is None or _raw_checksum(base) != _raw_checksum(node):
                touched[name] = scope[name]
        elif node.get("original_file_path") in changed_paths:
            touched[name] = scope[name]
    return touched


def _raw_checksum(node: dict) -> str | None:
    return (node.get("checksum") or {}).get("checksum")


def has_any_compiled_code(nodes: dict[str, ModelNode]) -> bool:
    return any(n.compiled_code for n in nodes.values())


def resolve_compiled_sql(node: ModelNode, project_dir: Path | None) -> str | None:
    """Prefer the on-disk compiled file (always written by ``dbt compile``);
    fall back to the manifest's inlined ``compiled_code``."""
    if node.compiled_path and project_dir is not None:
        candidate = project_dir / node.compiled_path
        if candidate.is_file():
            return candidate.read_text("utf-8")
    return node.compiled_code


def _resolve_ref(ref: str, nodes: dict[str, ModelNode], side: str) -> str:
    """Resolve a user-supplied model reference to a unique_id. Accepts a full
    unique_id (used verbatim) or a bare model name (looked up by ``.name``)."""
    if ref in nodes:
        return ref
    matches = [uid for uid, n in nodes.items() if n.name == ref]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ArtifactError(
            f"renames: {side} model {ref!r} not found. Use its dbt model name or its "
            f"full unique_id (e.g. model.<package>.<name>)."
        )
    raise ArtifactError(
        f"renames: {side} model name {ref!r} is ambiguous across packages "
        f"({', '.join(sorted(matches))}). Disambiguate with the full unique_id."
    )


def resolve_renames(
    renames: dict[str, str],
    current: dict[str, ModelNode],
    baseline: dict[str, ModelNode],
) -> dict[str, str]:
    """Turn a user ``renames`` map (current -> baseline, by name or unique_id) into
    a ``current_uid -> baseline_uid`` map against the loaded manifests. Fails loudly
    on an unresolvable, ambiguous, or many-to-one mapping rather than mis-diffing."""
    resolved: dict[str, str] = {}
    for current_ref, baseline_ref in renames.items():
        cur_uid = _resolve_ref(current_ref, current, "current")
        base_uid = _resolve_ref(baseline_ref, baseline, "baseline")
        resolved[cur_uid] = base_uid
    if len(set(resolved.values())) < len(resolved):
        raise ArtifactError(
            "renames: two or more current models map to the same baseline model; "
            "each baseline may be paired with only one current model."
        )
    return resolved


def _compiled_differs(base: ModelNode, node: ModelNode) -> bool:
    """Whether the two versions compile to different SQL. A missing side is
    *unknown*, not different — a parse-only manifest must never select everything."""
    if base.compiled_code is None or node.compiled_code is None:
        return False
    return base.compiled_code != node.compiled_code


def select_changed(
    baseline: dict[str, ModelNode],
    current: dict[str, ModelNode],
    renames: dict[str, str] | None = None,
) -> list[str]:
    """Models that were added, deleted, whose body changed (checksum differs), or
    whose compiled SQL differs — the last catching config-only and macro-only
    changes, which never touch the model's own file. A declared rename
    (``current_uid`` in ``renames``) is always selected."""
    renames = renames or {}
    changed = []
    for uid, node in current.items():
        if uid in renames:
            changed.append(uid)
            continue
        base = baseline.get(uid)
        if base is None:
            changed.append(uid)
        elif node.checksum is None or base.checksum is None or node.checksum != base.checksum:
            changed.append(uid)
        elif _compiled_differs(base, node):
            changed.append(uid)
    # Deletions. This used to walk the current manifest only, so removing a model
    # — the most common deliberate cost reduction there is, and a story the README
    # sells — produced no row and no credit: deleting a 411 GiB/run model reported
    # "Net change: none". A renamed model's baseline is not a deletion; it is the
    # other half of a pair already selected above.
    renamed_baselines = set(renames.values())
    changed.extend(uid for uid in baseline if uid not in current and uid not in renamed_baselines)
    return changed


def indirect_changes(baseline: dict[str, ModelNode], current: dict[str, ModelNode]) -> set[str]:
    """The models ``select_changed`` picks up *only* from their compiled SQL. Their
    own file is untouched, so the cause is upstream — surfaced as a warning so a
    model never appears in a report without an explanation."""
    out: set[str] = set()
    for uid, node in current.items():
        base = baseline.get(uid)
        if base is None or node.checksum is None or base.checksum is None:
            continue
        if node.checksum == base.checksum and _compiled_differs(base, node):
            out.add(uid)
    return out


@dataclass(frozen=True)
class MacroIndex:
    """The macro graph as the manifest records it: the file each macro is defined
    in, and the macros each macro calls."""

    files: dict[str, str]  # macro unique_id -> original_file_path
    depends_on: dict[str, list[str]]  # macro unique_id -> macro unique_ids it calls


def macro_index(manifest: dict) -> MacroIndex:
    files: dict[str, str] = {}
    depends_on: dict[str, list[str]] = {}
    for uid, macro in (manifest.get("macros") or {}).items():
        path = macro.get("original_file_path")
        if path:
            files[uid] = path
        depends_on[uid] = list((macro.get("depends_on") or {}).get("macros") or [])
    return MacroIndex(files=files, depends_on=depends_on)


def _changed_macros(paths: set[str], index: MacroIndex) -> set[str]:
    """Macros defined in a changed file, plus every macro that calls one — dbt
    treats the caller of a changed macro as changed too, and so must we."""
    callers: dict[str, set[str]] = {}
    for uid, deps in index.depends_on.items():
        for dep in deps:
            callers.setdefault(dep, set()).add(uid)
    stack = [uid for uid, path in index.files.items() if path in paths]
    seen: set[str] = set()
    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        stack.extend(callers.get(uid, ()))
    return seen


def _patch_file(patch_path: str) -> str:
    """``patch_path`` is ``<package>://<path>``; the path half is project-relative,
    the same basis as ``original_file_path``."""
    return patch_path.split("://", 1)[-1]


@dataclass(frozen=True)
class EphemeralIndex:
    """Ephemeral models and who inlines them.

    Ephemeral nodes never reach `model_nodes` — they have no relation of their
    own — so a branch touching only `models/intermediate/int_order_items.sql`
    selected nothing locally and printed "No changed models to estimate", even
    though that SQL is inlined into two downstream models and the cost change is
    entirely real. The baseline path found it; the local path, which is what the
    pre-commit hook runs, did not. Intermediate models are one of the most common
    places a filter gets widened.
    """

    files: dict[str, str]  # ephemeral unique_id -> its project-relative file
    dependents: dict[str, set[str]]  # unique_id -> the nodes that select from it


def ephemeral_index(manifest: dict) -> EphemeralIndex:
    nodes = manifest.get("nodes") or {}
    files = {
        uid: node.get("original_file_path") or ""
        for uid, node in nodes.items()
        if node.get("resource_type") == "model"
        and (node.get("config") or {}).get("materialized") == "ephemeral"
    }
    dependents: dict[str, set[str]] = {}
    for uid, node in nodes.items():
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            dependents.setdefault(dep, set()).add(uid)
    return EphemeralIndex(files=files, dependents=dependents)


def _inlining_models(
    changed_paths: set[str], index: EphemeralIndex, nodes: dict[str, ModelNode]
) -> set[str]:
    """Priced models that inline a changed ephemeral, following chains of them.

    Walks down rather than up, and stops at the first priced model: that is where
    the ephemeral's SQL ends up, and where the cost shows.
    """
    stack = [uid for uid, path in index.files.items() if path and path in changed_paths]
    seen: set[str] = set()
    out: set[str] = set()
    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        for child in index.dependents.get(uid, ()):
            if child in nodes:
                out.add(child)
            elif child in index.files:
                stack.append(child)
    return out


def select_by_paths(
    nodes: dict[str, ModelNode],
    paths: list[str],
    macros: MacroIndex | None = None,
    ephemeral: EphemeralIndex | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Models a set of changed project-relative paths touches: the model's own file,
    the YAML that patches it, any macro in its macro closure, or an ephemeral model
    whose SQL it inlines. Needs no baseline manifest, so it drives the zero-setup
    local run.

    Returns the selection and, for anything selected for a reason other than its
    own file changing, a note saying why — so a model never appears in a report
    without an explanation.
    """
    changed = set(paths)
    changed_macros = _changed_macros(changed, macros) if macros else set()
    via_ephemeral = _inlining_models(changed, ephemeral, nodes) if ephemeral else set()
    selected = []
    notes: dict[str, str] = {}
    for uid, node in nodes.items():
        if node.original_file_path and node.original_file_path in changed:
            selected.append(uid)
        elif node.patch_path and _patch_file(node.patch_path) in changed:
            selected.append(uid)
        elif changed_macros.intersection(node.depends_on_macros):
            selected.append(uid)
        elif uid in via_ephemeral:
            selected.append(uid)
            notes[uid] = (
                "selected because an ephemeral model it inlines changed — the model's own "
                "file is untouched"
            )
    return selected, notes


def touches_project_config(paths: list[str]) -> bool:
    """A ``dbt_project.yml`` change is project-wide config — real, but not
    attributable to individual models from paths alone. Paths are project-relative,
    so only this project's file matches, never a sibling project's."""
    return any(p == "dbt_project.yml" for p in paths)


def detect_basis(node: ModelNode, sql: str | None) -> EstimateBasis | None:
    """Which query shape this SQL is, or None when there is no SQL to judge.

    A self-reference in an incremental model's compiled SQL means it was compiled
    against an existing table (incremental form); its absence means the
    full-refresh form. Non-incrementals are always direct.

    The no-SQL case returns None rather than falling through to a basis. A basis
    describes a measurement, so with nothing compiled there is nothing to
    describe: answering `full_refresh` there named a shape for SQL that does not
    exist, which then reached a reader as a row tag on an unestimated model and,
    worse, as a `mixed basis — baseline is full_refresh` warning about a baseline
    that was never compiled at all.

    The check comes first, ahead of the non-incremental branch, for the same
    reason — "nothing was measured" outranks "this is a table".
    """
    if not sql:
        return None
    if not node.is_incremental:
        return EstimateBasis.DIRECT
    if node.relation_name and node.relation_name in sql:
        return EstimateBasis.INCREMENTAL_FORM
    return EstimateBasis.FULL_REFRESH


def sql_warnings(node: ModelNode, sql: str | None) -> list[str]:
    """Non-fatal caveats about how trustworthy a dry-run of this SQL will be."""
    warnings: list[str] = []
    if node.materialized == "materialized_view":
        # A materialized view is priced exactly like a plain view, and the number
        # that comes out describes building it once. BigQuery refreshes it on its
        # own schedule and bills each refresh, so the recurring cost — the figure
        # a reader of this row actually wants — is some multiple of what is shown,
        # and dbt-costgate cannot see the schedule to say which.
        warnings.append(
            "materialized view — this is the cost of building it once; BigQuery bills each "
            "automatic refresh separately, so the recurring cost is higher"
        )
    if not sql:
        return warnings
    if _DYNAMIC_FILTER.search(sql) or _SUBQUERY_IN_PREDICATE.search(sql):
        warnings.append("dynamic filter — dry-run may be worst-case (overestimate)")
    if _MULTI_STATEMENT.search(sql):
        warnings.append("multi-statement — dry-run bytes may be partial")
    # Through `detect_basis` rather than `node.is_incremental`, so the warning and
    # the basis the report labels the row with are the same answer to the same
    # question. Asking `is_incremental` here is what told a model compiled in
    # incremental form that its figure was a full-refresh scan: true of the model,
    # false of the number beside it.
    label = BASIS_LABELS.get(detect_basis(node, sql))
    if label:
        warnings.append(label.warning)
    return warnings
