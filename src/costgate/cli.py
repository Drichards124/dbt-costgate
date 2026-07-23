# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for costgate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from costgate import __version__, artifacts, estimate, gitdiff, policy, report
from costgate.artifacts import ArtifactError
from costgate.bigquery import BigQueryDryRunner, DryRunner
from costgate.config import Config
from costgate.gitdiff import GitDiffError
from costgate.models import PricingDisclosure, Report
from costgate.pricing import PricingTable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="costgate",
        description="BigQuery cost gate for dbt pull requests.",
    )
    parser.add_argument("--version", action="version", version=f"costgate {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser(
        "check",
        help="Estimate the BigQuery cost impact of changed dbt models.",
        description=(
            "Dry-run the models this change touches and report their scan cost. "
            "With --baseline, reports the before/after diff and can gate on "
            "thresholds. Without it (local default), reports each changed model's "
            "current scan cost. Selection: --select wins, else --baseline diff, "
            "else `git diff` vs main. Detects added and body-modified models; "
            "config-only and macro-only changes are not yet detected."
        ),
    )
    check.add_argument(
        "--current",
        default="target",
        help="Path to the compiled dbt target/ dir or manifest.json (default: ./target).",
    )
    check.add_argument(
        "--baseline",
        help="Path to the baseline (main, compiled the same way) manifest.json for a diff.",
    )
    check.add_argument(
        "--select",
        help="Comma-separated model names to estimate (overrides change detection).",
    )
    check.add_argument(
        "--base", help="Git ref to diff against for local selection (default: main)."
    )
    check.add_argument("--config", help="Path to a costgate config file (default: .costgate.yml).")
    check.add_argument("--format", choices=["terminal", "markdown", "json"], help="Output format.")
    check.add_argument("--output", help="Write the report to this file instead of stdout.")
    check.add_argument("--threads", type=int, default=8, help="Parallel dry-runs (default: 8).")
    check.add_argument("--project", help="BigQuery project for dry-run jobs (default: ADC).")
    check.add_argument("--region", help="Force a pricing region (default: auto-detect).")
    check.add_argument("--usd-per-tib", type=float, help="Override the on-demand rate.")
    check.add_argument("--fail-on", choices=["never", "warn", "fail"], help="Gate strictness.")
    check.add_argument("--max-usd-per-run", type=float, help="Fail if $/run increase exceeds this.")
    check.add_argument("--max-pct", type=float, help="Fail if %% increase exceeds this.")
    check.add_argument(
        "--max-usd-per-month", type=float, help="Fail if $/month increase exceeds this."
    )
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.region is not None:
        config.region = args.region
    if args.usd_per_tib is not None:
        config.usd_per_tib = args.usd_per_tib
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
    return config


def _build_disclosure(table: PricingTable, deltas) -> PricingDisclosure:
    regions: dict[str, float] = {}
    sources = set()
    seen = deltas or []
    region_names = {d.region for d in seen} or {table.override_region or "US"}
    for name in sorted(region_names):
        rate = table.rate_for(name)
        regions[rate.region] = rate.usd_per_tib
        sources.add(rate.source)
    if "user-override" in sources:
        source = "user override"
    elif "default-fallback" in sources:
        source = "built-in table + default fallback"
    else:
        source = "built-in table"
    return PricingDisclosure(
        regions=regions,
        source=source,
        table_version=table.version,
        last_verified=table.last_verified,
    )


def _select(args, current_nodes, baseline_nodes, project_dir) -> list[str]:
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        return [uid for uid, node in current_nodes.items() if node.name in wanted or uid in wanted]
    if baseline_nodes is not None:
        return artifacts.select_changed(baseline_nodes, current_nodes)
    return gitdiff.select_by_git(current_nodes, project_dir, args.base)


def run_check(args: argparse.Namespace, runner: DryRunner | None = None) -> int:
    current_arg = Path(args.current)
    target_dir = current_arg if current_arg.is_dir() else current_arg.parent
    project_dir = target_dir.parent if target_dir.name else Path.cwd()

    config = Config.load(Path(args.config) if args.config else None, project_dir)
    config = _apply_overrides(config, args)

    try:
        current_manifest = artifacts.load_manifest(current_arg)
    except ArtifactError as exc:
        print(f"costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL
    current_nodes = artifacts.model_nodes(current_manifest)

    baseline_nodes = None
    if args.baseline:
        try:
            baseline_manifest = artifacts.load_manifest(Path(args.baseline))
        except ArtifactError as exc:
            print(f"costgate: {exc}", file=sys.stderr)
            return policy.EXIT_OPERATIONAL
        baseline_nodes = artifacts.model_nodes(baseline_manifest)
        if not artifacts.has_any_compiled_code(baseline_nodes):
            print(
                "costgate: the baseline manifest has no compiled SQL — it must be "
                "produced by `dbt compile` (a `dbt parse` manifest won't work).",
                file=sys.stderr,
            )
            return policy.EXIT_OPERATIONAL

    try:
        selected = _select(args, current_nodes, baseline_nodes, project_dir)
    except GitDiffError as exc:
        print(f"costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

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
    )

    if selected and estimate.has_only_operational_failures(estimates):
        print(
            "costgate: could not estimate any model (check credentials/permissions). "
            "Try `gcloud auth application-default login`.",
            file=sys.stderr,
        )
        return policy.EXIT_OPERATIONAL

    table = PricingTable.load(
        override_usd_per_tib=config.usd_per_tib, override_region=config.region
    )
    deltas = estimate.build_deltas(estimates, table, config)
    verdict = policy.evaluate(deltas, config)
    rep = Report(
        deltas=deltas,
        disclosure=_build_disclosure(table, deltas),
        verdict=verdict,
        mode="diff" if diff_mode else "absolute",
    )

    rendered = report.render(rep, config.report_format)
    if args.output:
        Path(args.output).write_text(rendered + "\n", "utf-8")
    else:
        print(rendered)
    return verdict.exit_code


def main(argv: list[str] | None = None, runner: DryRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args, runner=runner)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
