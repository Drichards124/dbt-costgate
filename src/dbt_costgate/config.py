# SPDX-License-Identifier: Apache-2.0
"""`.dbt-costgate.yml` loading and merge with CLI overrides (CLI always wins)."""

from __future__ import annotations

import difflib
import textwrap
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """A `.dbt-costgate.yml` the tool will not act on, reported as exit 2.

    Every way of getting the file wrong used to escape as whatever Python raised
    — `ValueError` for a threshold written as prose, `AttributeError` for a list
    where a mapping belongs, a yaml scanner error for a stray colon — and an
    uncaught exception exits 1. ADR-0008 reserves 1 for "a threshold was
    breached", so CI told the team a typo was a cost regression and blamed the
    author for it.

    One type, raised at every such point, so `cli` has a single thing to catch
    and every config mistake lands on the documented exit 2 with a message that
    names the file.
    """


@dataclass
class Thresholds:
    max_usd_increase_per_run: float | None = None
    max_pct_increase: float | None = None
    max_usd_increase_per_month: float | None = None
    # Absolute per-run ceilings: gate a model's total cost/scan, not its increase.
    # No baseline needed, so they also gate local (zero-setup) runs.
    max_usd_total: float | None = None
    max_tib_total: float | None = None

    @property
    def any_set(self) -> bool:
        return any(
            v is not None
            for v in (
                self.max_usd_increase_per_run,
                self.max_pct_increase,
                self.max_usd_increase_per_month,
                self.max_usd_total,
                self.max_tib_total,
            )
        )


@dataclass
class BaselineTarget:
    """A named baseline source: exactly one of a prebuilt ``manifest`` path or a git
    ``against`` ref to compile. The one-of rule is validated where the target is
    selected (cli), so an unused malformed entry never aborts an unrelated run."""

    manifest: str | None = None
    against: str | None = None


@dataclass
class Config:
    region: str | None = None
    usd_per_tib: float | None = None
    pricing_regions: dict[str, float] = field(default_factory=dict)
    currency: str | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    run_frequency_default: int | None = None
    run_frequency_models: dict[str, int] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    warn_only: list[str] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    baselines: dict[str, BaselineTarget] = field(default_factory=dict)
    default_baseline: str | None = None
    report_format: str = "terminal"
    fail_on: str = "fail"  # never | warn | fail
    silence_notices: list[str] = field(default_factory=list)

    DEFAULT_FILENAMES = (".dbt-costgate.yml", ".dbt-costgate.yaml", "dbt-costgate.yml")

    @classmethod
    def load(cls, path: Path | None, project_dir: Path) -> Config:
        """Load from an explicit path, else the first default filename found in
        the project directory. A missing file is fine — defaults apply."""
        cfg_path = path
        if cfg_path is None:
            for name in cls.DEFAULT_FILENAMES:
                candidate = project_dir / name
                if candidate.is_file():
                    cfg_path = candidate
                    break
        if cfg_path is None or not cfg_path.is_file():
            return cls()
        try:
            raw = yaml.safe_load(cfg_path.read_text("utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{cfg_path} is not valid YAML: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{cfg_path} could not be read: {exc}") from exc
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{cfg_path}: expected a mapping of settings at the top level, "
                f"got {type(raw).__name__}. Run `dbt-costgate config` for the key list."
            )
        try:
            return cls._from_dict(raw)
        except ConfigError as exc:
            # Re-raised with the path in front: the parser knows which key is
            # wrong and not which of several discoverable files it came from.
            raise ConfigError(f"{cfg_path}: {exc}") from exc
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from exc

    @classmethod
    def _from_dict(cls, raw: dict) -> Config:
        _reject_unknown(raw)
        pricing = _section(raw, "pricing")
        thr = _section(raw, "thresholds")
        freq = _section(raw, "run_frequency")
        report = _section(raw, "report")
        notices = _section(raw, "notices")
        return cls(
            region=pricing.get("region"),
            usd_per_tib=_opt_float(pricing.get("usd_per_tib"), "pricing.usd_per_tib"),
            pricing_regions=_region_rates(pricing.get("regions")),
            currency=_opt_currency(pricing.get("currency")),
            thresholds=Thresholds(
                **{
                    name: _opt_float(thr.get(name), f"thresholds.{name}")
                    for name in _THRESHOLD_KEYS
                }
            ),
            run_frequency_default=_opt_int(freq.get("default"), "run_frequency.default"),
            run_frequency_models={
                str(k): _req_int(v, f"run_frequency.models[{k!r}]")
                for k, v in _mapping(freq.get("models"), "run_frequency.models").items()
            },
            exclude=_as_list(raw.get("exclude"), "exclude"),
            warn_only=_as_list(raw.get("warn_only"), "warn_only"),
            renames={str(k): str(v) for k, v in _mapping(raw.get("renames"), "renames").items()},
            baselines=_baseline_targets(raw.get("baselines")),
            default_baseline=raw.get("default_baseline"),
            report_format=_one_of(
                report.get("format"), _REPORT_FORMATS, "report.format", "terminal"
            ),
            fail_on=_one_of(raw.get("fail_on"), _FAIL_ON, "fail_on", "fail"),
            silence_notices=_as_list(notices.get("silence"), "notices.silence"),
        )

    def runs_per_month(self, model_name: str) -> int | None:
        return self.run_frequency_models.get(model_name, self.run_frequency_default)


