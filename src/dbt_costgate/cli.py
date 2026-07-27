# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for dbt-costgate."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dbt_costgate import (
    __version__,
    against,
    artifacts,
    estimate,
    gitdiff,
    layout,
    notices,
    policy,
    report,
)
from dbt_costgate.against import AgainstError
from dbt_costgate.artifacts import ArtifactError
from dbt_costgate.bigquery import BigQueryDryRunner, DryRunner
from dbt_costgate.config import (
    CONFIG_REFERENCE,
    Config,
    ConfigError,
    reject_negative,
    render_config_template,
    validate_numbers,
)
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
    check.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Colour the terminal report (default: auto — on at a terminal, off when "
        "piped or when NO_COLOR is set).",
    )
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

    init = sub.add_parser(
        "init",
        help="Write a starter .dbt-costgate.yml, with every setting commented out.",
        description=(
            "Create a .dbt-costgate.yml in the project directory, documenting every "
            "setting with an example value and leaving all of them commented out, so "
            "the file changes nothing until you uncomment something. Refuses to "
            "overwrite an existing config."
        ),
    )
    init.add_argument(
        "--project-dir",
        help="Directory to write the config into (default: the current directory).",
    )

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


def _log_error_details(estimates) -> None:
    """Print BigQuery's own message for each failed model — to stderr only.

    Reports name the failure by kind and stop there, because they become
    pull-request comments and BigQuery quotes the query it was given (SECURITY.md).
    stderr is the job log: same information, an access-controlled place to put it.
    """
    for est in estimates:
        if est.error_kind is not None and est.error_detail:
            print(
                f"dbt-costgate: {est.name}: {est.error_kind.value}: {est.error_detail}",
                file=sys.stderr,
            )


def _build_disclosure(table: PricingTable, deltas, config: Config) -> PricingDisclosure:
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
        free_tib_per_month=config.free_tib_per_month,
    )


def _validate_resolved_config(table: PricingTable, config: Config, disclosure) -> None:
    """Config errors only detectable once pricing has resolved. Raises _UsageError.

    Kept together so both have a caught raise site: the currency check used to
    raise from the middle of `run_check`, where nothing caught it, so a
    mislabelled currency produced a traceback instead of the documented exit 2.
    """
    # A non-default currency describes a rate the user supplied. If any applied
    # rate still came from the bundled (USD) table, labelling it otherwise would
    # be silently wrong, so refuse rather than mislabel.
    problem = table.currency_is_sound(disclosure.region_sources)
    if problem:
        raise _UsageError(problem)

    unknown = notices.unknown_ids(config.silence_notices)
    if unknown:
        raise _UsageError(
            f"notices.silence: unknown notice id(s) {', '.join(unknown)}. "
            f"Valid ids: {', '.join(notices.NOTICE_IDS)}."
        )


def _unmatched_selection(missing, current_nodes, out_of_scope: dict[str, str]) -> str:
    """The message for a `--select` name that selected nothing.

    A name can miss for two quite different reasons, and telling them apart is
    the whole value of the message: the model may not exist (a typo, or a stale
    list), or it may exist and be a kind dbt-costgate does not price. "No such
    model" for a snapshot the user can see in their project reads as a bug in the
    tool.
    """
    known = sorted(node.name for node in current_nodes.values())
    parts = []
    for name in missing:
        if name in out_of_scope:
            parts.append(f"{name} — {out_of_scope[name]}")
            continue
        close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        parts.append(f"{name} (did you mean {close[0]}?)" if close else f"{name} — no such model")
    return "--select matched nothing for: " + "; ".join(parts) + "."


@dataclass
class _Selection:
    """What `_select` worked out.

    `notes` explains, per unique_id, why a model was selected when its own file
    was not what changed. `paths` is the git diff the local path used, kept so
    the caller can say which out-of-scope nodes a change touched — and `None`
    when selection did not come from a diff at all.

    `unpriced` is the `--select` counterpart of that last part: names the user
    asked for by hand that exist but are not something dbt-costgate prices. The
    diff path learns them from `paths`, which `--select` does not have.
    """

    uids: list[str]
    notes: dict[str, str] = field(default_factory=dict)
    paths: list[str] | None = None
    unpriced: dict[str, str] = field(default_factory=dict)


