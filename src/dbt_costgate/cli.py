# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for dbt-costgate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dbt_costgate import __version__, against, artifacts, estimate, gitdiff, policy, report
from dbt_costgate.against import AgainstError
from dbt_costgate.artifacts import ArtifactError
from dbt_costgate.bigquery import BigQueryDryRunner, DryRunner
from dbt_costgate.config import CONFIG_REFERENCE, Config
from dbt_costgate.gitdiff import GitDiffError
from dbt_costgate.models import PricingDisclosure, Report
from dbt_costgate.pricing import PricingTable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbt-costgate",
        description="BigQuery cost gate for dbt pull requests.",
    )
    parser.add_argument("--version", action="version", version=f"dbt-costgate {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser(
        "check",
        help="Estimate the BigQuery cost impact of changed dbt models.",
        description=(
            "Dry-run the models this change touches and report their scan cost. "
            "With --baseline (or --against <ref>, which auto-compiles that ref in "
            "an isolated worktree), reports the before/after diff and can gate on "
            "thresholds. Without one (local default), reports each changed model's "
            "current scan cost. Selection: --select wins, else the baseline diff, "
            "else `git diff` vs main. Detects added and body-modified models, plus "
            "config-only and macro-only changes that alter a model's compiled SQL."
        ),
    )
    check.add_argument(
        "--current",
        default="target",
        help="Path to the compiled dbt target/ dir or manifest.json (default: ./target).",
    )
    check.add_argument(
        "--project-dir",
        help=(
            "Directory containing dbt_project.yml. Overrides the location inferred "
            "from --current; use when your compiled target isn't at <project>/target."
        ),
    )
    check.add_argument(
        "--baseline",
        help="Path to the baseline (main, compiled the same way) manifest.json for a diff.",
    )
    check.add_argument(
        "--against",
        help=(
            "Git ref to auto-compile as the baseline in an isolated worktree "
            "(removes the manual --baseline step). Mutually exclusive with --baseline."
        ),
    )
    check.add_argument(
        "--baseline-target",
        help=(
            "Name of a baseline defined under `baselines:` in .dbt-costgate.yml "
            "(each is a manifest path or a git ref). Mutually exclusive with "
            "--baseline/--against; defaults to the config's default_baseline."
        ),
    )
    check.add_argument(
        "--select",
        help="Comma-separated model names to estimate (overrides change detection).",
    )
    check.add_argument(
        "--base", help="Git ref to diff against for local selection (default: main)."
    )
    check.add_argument(
        "--config", help="Path to a dbt-costgate config file (default: .dbt-costgate.yml)."
    )
    check.add_argument("--format", choices=["terminal", "markdown", "json"], help="Output format.")
    check.add_argument("--output", help="Write the report to this file instead of stdout.")
    check.add_argument("--threads", type=int, default=8, help="Parallel dry-runs (default: 8).")
    check.add_argument("--project", help="BigQuery project for dry-run jobs (default: ADC).")
    check.add_argument("--region", help="Force a pricing region (default: auto-detect).")
    check.add_argument("--usd-per-tib", type=float, help="Override the on-demand rate.")
    check.add_argument(
        "--currency",
        help="ISO 4217 code to label amounts with (default USD). Labels a rate you "
        "supplied; never converts.",
    )
    check.add_argument("--fail-on", choices=["never", "warn", "fail"], help="Gate strictness.")
    check.add_argument("--max-usd-per-run", type=float, help="Fail if $/run increase exceeds this.")
    check.add_argument("--max-pct", type=float, help="Fail if %% increase exceeds this.")
    check.add_argument(
        "--max-usd-per-month", type=float, help="Fail if $/month increase exceeds this."
    )
    check.add_argument(
        "--max-usd-total",
        type=float,
        help="Fail if a model's total $/run exceeds this (absolute cap, no baseline needed).",
    )
    check.add_argument(
        "--max-tib-total",
        type=float,
        help="Fail if a model's total TiB/run exceeds this (absolute cap, no baseline needed).",
    )

    cfg = sub.add_parser(
        "config",
        help="List every .dbt-costgate.yml key with a plain-English explanation.",
        description=(
            "Print the full configuration reference — every key dbt-costgate reads "
            "from .dbt-costgate.yml, its type, default, and what it does. Use "
            "--format json for a machine-readable list."
        ),
    )
    cfg.add_argument("--format", choices=["terminal", "json"], default="terminal")

    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.region is not None:
        config.region = args.region
    # NB: --usd-per-tib is *not* merged into config here. It is a CLI-level flat
    # override that must outrank the config file's pricing.regions map, so it is
    # threaded to PricingTable separately (see run_check). config.usd_per_tib
    # therefore stays the file-level global override.
    if args.format is not None:
        config.report_format = args.format
    if args.fail_on is not None:
        config.fail_on = args.fail_on
    if args.max_usd_per_run is not None:
        config.thresholds.max_usd_increase_per_run = args.max_usd_per_run
    if args.max_pct is not None:
        config.thresholds.max_pct_increase = args.max_pct
    if args.max_usd_per_month is not None:
        config.thresholds.max_usd_increase_per_month = args.max_usd_per_month
    # Explicit `is not None`: a 0 cap (zero-tolerance) must survive 0.0's falsiness.
    if args.max_usd_total is not None:
        config.thresholds.max_usd_total = args.max_usd_total
    if args.max_tib_total is not None:
        config.thresholds.max_tib_total = args.max_tib_total
    return config