# The allowed values for the two enum-ish settings. argparse validates the
# equivalent flags from `choices=`; the file had nothing checking it at all, so
# `report.format: markdwn` printed terminal output and said nothing, and
# `fail_on: no` — YAML for the boolean False — matched neither "never" nor "warn"
# and fell through to the strictest setting a user could have chosen.
_FAIL_ON = ("never", "warn", "fail")
_REPORT_FORMATS = ("terminal", "markdown", "json")

# Derived, not written out again: a threshold added to the dataclass and not to a
# hand-kept list here would parse as absent, which is a threshold the user
# configured and the gate never applied.
_THRESHOLD_KEYS = tuple(f.name for f in fields(Thresholds))


def _reject_unknown(raw: dict) -> None:
    """Refuse a key `CONFIG_REFERENCE` does not describe.

    An unknown key used to be dropped in silence, so `thresholds.max_usd_totl`
    left the gate with no threshold at all while the user believed they had
    configured one — the failure mode a cost gate can least afford, because
    nothing about the run looks wrong.

    Checked one level deep. A key that heads a documented section
    (`pricing`, `thresholds`, …) has its children checked; anything else is
    matched whole. That is what keeps the values of an open map — a region name
    under `pricing.regions`, a model name under `renames` — as the user data they
    are rather than settings to be recognised.
    """
    known = {f.key for f in CONFIG_REFERENCE}
    sections = {key.rpartition(".")[0] for key in known if "." in key}
    for key, value in raw.items():
        name = str(key)
        if name in sections and isinstance(value, dict):
            for child in value:
                _reject_one(f"{name}.{child}", known)
        elif name not in sections:
            _reject_one(name, known)


def _reject_one(key: str, known: set[str]) -> None:
    if key in known:
        return
    close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
    hint = f" Did you mean `{close[0]}`?" if close else ""
    raise ConfigError(
        f"unknown setting `{key}`.{hint} Run `dbt-costgate config` for every key it accepts."
    )


def _section(raw: dict, key: str) -> dict:
    return _mapping(raw.get(key), key)


def _mapping(value, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{key}: expected a mapping of settings, got {type(value).__name__}. "
            f"Its entries belong on indented lines under `{key}:`."
        )
    return value