def _select(
    args,
    current_nodes,
    baseline_nodes,
    project_dir,
    renames=None,
    macros=None,
    out_of_scope=None,
    ephemeral=None,
) -> _Selection:
    out_of_scope = out_of_scope or {}
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        selected = [
            uid for uid, node in current_nodes.items() if node.name in wanted or uid in wanted
        ]
        # A name that matched nothing is a usage error, not an empty selection.
        # It used to return `[]` in silence, so `--select` built from a script —
        # the pattern the docs recommend, piping `dbt ls --select state:modified`
        # — checked nothing at all the day that list went stale, and the pull
        # request went green.
        matched = set(selected) | {current_nodes[uid].name for uid in selected}
        missing = sorted(wanted - matched)
        # Two kinds of miss, and only one of them is the user's mistake. A name
        # nobody recognises is a typo or a stale list, and must stay loud. A name
        # that names a real seed, snapshot or ephemeral is the same script doing
        # its job — `dbt ls --resource-type model` emits ephemerals — and
        # discarding the whole report over it threw away every model that did
        # have an answer. The diff path has always reported those and carried on;
        # this makes --select agree with it.
        unknown = [name for name in missing if name not in out_of_scope]
        if unknown or not selected:
            raise _UsageError(_unmatched_selection(missing, current_nodes, out_of_scope))
        return _Selection(selected, unpriced={name: out_of_scope[name] for name in missing})
    if baseline_nodes is not None:
        return _Selection(artifacts.select_changed(baseline_nodes, current_nodes, renames))
    paths = gitdiff.changed_paths(project_dir, args.base)
    if artifacts.touches_project_config(paths):
        print(
            "dbt-costgate: dbt_project.yml changed — project-wide config can't be traced "
            "to individual models; price the affected ones with --select.",
            file=sys.stderr,
        )
    uids, notes = artifacts.select_by_paths(current_nodes, paths, macros, ephemeral)
    return _Selection(uids, notes, paths)


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

    try:
        config = Config.load(Path(args.config) if args.config else None, project_dir)
        config = _apply_overrides(config, args)
        # Re-run after the merge, not only inside `load`: a threshold given on the
        # command line never passes through the file's parser, so validating only
        # there would leave `--max-usd-total -20` a way around the rule. Named as
        # the flag rather than the config key, since that is what the user typed.
        validate_numbers(config)
        reject_negative("--usd-per-tib", args.usd_per_tib)
    except ConfigError as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

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
    adapter_problem = artifacts.adapter_problem(current_manifest)
    if adapter_problem:
        print(f"dbt-costgate: {adapter_problem}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

    current_nodes = artifacts.model_nodes(current_manifest)
    macros = artifacts.macro_index(current_manifest)
    out_of_scope = artifacts.out_of_scope_nodes(current_manifest)

    baseline_nodes = None
    if eff_baseline:
        try:
            baseline_manifest = artifacts.load_manifest(Path(eff_baseline), "--baseline")
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
        selection = _select(
            args,
            current_nodes,
            baseline_nodes,
            project_dir,
            renames,
            macros,
            out_of_scope,
            artifacts.ephemeral_index(current_manifest),
        )
    except (GitDiffError, _UsageError) as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL
    selected = selection.uids

    # Models the diff picked up from their compiled SQL alone; each carries a
    # note saying so, since the change is upstream of the model's own file.
    if baseline_nodes is not None and not args.select:
        for uid in artifacts.indirect_changes(baseline_nodes, current_nodes):
            selection.notes[uid] = (
                "compiled SQL changed but the model file didn't — an upstream macro "
                "or a config change"
            )

    # "Nothing to estimate" and "what you changed is not something I price" are
    # different answers and used to read identically. Said on every run rather
    # than only an empty one: a change that touches three models and a snapshot
    # has an unpriced snapshot in it either way, and a snapshot runs a MERGE.
    # stderr, so it never reaches a pull-request comment as if it were a figure.
    touched = artifacts.changed_out_of_scope(
        current_manifest,
        baseline_manifest if baseline_nodes is not None and eff_baseline else None,
        selection.paths,
    )
    # "not in the report" rather than "not priced", because the reasons say "not
    # priced" themselves: the pair read "country_codes changed but is not priced
    # — seeds are not priced", which states its one fact twice and reads like a
    # template nobody assembled. This half says what happened, the reason says
    # why. It is also the more accurate half for an ephemeral, whose cost really
    # is priced — in the rows of the models that inline it.
    for name, reason in sorted(touched.items()):
        print(f"dbt-costgate: {name} changed but is not in the report — {reason}.", file=sys.stderr)
    # The --select equivalent. Same stream and same reason: it belongs beside the
    # figures, not among them, so it cannot reach a pull-request comment looking
    # like one.
    for name, reason in sorted(selection.unpriced.items()):
        print(
            f"dbt-costgate: {name} was selected but is not in the report — {reason}.",
            file=sys.stderr,
        )

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
        notes=selection.notes,
    )

    _log_error_details(estimates)

    if selected and estimate.has_only_operational_failures(estimates):
        print(
            "dbt-costgate: could not estimate any model — each reason is above. "
            "BigQuery returns the same 403 whether the dry-run is not allowed or "
            "the dataset or project does not exist, so check those names too. "
            "If it is credentials: `gcloud auth application-default login`.",
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
    disclosure = _build_disclosure(table, deltas, config)

    try:
        _validate_resolved_config(table, config, disclosure)
    except _UsageError as exc:
        print(f"dbt-costgate: {exc}", file=sys.stderr)
        return policy.EXIT_OPERATIONAL

    verdict = policy.evaluate(deltas, config, currency=table.currency)
    # Advisory notes about the configuration itself — a setting that cannot do
    # what it looks like it does. Collected after the verdict to make it plain
    # they are not inputs to it.
    rep = Report(
        deltas=deltas,
        disclosure=disclosure,
        verdict=verdict,
        mode="diff" if diff_mode else "absolute",
        notices=notices.collect(config, table, disclosure, deltas),
    )

    # A report going to a file is not going to this terminal: it is rendered at a
    # fixed width with no escape sequences, so a committed or CI-captured report
    # never depends on the window that produced it.
    to_file = bool(args.output)
    rendered = report.render(
        rep,
        config.report_format,
        width=layout.DEFAULT_WIDTH if to_file else layout.terminal_width(sys.stdout),
        color=not to_file and layout.should_color(args.color, sys.stdout),
    )
    if to_file:
        try:
            Path(args.output).write_text(rendered + "\n", "utf-8")
        except OSError as exc:
            # Every dry-run succeeded and the report rendered; only the write
            # failed. That is the gate's own plumbing, not a cost regression, so
            # it exits 2 — unguarded it threw a completed run away as a traceback
            # and exit 1, which CI reads as "the author broke something".
            print(
                f"dbt-costgate: could not write the report to {args.output}: {exc}",
                file=sys.stderr,
            )
            return policy.EXIT_OPERATIONAL
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


def run_init(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve() if args.project_dir else Path.cwd()
    if not project_dir.is_dir():
        print(
            f"dbt-costgate: --project-dir does not exist: {args.project_dir}",
            file=sys.stderr,
        )
        return policy.EXIT_OPERATIONAL

    # Refuse on any discoverable name, not just the one about to be written.
    # Leaving two config files behind, with load order quietly picking one, is a
    # worse outcome than not writing anything.
    for name in Config.DEFAULT_FILENAMES:
        existing = project_dir / name
        if existing.is_file():
            print(
                f"dbt-costgate: {existing} already exists — not overwriting it.",
                file=sys.stderr,
            )
            return policy.EXIT_OPERATIONAL

    target = project_dir / Config.DEFAULT_FILENAMES[0]
    target.write_text(render_config_template(), "utf-8")
    print(f"Wrote {target}")
    print("Every setting is commented out; uncomment what you need. Nothing changes yet.")
    return 0


def main(argv: list[str] | None = None, runner: DryRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args, runner=runner)
    if args.command == "config":
        return run_config(args)
    if args.command == "init":
        return run_init(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