_SOURCE_LABELS = {
    "user-override": "user override",
    "region-table": "built-in table",
    "default-fallback": "default fallback",
}


def _aggregate_source(sources: set[str]) -> str:
    """Name every provenance present, so a mixed report says so honestly."""
    present = [_SOURCE_LABELS[s] for s in _SOURCE_LABELS if s in sources]
    return " + ".join(present) or "built-in table"


def _build_disclosure(table: PricingTable, deltas) -> PricingDisclosure:
    regions: dict[str, float] = {}
    region_sources: dict[str, str] = {}
    seen = deltas or []
    region_names = {d.region for d in seen} or {table.override_region or "US"}
    for name in sorted(region_names):
        rate = table.rate_for(name)
        regions[rate.region] = rate.usd_per_tib
        region_sources[rate.region] = rate.source
    return PricingDisclosure(
        regions=regions,
        source=_aggregate_source(set(region_sources.values())),
        table_version=table.version,
        last_verified=table.last_verified,
        region_sources=region_sources,
        currency=table.currency,
    )


def _select(
    args, current_nodes, baseline_nodes, project_dir, renames=None, macros=None
) -> list[str]:
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        return [uid for uid, node in current_nodes.items() if node.name in wanted or uid in wanted]
    if baseline_nodes is not None:
        return artifacts.select_changed(baseline_nodes, current_nodes, renames)
    paths = gitdiff.changed_paths(project_dir, args.base)
    if artifacts.touches_project_config(paths):
        print(
            "dbt-costgate: dbt_project.yml changed — project-wide config can't be traced "
            "to individual models; price the affected ones with --select.",
            file=sys.stderr,
        )
    return artifacts.select_by_paths(current_nodes, paths, macros)


class _UsageError(Exception):
    """A CLI/config misuse that should exit 2 with a clean message, not a traceback."""


def _resolve_baseline(args: argparse.Namespace, config: Config) -> tuple[str | None, str | None]:
    """Resolve the effective baseline source to (manifest_path, against_ref) — at most
    one set. Precedence: explicit --baseline/--against > --baseline-target > config
    default_baseline > none (local mode). Raises _UsageError on misuse."""
    explicit = [f for f in (args.baseline, args.against, args.baseline_target) if f]
    if len(explicit) > 1:
        raise _UsageError("use only one of --baseline, --against, or --baseline-target.")
    if args.baseline:
        return args.baseline, None
    if args.against:
        return None, args.against
    name = args.baseline_target or config.default_baseline
    if not name:
        return None, None
    target = config.baselines.get(name)
    if target is None:
        known = ", ".join(sorted(config.baselines)) or "none"
        raise _UsageError(
            f"baseline target {name!r} is not defined under `baselines:` in "
            f".dbt-costgate.yml (defined: {known})."
        )
    if bool(target.manifest) == bool(target.against):
        raise _UsageError(
            f"baseline target {name!r} needs exactly one of `manifest:` or `against:`."
        )
    return target.manifest, target.against


