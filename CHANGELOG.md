# Changelog

All notable, user-visible changes to `costgate` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(currently pre-1.0: minor versions may contain breaking changes, noted here).

## [Unreleased]

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
  - Incremental models estimated via their full-refresh form and labeled as such.
  - Output formats: `terminal`, `markdown`, `json` (`--format`, `--output`).
  - Thresholds and behavior via `.costgate.yml` or flags; `--fail-on`.
  - Exit codes: `0` pass, `1` gate failed, `2` costgate couldn't run.
  - Credentials are never handled — Application Default Credentials only.
- Documentation: `docs/usage.md`.
