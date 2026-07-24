# SPDX-License-Identifier: Apache-2.0
"""Shared test builders: hand-authored manifest nodes and a fake dry-runner, so
the whole pipeline is exercised without a warehouse or credentials."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from costgate.bigquery import DryRunResult
from costgate.models import ErrorKind


def make_node(
    name: str,
    *,
    resource_type: str = "model",
    language: str = "sql",
    materialized: str = "view",
    compiled_code: str | None = None,
    checksum: str | None = None,
    relation_name: str | None = None,
    original_file_path: str | None = None,
    compiled_path: str | None = None,
    patch_path: str | None = None,
    depends_on_macros: list[str] | None = None,
    database: str = "proj",
    schema: str = "analytics",
    package: str = "pkg",
) -> tuple[str, dict]:
    unique_id = f"{resource_type}.{package}.{name}"
    node = {
        "name": name,
        "resource_type": resource_type,
        "language": language,
        "database": database,
        "schema": schema,
        "relation_name": relation_name or f"`{database}`.`{schema}`.`{name}`",
        "compiled_code": compiled_code
        if compiled_code is not None
        else f"select * from {name}_src",
        "compiled_path": compiled_path,
        "original_file_path": original_file_path or f"models/{name}.sql",
        "patch_path": patch_path,
        "depends_on": {"macros": list(depends_on_macros or [])},
        "checksum": {"checksum": checksum if checksum is not None else f"sum-{name}"},
        "config": {"materialized": materialized},
    }
    return unique_id, node


def make_macro(
    name: str,
    *,
    original_file_path: str | None = None,
    depends_on_macros: list[str] | None = None,
    package: str = "pkg",
) -> tuple[str, dict]:
    unique_id = f"macro.{package}.{name}"
    macro = {
        "name": name,
        "resource_type": "macro",
        "original_file_path": original_file_path or f"macros/{name}.sql",
        "macro_sql": f"{{% macro {name}() %}}{{% endmacro %}}",
        "depends_on": {"macros": list(depends_on_macros or [])},
    }
    return unique_id, macro


def make_manifest(*nodes: tuple[str, dict], macros: tuple[tuple[str, dict], ...] = ()) -> dict:
    return {
        "nodes": {uid: node for uid, node in nodes},
        "macros": {uid: macro for uid, macro in macros},
    }


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def init_repo(repo: Path) -> None:
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")


def write_target(tmp_path: Path, manifest: dict, compiled: dict[str, str] | None = None) -> Path:
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    for rel, sql in (compiled or {}).items():
        fp = target / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(sql, "utf-8")
    return target


class FakeDryRunner:
    """Maps a model name (matched as a substring of the SQL) to canned bytes or a
    DryRunResult. Unmapped SQL returns a small default so tests stay terse."""

    def __init__(self, responses: dict[str, object], default_bytes: int = 1024):
        self.responses = responses
        self.default_bytes = default_bytes
        self.calls: list[str] = []

    def dry_run(self, sql: str, self_relation: str | None = None) -> DryRunResult:
        self.calls.append(sql)
        for key, val in self.responses.items():
            if key in sql:
                return self._to_result(val)
        return DryRunResult(total_bytes=self.default_bytes, location="US")

    @staticmethod
    def _to_result(val: object) -> DryRunResult:
        if isinstance(val, DryRunResult):
            return val
        if isinstance(val, ErrorKind):
            return DryRunResult(error_kind=val, error_detail=f"simulated {val.value}")
        return DryRunResult(total_bytes=int(val), location="US")  # type: ignore[arg-type]


@pytest.fixture
def fake_runner():
    return FakeDryRunner