def run_check(args: argparse.Namespace, runner: DryRunner | None = None) -> int:
    current_arg = Path(args.current)
    target_dir = current_arg if current_arg.is_dir() else current_arg.parent
    project_dir = target_dir.parent if target_dir.name else Path.cwd()

    # --project-dir decouples "where the dbt project / git repo is" from where the
    # compiled --current manifest sits; validate up front so config discovery,
    # --against, and git selection all get a real directory.
    if args.project_dir:
        project_dir = Path(args.project_dir).resolve()
        if not project_dir.is_dir():
            print(
                f"dbt-costgate: --project-dir does not exist: {args.project_dir}",
                file=sys.stderr,
            )
            return policy.EXIT_OPERATIONAL

    config = Config.load(Path(args.config) if args.config else None, project_dir)
    config = _apply_overrides(config, args)

    # Resolve the baseline source: an explicit --baseline/--against, else a named
    # --baseline-target / config default_baseline (each a manifest path or a git ref).
    try:
        eff_baseline, eff_against = _resolve_baseline(args, config)
    except _UsageError as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

    try:
        current_manifest = artifacts.load_manifest(current_arg)
    except ArtifactError as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL
    current_nodes = artifacts.model_nodes(current_manifest)
    macros = artifacts.macro_index(current_manifest)

    baseline_nodes = None
    if eff_baseline:
        try:
            baseline_manifest = artifacts.load_manifest(Path(eff_baseline))
        except ArtifactError as exc:
            print(f"dbt-costgate: {exc}", file=sys.stderr)
            return policy.EXIT_OPERATIONAL
        baseline_nodes = artifacts.model_nodes(baseline_manifest)
    elif eff_against:
        # Pre-flight before the 5–30s worktree+compile: bail if the current side
        # can't be dry-run anyway.
        if not artifacts.has_any_compiled_code(current_nodes):
            print(
                "dbt-costgate: no compiled SQL in the current target — run `dbt compile` "
                "on your branch before --against <ref>.",
                file=sys.stderr,
            )
            return policy.EXIT_OPERATIONAL
        try:
            baseline_nodes = against.compiled_baseline(eff_against, project_dir)
        except AgainstError as exc:
            print(f"dbt-costgate: {exc}", file=sys.stderr)
            return policy.EXIT_OPERATIONAL

    if baseline_nodes is not None and not artifacts.has_any_compiled_code(baseline_nodes):
        print(
            "dbt-costgate: the baseline manifest has no compiled SQL — it must be "
            "produced by `dbt compile` (a `dbt parse` manifest won't work).",
            file=sys.stderr,
        )
        return policy.EXIT_OPERATIONAL

    # Resolve the rename map (current -> baseline) once the manifests are loaded.
    # Only meaningful with a baseline; a bad entry fails loudly rather than mis-diffing.
    renames: dict[str, str] = {}
    if baseline_nodes is not None and config.renames:
        try:
            renames = artifacts.resolve_renames(config.renames, current_nodes, baseline_nodes)
        except ArtifactError as exc:
            print(f"dbt-costgate: {exc}", file=sys.stderr)
            return policy.EXIT_OPERATIONAL

    try:
        selected = _select(args, current_nodes, baseline_nodes, project_dir, renames, macros)
    except GitDiffError as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

    # Models the diff picked up from their compiled SQL alone; each carries a
    # warning saying so, since the change is upstream of the model's own file.
    indirect: set[str] = set()
    if baseline_nodes is not None and not args.select:
        indirect = artifacts.indirect_changes(baseline_nodes, current_nodes)

    diff_mode = baseline_nodes is not None
    if runner is None:
        runner = BigQueryDryRunner(project=args.project)

    estimates = estimate.estimate_models(
        selected,
        current_nodes,
        baseline_nodes or {},
        runner,
        current_dir=target_dir,
        diff_mode=diff_mode,
        threads=args.threads,
        renames=renames,
        indirect=indirect,
    )

    if selected and estimate.has_only_operational_failures(estimates):
        print(
            "dbt-costgate: could not estimate any model (check credentials/permissions). "
            "Try `gcloud auth application-default login`.",
            file=sys.stderr,
        )
        return policy.EXIT_OPERATIONAL

    table = PricingTable.load(
        cli_override_usd_per_tib=args.usd_per_tib,
        override_regions=config.pricing_regions,
        override_usd_per_tib=config.usd_per_tib,
        override_region=config.region,
        currency=args.currency or config.currency,
    )
    deltas = estimate.build_deltas(estimates, table, config)
    disclosure = _build_disclosure(table, deltas)

    # A non-default currency describes a rate the user supplied. If any applied
    # rate still came from the bundled (USD) table, labelling it otherwise would
    # be silently wrong, so refuse rather than mislabel.
    problem = table.currency_is_sound(disclosure.region_sources)
    if problem:
        raise _UsageError(problem)

    verdict = policy.evaluate(deltas, config, currency=table.currency)
    # Advisory notes about the configuration itself — a setting that cannot do
    # what it looks like it does. Collected after the verdict to make it plain
    # they are not inputs to it.
    notices = [
        n
        for n in (
            policy.unpriced_threshold_notice(config.thresholds, disclosure.priced),
            table.fallback_notice(disclosure.region_sources),
        )
        if n
    ]
    rep = Report(
        deltas=deltas,
        disclosure=disclosure,
        verdict=verdict,
        mode="diff" if diff_mode else "absolute",
        notices=notices,
    )

    rendered = report.render(rep, config.report_format)
    if args.output:
        Path(args.output).write_text(rendered + "\n", "utf-8")
    else:
        print(rendered)
    return verdict.exit_code


def run_config(args: argparse.Namespace) -> int:
    if args.format == "json":
        payload = [
            {"key": f.key, "type": f.type_label, "default": f.default, "help": f.help}
            for f in CONFIG_REFERENCE
        ]
        print(json.dumps(payload, indent=2))
        return 0

    key_w = max(len(f.key) for f in CONFIG_REFERENCE)
    type_w = max(len(f.type_label) for f in CONFIG_REFERENCE)
    print("dbt-costgate configuration keys (.dbt-costgate.yml). CLI flags override the file.\n")
    for f in CONFIG_REFERENCE:
        default = "none" if f.default is None else str(f.default)
        print(f"{f.key.ljust(key_w)}  {f.type_label.ljust(type_w)}  default: {default}")
        print(f"    {f.help}")
    return 0


def main(argv: list[str] | None = None, runner: DryRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args, runner=runner)
    if args.command == "config":
        return run_config(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
