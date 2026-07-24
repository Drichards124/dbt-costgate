# Changelog

All notable, user-visible changes to `costgate` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(currently pre-1.0: minor versions may contain breaking changes, noted here).

## [Unreleased]

## [0.2.0] - 2026-07-24

### Added

- **GitHub Action** — gate pull requests with a single `uses:` step. The Action
  runs `costgate check` and posts one **sticky** comment with the cost report,
  updating that same comment on each new commit instead of stacking new ones.
  - Fails the check when the gate is breached (exit `1`). Operational errors
    (exit `2`, e.g. missing credentials) are **alert-only** by default; set
    `fail-on-operational: true` to hard-fail instead.
  - On an operational error the comment says costgate couldn't run, so a stale
    passing report never lingers over a commit that wasn't evaluated.
  - Fork pull requests degrade to no comment (never a failed check).
  - Inputs mirror the `check` flags (`baseline`, `config`, `select`, `fail-on`,
    `max-usd-per-run`, `max-pct`, `max-usd-per-month`, `region`, `usd-per-tib`,
    `project`, …); outputs `exit-code` and `status`.
  - See [docs/usage.md](docs/usage.md) for a ready-to-copy workflow.

## [0.1.0] - 2026-07-24

### Added

- `costgate check` — estimate the BigQuery cost impact of changed dbt models
  from compiled artifacts, using free BigQuery dry-runs.
  - **Local mode** (zero setup): run in a compiled dbt project to see the scan
    cost of the models you changed versus `main` (selected via `git diff`).
  - **Diff mode** (`--baseline <manifest>`): before/after cost per model, with
    threshold gating.
  - Selection: `--select`, else baseline checksum-diff, else `git diff`.
  - Region-aware pricing that discloses the region, rate, and source in every
    report; overridable via config or `--usd-per-tib`/`--region`.
  - Per-region pricing map: set `pricing.regions` in `.costgate.yml` to patch or
    extend the built-in rate table one region at a time (region keys match
    case-insensitively; `0.00` is allowed for flat-rate slots). Reports tag each
    region's source when a run spans regions of differing provenance, and the
    `json` output gains a `pricing.region_sources` map. Rate precedence, most
    specific first: `--usd-per-tib` → `pricing.regions` → `pricing.usd_per_tib` →
    built-in table → disclosed default.
  - Incremental models estimated via their full-refresh form and labeled as such.
  - Output formats: `terminal`, `markdown`, `json` (`--format`, `--output`).
  - Thresholds and behavior via `.costgate.yml` or flags; `--fail-on`.
  - Exit codes: `0` pass, `1` gate failed, `2` costgate couldn't run.
  - Credentials are never handled — Application Default Credentials only.
- `costgate config` — list every `.costgate.yml` key with its type, default, and a
  plain-English explanation; `--format json` for a machine-readable reference.
- Documentation: `docs/usage.md`.