def _as_list(raw, key: str) -> list[str]:
    """A list of names, accepting a bare scalar as a one-item list.

    A string is iterable, so `list("dim_orders")` is a list of eleven single
    characters — which matched no model, leaving `exclude: dim_orders` doing
    nothing and `warn_only: dim_orders` blocking the build the user had asked it
    only to warn about.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        raise ConfigError(f"{key}: expected a list of names, got a mapping.")
    if isinstance(raw, (str, int, float, bool)):
        return [str(raw)]
    return [str(item) for item in raw]


def _one_of(raw, allowed: tuple[str, ...], key: str, default: str) -> str:
    if raw is None:
        return default
    if str(raw) in allowed:
        return str(raw)
    note = ""
    if isinstance(raw, bool):
        # `no`, `yes`, `on` and `off` are all YAML booleans when unquoted, so they
        # arrive here as True/False and never match a setting name. Worth naming,
        # because the value the user typed is not the value in the error.
        note = " — YAML reads an unquoted yes/no/on/off as a boolean, not as a word"
    raise ConfigError(f"{key}: expected one of {', '.join(allowed)}, got {raw!r}{note}.")


def _opt_currency(raw) -> str | None:
    """Parse `pricing.currency` as an ISO 4217 code.

    Validated as three letters rather than checked against a list of codes: a
    hard-coded list would reject valid codes as they change, and the point of the
    check is to catch `pricing.currency: "$"` or `Euro`, not to police ISO's
    registry. Stored upper-case so the report label is consistent.
    """
    if raw is None:
        return None
    code = str(raw).strip().upper()
    if not (len(code) == 3 and code.isalpha()):
        raise ConfigError(
            f"pricing.currency: expected a three-letter ISO 4217 code such as USD or EUR, "
            f"got {raw!r}"
        )
    return code


def _region_rates(raw) -> dict[str, float]:
    """Parse a `pricing.regions` map. A rate may be 0 (flat-rate slots), but a
    negative rate is nonsense and would silently subtract cost."""
    rates: dict[str, float] = {}
    for region, value in _mapping(raw, "pricing.regions").items():
        key = f"pricing.regions[{region!r}]"
        rate = _req_float(value, key)
        if rate < 0:
            raise ConfigError(f"{key}: rate must be >= 0, got {rate}")
        rates[region] = rate
    return rates


def _baseline_targets(raw) -> dict[str, BaselineTarget]:
    """Parse a `baselines:` map (name -> {manifest|against}). Non-dict entries
    become empty targets; the cli reports the one-of violation when selected."""
    out: dict[str, BaselineTarget] = {}
    for name, spec in _mapping(raw, "baselines").items():
        spec = spec if isinstance(spec, dict) else {}
        out[str(name)] = BaselineTarget(manifest=spec.get("manifest"), against=spec.get("against"))
    return out


def _opt_float(v, key: str) -> float | None:
    return None if v is None else _req_float(v, key)


def _opt_int(v, key: str) -> int | None:
    return None if v is None else _req_int(v, key)


def _req_float(v, key: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: expected a number, got {v!r}") from exc


def _req_int(v, key: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: expected a whole number, got {v!r}") from exc


@dataclass(frozen=True)
class ConfigField:
    """One documented `.dbt-costgate.yml` key. The single source of truth behind the
    `dbt-costgate config` command; `attr` ties it to the Config attribute it fills so
    a test can prove the registry and the parser never drift apart."""

    key: str  # dotted YAML path, e.g. "pricing.regions"
    attr: str  # dotted Config attribute it populates (for the drift test)
    type_label: str  # human type hint, e.g. "map[str->float]"
    default: Any  # the literal parser default (native value, for a clean JSON contract)
    help: str  # plain-English explanation; notes the *effective* default when it differs
    # An illustrative value, as YAML. Scalars are written inline after the key;
    # `map[...]`/`list[...]` entries are written as an indented block under it,
    # decided from type_label rather than by inspecting the string. This is what
    # `dbt-costgate init` puts in the starter file — deliberately an example, never
    # the default, so a key whose default is also a valid setting (report.format,
    # fail_on) shows something that visibly differs from doing nothing.
    example: str = ""


# Every key `Config._from_dict` understands, described once. Keep this in lockstep
# with the dataclass — `tests/test_config.py` enforces it in both directions.
CONFIG_REFERENCE: list[ConfigField] = [
    ConfigField(
        "pricing.region",
        "region",
        "str",
        None,
        "Force the pricing region. Default: auto-detected from the dry-run job "
        "location, falling back to US.",
        example="europe-west3",
    ),
    ConfigField(
        "pricing.usd_per_tib",
        "usd_per_tib",
        "float",
        None,
        "Flat on-demand rate override (USD/TiB) for every region. Default: the "
        "built-in per-region rate table.",
        example="5.00",
    ),
    ConfigField(
        "pricing.currency",
        "currency",
        "str",
        None,
        "ISO 4217 code the reported amounts are labelled with, e.g. EUR. Default: "
        "USD, matching the built-in table. This labels a rate you supplied "
        "yourself — dbt-costgate never converts between currencies — so any "
        "region still priced from the built-in table is an error, not a "
        "conversion.",
        example="EUR",
    ),
    ConfigField(
        "pricing.regions",
        "pricing_regions",
        "map[str->float]",
        {},
        "Per-region rate overrides (region -> USD/TiB) that patch the built-in "
        "table. Keys match case-insensitively; 0 is allowed. Unlisted regions "
        "use the table.",
        example="europe-west3: 4.80\nUS: 6.00",
    ),
    ConfigField(
        "thresholds.max_usd_increase_per_run",
        "thresholds.max_usd_increase_per_run",
        "float",
        None,
        "Gate fails if a model's per-run cost increase exceeds this many USD.",
        example="5.00",
    ),
    ConfigField(
        "thresholds.max_pct_increase",
        "thresholds.max_pct_increase",
        "float",
        None,
        "Gate fails if a model's cost increases by more than this percent.",
        example="25",
    ),
    ConfigField(
        "thresholds.max_usd_increase_per_month",
        "thresholds.max_usd_increase_per_month",
        "float",
        None,
        "Gate fails if a model's projected monthly cost increase exceeds this many USD.",
        example="100.00",
    ),
    ConfigField(
        "thresholds.max_usd_total",
        "thresholds.max_usd_total",
        "float",
        None,
        "Absolute ceiling: gate fails if a model's total per-run cost exceeds this "
        "many USD, regardless of its increase. Needs no baseline (works in local mode).",
        example="20.00",
    ),
    ConfigField(
        "thresholds.max_tib_total",
        "thresholds.max_tib_total",
        "float",
        None,
        "Absolute ceiling: gate fails if a model's total per-run scan exceeds this "
        "many TiB, regardless of its increase. Needs no baseline (works in local mode).",
        example="3.00",
    ),
    ConfigField(
        "run_frequency.default",
        "run_frequency_default",
        "int",
        None,
        "Assumed runs per month for the monthly-cost estimate, for models "
        "without an explicit entry.",
        example="30",
    ),
    ConfigField(
        "run_frequency.models",
        "run_frequency_models",
        "map[str->int]",
        {},
        "Per-model runs-per-month overrides (model name -> runs) for the monthly estimate.",
        example="fct_orders_daily: 24",
    ),
    ConfigField(
        "exclude",
        "exclude",
        "list[str]",
        [],
        "Model names reported but never gated.",
        example="- events_partitioned",
    ),
    ConfigField(
        "warn_only",
        "warn_only",
        "list[str]",
        [],
        "Model names shown as a warning instead of gated.",
        example="- sessions_rolling",
    ),
    ConfigField(
        "renames",
        "renames",
        "map[str->str]",
        {},
        "Pair a renamed model to its baseline for a diff (current -> baseline), for "
        "when a model rename changes its unique_id and auto-matching can't. Each side "
        "is a model name or a full unique_id. Requires a baseline (diff mode).",
        example="fct_orders_daily: fct_orders_monthly",
    ),
    ConfigField(
        "baselines",
        "baselines",
        "map[str->{manifest|against}]",
        {},
        "Named baseline sources (dbt --target analogy). Each name maps to either a "
        "`manifest:` path or an `against:` git ref. Select one with --baseline-target "
        "<name>; a `manifest` target travels to CI, an `against` target needs git+dbt.",
        example="main:\n  against: main\nple:\n  manifest: artifacts/ple/manifest.json",
    ),
    ConfigField(
        "default_baseline",
        "default_baseline",
        "str",
        None,
        "Name of the `baselines:` entry to use when no --baseline/--against/"
        "--baseline-target is given, so `dbt-costgate check` diffs without a flag.",
        example="main",
    ),
    ConfigField(
        "report.format",
        "report_format",
        "terminal|markdown|json",
        "terminal",
        "Output format when not overridden by --format.",
        example="markdown",
    ),
    ConfigField(
        "fail_on",
        "fail_on",
        "never|warn|fail",
        "fail",
        "Gate strictness. 'never' reports breaches but always exits 0. 'fail' "
        "(the default) and 'warn' both exit 1 on a breach; they differ only in "
        "the label the report prints, FAIL or WARN. Warnings themselves are "
        "never an input to the gate.",
        example="warn",
    ),
    ConfigField(
        "notices.silence",
        "silence_notices",
        "list[str]",
        [],
        "Ids of advisory notices to stop reporting, e.g. dead-money-thresholds "
        "on a team that has deliberately priced at 0. Each report prints a "
        "notice's id beside it, and `dbt-costgate config` lists them. Silencing "
        "is per-notice on purpose: there is no blanket off-switch, so turning "
        "one off can never hide a different one you have not seen. An unknown "
        "id is an error, not a no-op.",
        example="- dead-money-thresholds",
    ),
]


_TEMPLATE_PREAMBLE = """\
# .dbt-costgate.yml — written by `dbt-costgate init`.
#
# Every setting below is commented out, so this file changes nothing until you
# uncomment one. Section headers are left live, so switching a setting on is a
# one-line edit.
#
# The values shown are illustrations, not defaults. Each key's real default is
# in the note above it, and `dbt-costgate config` prints the same reference
# at any time.
#
# Commit this file. `dbt-costgate check` reads it identically on your machine
# and in CI, so there is nothing separate to configure for a PR run.
---
"""

# yamllint's default profile caps lines at 80, and this file is handed to people
# who may well lint it. The budget is the whole rendered line: the indent, the
# "# " that comments it out, and the text.
_WIDTH = 80


def render_config_template(*, commented: bool = True) -> str:
    """The starter `.dbt-costgate.yml`, generated from `CONFIG_REFERENCE`.

    Generated rather than written, for the reason the config table in the docs is:
    a hand-maintained template has to be edited every time a key is added, by
    someone who has just finished adding the key. Here that failure is quiet —
    the file simply never mentions the setting.

    `commented=False` renders the same content live. Nothing ships that form; it
    exists so a test can prove every `example` is YAML the parser actually
    accepts, which reading the commented file cannot establish.
    """
    lines = [_TEMPLATE_PREAMBLE.rstrip("\n")] if commented else []
    section: str | None = None
    prefix = "# " if commented else ""

    first_in_section = True
    for f in CONFIG_REFERENCE:
        head, _, leaf = f.key.rpartition(".")
        if head != section:
            section = head
            first_in_section = True
            if head:
                lines += ["", f"{head}:"]
        indent = "  " if head else ""
        if not first_in_section or not head:
            lines.append("")
        first_in_section = False

        if commented:
            # break_on_hyphens: otherwise "dbt-costgate" wraps as "dbt-" / "costgate".
            for note in textwrap.wrap(
                " ".join(f.help.split()),
                width=_WIDTH - len(indent) - len(prefix),
                break_on_hyphens=False,
            ):
                lines.append(f"{indent}{prefix}{note}")

        # A map or a list has to sit in a block under its key; writing
        # `exclude: - events_partitioned` inline would not be YAML at all.
        if f.type_label.startswith(("map[", "list[")):
            lines.append(f"{indent}{prefix}{leaf}:")
            for value_line in f.example.splitlines():
                lines.append(f"{indent}{prefix}  {value_line}")
        else:
            lines.append(f"{indent}{prefix}{leaf}: {f.example}")

    return "\n".join(lines).lstrip("\n") + "\n"
