# Changelog

All notable, user-visible changes to `dbt-costgate` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(currently pre-1.0: minor versions may contain breaking changes, noted here).

## [Unreleased]

### Added

- **A net impact line, so a change that *lowers* cost is reported as an outcome.**
  Diff reports now end with one line naming the overall direction in words:

  ```text
  Net saving: USD 43.75/run · USD 1,312.50/month
  Net increase: USD 13.19/run · USD 395.63/month
  ```

  Reductions were always calculated correctly, but they showed only as a minus
  sign on a row and a `Gate: PASS` identical to a change that did nothing — so
  optimisation work looked like the absence of a failure. On a pull request
  touching several models, the totals also had to be added up by hand.

  The net is a **measurement, not a verdict**: it counts every estimated model,
  including ones excluded from gating (their cost is real either way), and it can
  report a net saving while the gate still fails on an individual model that
  breached its own threshold. If some models could not be estimated the line says
  so rather than presenting a partial sum as a total, and the monthly figure is
  omitted entirely unless every model contributed one. With a rate of `0` it
  reports scanned bytes. `--format json` gains a signed `net` block, where
  negative means a saving.

- **`pricing.currency` / `--currency`** — label reported amounts with an ISO 4217
  code of your choice, e.g. `EUR`. Pair it with your own rate
  (`pricing.usd_per_tib` or `pricing.regions`) when you are billed in something
  other than USD.

  **dbt-costgate labels; it never converts.** The setting means "the rate I gave
  you is denominated in this", not "convert into this". Because the built-in table
  is USD, setting a non-USD currency while any region is still priced *from that
  table* now exits 2 with an explanation, rather than printing a USD number with
  your label on it. The Action gains a matching `currency` input.

### Changed

- **Amounts now carry an ISO 4217 code instead of a `$` symbol** —
  `USD 6.25/TiB`, `+USD 43.75/run`, `fct_orders: +USD 43.75/run exceeds USD 10.00`.
  `$` is also CAD, AUD and SGD, so a bare symbol was ambiguous the moment anyone
  reported in anything but US dollars. The code appears on each amount rather than
  once in a column header, so a single quoted row is never ambiguous; the markdown
  cost columns are therefore now headed `Cost / run` and `Cost / month`.

  If you scrape terminal or markdown output, this changes what you parse.
  `--format json` is unaffected — its `usd_*` field names are a published contract
  and keep their names — and it gains `pricing.currency` and `pricing.priced`.

- **Diff reports now show percentage growth (`Δ %`) alongside the amounts.** It is
  often the quicker read for spotting a regression, and it is the threshold most
  teams set first. A model with no baseline (a new one) shows `—`, since there is
  nothing to take a ratio against.

- **A rate of `0` now drops money from reports entirely.** Setting
  `pricing.usd_per_tib: 0.00` is the documented way to run under capacity/Editions
  (slot) pricing, where there is no per-byte price and bytes scanned is a work
  signal rather than an invoice. Reports previously showed a column of `USD 0.00`,
  which read as broken output. They now omit amounts and show scanned bytes plus
  percentage growth instead, and say why in the disclosure line. No new flag or
  config key: a zero rate already says everything needed.

- **Region-aware pricing now covers 48 BigQuery locations instead of 2.** The
  built-in rate table previously held only the `US` and `EU` multi-regions, so
  every regional location fell back to the US rate of $6.25/TiB and was reported
  as `default-fallback`. Published on-demand rates vary far more than that —
  from $6.25/TiB up to $11.25/TiB in `southamerica-east1` — so if your models run
  outside the US/EU multi-regions, dbt-costgate was under-reporting their cost,
  by up to 80% in the worst case.

  Your reported figures will change accordingly, and the rate source for those
  regions now reads `region-table` rather than `default-fallback`. A few examples:

  | location | before | now |
  |---|---|---|
  | `europe-west3` (Frankfurt) | $6.25 | $8.125 |
  | `asia-northeast1` (Tokyo) | $6.25 | $7.50 |
  | `australia-southeast1` (Sydney) | $6.25 | $8.125 |
  | `southamerica-east1` (São Paulo) | $6.25 | $11.25 |

  Note that US regions are not uniform either: `us-south1` is $7.50/TiB and
  `us-west2`/`us-west3` are $8.4375/TiB, not $6.25. Rates are unchanged for the
  `US` and `EU` multi-regions.

  If you set your own rate with `pricing.usd_per_tib` or `pricing.regions`, your
  override still wins and nothing changes for you. Any location still not listed
  continues to fall back to $6.25/TiB and to say so in the report.

### Fixed

- **`max_pct_increase` no longer stops working when you set a rate of `0`.** A
  rate of `0.00` is documented for capacity/flat-rate slots, where bytes scanned
  is a work signal rather than an invoice. Setting it makes every dollar figure
  `0.00`, which correctly neutralises the three USD thresholds — but it also
  silently disabled the percentage threshold, because the percentage was computed
  from dollars. `max_tib_total` was left as the only gate that still fired, so a
  model whose scan grew tenfold could pass with exit code 0 and no warning.

  The percentage is now computed from scanned bytes. A percentage has no
  currency, so it holds under any pricing model.

  **This can newly fail a build that previously passed** — that is the point, but
  worth knowing before you upgrade if you run with `usd_per_tib: 0`. If you are
  on on-demand pricing with any non-zero rate, nothing changes: both sides of a
  comparison are priced at the same regional rate, so the rate cancels out of a
  ratio and the reported percentage is identical to before.

  `pct_delta` in `--format json` output is affected the same way — it reported
  `null` at a zero rate and now reports the real percentage.

