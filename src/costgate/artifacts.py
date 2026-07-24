# SPDX-License-Identifier: Apache-2.0
"""Read dbt artifacts and select the cost-bearing models to estimate.

Pure functions over manifest JSON — no dbt or git invocation. The only I/O is
reading files the user already produced with ``dbt compile``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from costgate.models import EstimateBasis, ModelNode

# Statically-unknowable filters make a BigQuery dry-run fall back to a full-table
# scan (see docs/architecture.md). We can't correct the number, only flag it.
_DYNAMIC_FILTER = re.compile(
    r"\b(current_date|current_timestamp|current_datetime)\b", re.IGNORECASE
)
_SUBQUERY_IN_PREDICATE = re.compile(r"\b(where|and|or)\b[^;]*\(\s*select\b", re.IGNORECASE)
_MULTI_STATEMENT = re.compile(r"\b(declare|begin)\b", re.IGNORECASE)


class ArtifactError(Exception):
    """A manifest could not be read or is missing information costgate needs."""


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
        )
    return out


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


def select_changed(
    baseline: dict[str, ModelNode],
    current: dict[str, ModelNode],
    renames: dict[str, str] | None = None,
) -> list[str]:
    """Added or body-modified models (checksum differs). Mirrors the common core
    of dbt's state:modified; config-only/macro-only changes are a documented gap.
    A declared rename (``current_uid`` in ``renames``) is always selected."""
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
    return changed


def detect_basis(node: ModelNode, sql: str | None) -> EstimateBasis:
    """A self-reference in an incremental model's compiled SQL means it was
    compiled against an existing table (incremental form); its absence means the
    full-refresh form. Non-incrementals are always direct."""
    if not node.is_incremental:
        return EstimateBasis.DIRECT
    if sql and node.relation_name and node.relation_name in sql:
        return EstimateBasis.INCREMENTAL_FORM
    return EstimateBasis.FULL_REFRESH


def sql_warnings(node: ModelNode, sql: str | None) -> list[str]:
    """Non-fatal caveats about how trustworthy a dry-run of this SQL will be."""
    warnings: list[str] = []
    if not sql:
        return warnings
    if _DYNAMIC_FILTER.search(sql) or _SUBQUERY_IN_PREDICATE.search(sql):
        warnings.append("dynamic filter — dry-run may be worst-case (overestimate)")
    if _MULTI_STATEMENT.search(sql):
        warnings.append("multi-statement — dry-run bytes may be partial")
    if node.is_incremental:
        warnings.append("incremental — figure is the full-refresh scan")
    return warnings
