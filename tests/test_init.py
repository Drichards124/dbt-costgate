# SPDX-License-Identifier: Apache-2.0
"""`dbt-costgate init` and the config template it writes.

The template is generated from `CONFIG_REFERENCE` so that it cannot fall behind
the code, but generation only moves the failure: a template that silently omits a
key, or one whose example values do not actually parse, would read as correct
while being useless. These tests pin both ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dbt_costgate.cli import main
from dbt_costgate.config import CONFIG_REFERENCE, Config, render_config_template


def _get(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_the_written_file_changes_nothing(tmp_path: Path):
    """The whole point of the starter file: writing it is not configuring
    anything. Every value it shows is an illustration, and a user who runs `init`
    and then `check` must get exactly the behaviour they had before."""
    (tmp_path / ".dbt-costgate.yml").write_text(render_config_template(), "utf-8")
    assert Config.load(None, tmp_path) == Config()


@pytest.mark.parametrize("field", CONFIG_REFERENCE, ids=lambda f: f.key)
def test_every_documented_key_appears_in_the_template(field):
    """A key added to the registry but missing from the template is invisible —
    the file just never mentions the setting."""
    leaf = field.key.rpartition(".")[2]
    assert f"{leaf}:" in render_config_template(), field.key


def test_nothing_in_the_template_is_live_yaml():
    """Catches a multi-line example whose first line was commented and whose rest
    was not — which would ship live settings inside a file the user has been told
    is inert. The section headers are the only content, deliberately."""
    headers = {f"{f.key.rpartition('.')[0]}:" for f in CONFIG_REFERENCE if "." in f.key}
    for line in render_config_template().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        assert stripped in headers, f"live YAML in the template: {line!r}"


def test_the_live_headers_parse_to_nothing():
    """The headers left uncommented are empty mappings. `Config._from_dict` reads
    each section with `or {}`, so an empty one is the same as an absent one —
    this is what makes leaving them live safe."""
    loaded = yaml.safe_load(render_config_template())
    assert loaded is None or all(v is None for v in loaded.values())


def test_every_example_is_yaml_the_parser_accepts(tmp_path: Path):
    """The commented file cannot demonstrate this: a broken example would sit
    inertly in the template forever. Rendered live, every example has to parse
    *and* land on the attribute it documents."""
    (tmp_path / ".dbt-costgate.yml").write_text(render_config_template(commented=False), "utf-8")
    cfg = Config.load(None, tmp_path)
    for field in CONFIG_REFERENCE:
        assert _get(cfg, field.attr) != field.default, (
            f"{field.key}: the example is not reaching {field.attr} — either it is "
            f"not valid YAML for this key, or it repeats the default and so proves "
            f"nothing"
        )


def test_init_writes_the_config_and_succeeds(tmp_path: Path, capsys):
    assert main(["init", "--project-dir", str(tmp_path)]) == 0
    written = tmp_path / ".dbt-costgate.yml"
    assert written.is_file()
    assert written.read_text("utf-8") == render_config_template()
    assert str(written) in capsys.readouterr().out


@pytest.mark.parametrize("name", Config.DEFAULT_FILENAMES)
def test_init_refuses_to_overwrite_any_discovered_config(name: str, tmp_path: Path, capsys):
    """All three discovery names, not just the one it would write. Writing
    `.dbt-costgate.yml` beside an existing `.dbt-costgate.yaml` leaves two configs
    with load order quietly picking one."""
    existing = tmp_path / name
    existing.write_text("fail_on: never\n", "utf-8")

    assert main(["init", "--project-dir", str(tmp_path)]) == 2
    assert existing.read_text("utf-8") == "fail_on: never\n"
    assert name in capsys.readouterr().err


def test_init_rejects_a_project_dir_that_does_not_exist(tmp_path: Path, capsys):
    missing = tmp_path / "nope"
    assert main(["init", "--project-dir", str(missing)]) == 2
    assert not missing.exists()
    assert "does not exist" in capsys.readouterr().err