## [0.7.1] - 2026-07-25

### Added

- **Published to PyPI.** `pip install dbt-costgate` now works, alongside
  `pipx install` and `uv tool install`. Releases are published from CI using
  PyPI trusted publishing (OIDC), so no API token exists for this project.
  Every release still attaches a wheel, an sdist, and `SHA256SUMS` to its GitHub
  Release if you prefer to pin to a checksummed artifact.

## [0.7.0] - 2026-07-25

### Changed

- **Renamed from `costgate` to `dbt-costgate`.** The name `costgate` is taken on
  PyPI by an unrelated project, so `pip install costgate` would have installed
  someone else's package. Everything follows the new name:

  | | before | after |
  |---|---|---|
  | command | `costgate check` | `dbt-costgate check` |
  | config file | `.costgate.yml` | `.dbt-costgate.yml` |
  | Action | `Drichards124/costgate@v` | `Drichards124/dbt-costgate@v` |
  | import | `costgate` | `dbt_costgate` |

  To migrate: rename your config file, update the `uses:` line in any workflow,
  and reinstall. Flags, config keys, report format, and exit codes are unchanged.
  The old repository URL redirects permanently, so existing clones keep working.

  Releases v0.1.0–v0.6.0 shipped under the old name and their changelog entries
  below are left as-is, as a record of what actually shipped.

## [0.6.0] - 2026-07-24

### Added

- **Config- and macro-only change detection.** `costgate check` now selects a model
  when a change it doesn't own reaches it, instead of silently skipping it:
  - With a baseline (`--baseline` / `--against`), when the model's **compiled SQL**
    differs even though its own `.sql` file is unchanged — what an upstream macro
    edit or a config change actually does. These models are labelled in the report
    ("compiled SQL changed but the model file didn't"), so a model never shows up
    without a reason.
  - In the local `git diff` default (no baseline), when the change touches a
    **macro** anywhere in the model's macro closure, or the **YAML file that
    patches it** (its `schema.yml`).

  A changed `dbt_project.yml` is reported on stderr rather than guessed at —
  project-wide config can't be traced to individual models from a diff.

### Fixed

- **Local change detection now works when the dbt project is in a repo
  subdirectory** (monorepos). `git diff` reports paths from the repository root
  while a dbt manifest records them relative to the project, so nothing matched and
  `costgate check` reported zero models and exited 0 — indistinguishable from
  "nothing changed". Changes in sibling directories are no longer considered
  either. Projects at the repository root are unaffected.

### Changed

- A config change that doesn't alter a model's compiled SQL (`partition_by`,
  `cluster_by`) is still not selected: it doesn't change that model's own scan
  either. `docs/usage.md` now states this precisely instead of describing all
  config changes as undetected.

## [0.5.0] - 2026-07-24

### Added

- **`--max-usd-total` / `--max-tib-total`** on `costgate check` (and
  `thresholds.max_usd_total` / `thresholds.max_tib_total` in `.costgate.yml`) —
  absolute per-run ceilings that fail the gate when a single model's *total* cost
  or scan exceeds the cap, regardless of how much it changed. They need no
  baseline, so they gate the zero-setup local run and also catch an
  already-expensive model that a before/after diff would pass because it barely
  changed. For incrementals the figure is the full-refresh scan.
- **GitHub Action** now exposes `max-usd-total`, `max-tib-total`, and
  `project-dir` inputs (the latter catching the Action up to the `--project-dir`
  flag), forwarding each to `costgate check`.
- **`renames`** config key (`.costgate.yml`) — pair a renamed model to its
  baseline (`current: baseline`, by model name or `unique_id`) so costgate diffs
  across a model rename instead of reporting the model as new. Requires a
  baseline; a bad mapping fails the run with an actionable error.
- **Named baselines** — a `baselines:` map in `.costgate.yml` (each entry a
  `manifest:` path or an `against:` git ref) plus `default_baseline`, selected with
  `--baseline-target <name>` (and the Action's `baseline-target` input). Set a
  default once and `costgate check` diffs without a baseline flag; switch
  environments (main / ple / prod) by name.

### Docs

- New "Audit / monitor-only" guide — track cost without ever blocking a deploy
  (`fail_on: never`, no thresholds, or `--format json` for a record).

## [0.4.0] - 2026-07-24

### Added

- **`--project-dir <dir>`** on `costgate check` — point costgate at the directory
  containing your `dbt_project.yml` when it can't be inferred from `--current`
  (for example a custom `target-path` or a copied manifest).

### Changed

- **`--against <ref>`** now works when your dbt project lives in a repo
  subdirectory, not just at the git repo root. costgate detects the repo root and
  compiles the project in its actual location.

## [0.3.0] - 2026-07-24

### Added

- **`costgate check --against <ref>`** — get a before/after cost diff locally in
  one command, with no manual baseline. costgate checks `<ref>` out into a
  throwaway git worktree, runs `dbt compile` there, uses it as the baseline, and
  removes the worktree when done. Compile your own branch once; `--against` handles
  the other side.
  - Mutually exclusive with `--baseline`.
  - Reuses your installed `dbt_packages/` (no `dbt deps` step); assumes the dbt
    project is at the git repo root and resolves `dbt` from your active
    environment.
  - On any failure (unknown ref, `dbt` not found, compile error) it exits `2` with
    an actionable message and always cleans up the worktree.

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
