<!-- SPDX-License-Identifier: Apache-2.0 -->

# Using costgate

`costgate check` estimates the BigQuery cost impact of the dbt models a change
touches. It reads compiled dbt artifacts, dry-runs each changed model (free —
nothing is executed), prices the bytes region-aware, and reports.

> costgate never runs a billable query and never handles a credential. Auth is
> Application Default Credentials, exactly like dbt-bigquery.

## Install

```bash
pip install costgate      # or: pipx install costgate / uv tool install costgate
```

Requires Python ≥ 3.9 and BigQuery access via ADC:

```bash
gcloud auth application-default login
```

## Local, zero setup (start here)

On a feature branch, compile your project, then check what you changed:

```bash
dbt compile
costgate check
```

costgate finds the models you changed versus `main` (via `git diff`) and prints
each one's current scan cost. No baseline, no CI, no config required:

```text
costgate — region: US · on-demand $6.25/TiB · built-in table

  fct_orders_daily  (full-refresh): 2.91 TiB scanned   $18.19/run   $545.62/month (30 runs)
      ⚠ incremental — figure is the full-refresh scan
  dim_customers: 412.50 MiB scanned   $2.51/run   $75.30/month (30 runs)

  GATE: PASS

  Pricing: US $6.25/TiB · built-in table (table 2026.07, verified 2026-07-23)
  Estimates from BigQuery dry-run — nothing executed, no bytes billed, no SQL shown.
```

Pick models explicitly with `--select name1,name2` (e.g. pipe
`dbt ls --select state:modified` for dbt-authoritative selection).

### Gate locally with absolute ceilings

You can fail the run here — no baseline required — with an **absolute** cap on any
single model's total per-run cost or scan:

```bash
costgate check --max-usd-total 20 --max-tib-total 3
```

These gate the *total* (not the increase), so they also catch an already-expensive
model that a before/after diff would wave through because it barely changed. For
incrementals the figure is the full-refresh scan (flagged `full-refresh`), so an
absolute cap gates rebuild cost. Set them in `.costgate.yml` under `thresholds` to
apply everywhere (see [Configuration](#configuration-costgateyml)).

## CI: before/after diff and gating

To show the **change** in cost (and block a PR that crosses a threshold), give
costgate a baseline — `main`, compiled the same way — and thresholds:

```bash
costgate check --baseline path/to/main/manifest.json --format markdown
```

```text
  fct_orders_daily  (full-refresh): 68.20 MiB → 2.91 TiB   +$18.19/run   +$545.61/month (30 runs)
  GATE: FAIL
    - fct_orders_daily: +$18.19/run exceeds $5.00
```

Exit codes: **0** pass, **1** gate failed, **2** costgate couldn't run
(bad args, missing/uncompiled manifest, auth failure, or every model errored).
CI can hard-block on 1 and alert-only on 2.

### Getting the baseline

The baseline is `main`, compiled. It doesn't exist until something compiles main
(it is *not* derivable from a warehouse table or a model name — the warehouse has
the built table, not the current main branch's compiled SQL).

- **Recommended:** a one-time job on merge to `main` compiles main and stashes its
  `manifest.json` (a bucket, a CI artifact, or a dedicated branch). Every PR run
  downloads it — the author never leaves their branch.
- **Compile it the same way as the PR** so incremental models are in full-refresh
  form on both sides (see below). A prod-run manifest captures incrementals in
  their incremental form; costgate flags that as a basis mismatch rather than
  mis-diffing.

### Local before/after in one command (`--against <ref>`)

Locally, skip the manual baseline entirely. Compile your branch once, then let
costgate produce the baseline for you:

```bash
dbt compile                       # your branch (the "current" side)
costgate check --against main     # costgate compiles `main` for the baseline
```

`--against <ref>` checks `<ref>` out into a throwaway git worktree, runs
`dbt compile` there, diffs it against your compiled branch, and removes the
worktree when done — no stashing, no leaving your branch, no `--baseline` path to
manage. `--against` and `--baseline` are mutually exclusive.

Notes:

- Your dbt project can live at the git repo root or in a subdirectory (monorepos):
  costgate detects the repo root and compiles the project in its actual location.
  It infers the project directory from `--current` (the parent of your target dir);
  pass `--project-dir <dir>` to state it explicitly when your compiled target isn't
  at `<project>/target`.
- `dbt` must be importable from your environment (it resolves your venv's `dbt`
  even when only a shell alias is on `PATH`).
- Your installed `dbt_packages/` are reused (symlinked into the worktree), so there
  is no `dbt deps` step — meaning the baseline compiles against *your branch's*
  package versions, close enough for a cost baseline.
- This is a local convenience. In CI, prefer the stashed-baseline approach above:
  it's faster (no second compile) and doesn't depend on the base ref being
  buildable from the PR runner.

### Renamed a model?

costgate pairs baseline↔current models by dbt identity (`unique_id`), which is
tied to the model's `.sql` name. A **physical** table rename (a dbt `alias`,
`schema`, or `database` that differs dev↔prod) does **not** change the identity, so
the baseline is matched automatically — nothing to do. But renaming the **model
itself** (e.g. `fct_orders_monthly` → `fct_orders_daily`) changes its `unique_id`,
and auto-matching can no longer pair the two — the current model would be reported
as *new*, losing the before/after.

For that case, declare the pairing (`current: baseline`) so costgate diffs them —
useful for seeing how, say, a granularity change affects scan cost:

```yaml
# .costgate.yml
renames:
  fct_orders_daily: fct_orders_monthly
```

- Each side is a **model name** or a full **`unique_id`** (use the `unique_id` if a
  bare name is ambiguous across packages).
- Requires a baseline (`--baseline`/`--against`); renames are a diff-mode concept.
- An unresolvable, ambiguous, or many-to-one entry **fails the run** (exit 2) with
  an actionable message — it never silently mis-diffs.
- `--select` targets **current** model names: a renamed model is selected by its
  new name, then diffed via the map.

## GitHub Action

Wrap `costgate check` in a pull-request gate with one step. The Action installs
costgate, runs the check, and posts a single **sticky** comment with the cost
report — updated in place on every push, never stacked.

```yaml
# .github/workflows/costgate.yml
name: costgate
on: pull_request

# pull-requests: write lets the Action post the sticky comment on same-repo PRs.
# Fork PRs get a read-only token and degrade to "no comment" — never a failed
# check. id-token: write is for keyless Workload Identity Federation (below).
permissions:
  contents: read
  pull-requests: write
  id-token: write

concurrency: # one run per PR — avoids two pushes racing to double-post
  group: costgate-${{ github.ref }}
  cancel-in-progress: true

jobs:
  costgate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # Your dbt setup + keyless auth to BigQuery (google-github-actions/auth).
      - run: pip install dbt-bigquery
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER }}
          service_account: ${{ vars.WIF_SERVICE_ACCOUNT }}

      # Compile the PR branch, and fetch the baseline (main, compiled the same
      # way — see "Getting the baseline" above).
      - run: dbt compile
      # - run: <download your baseline manifest.json to baseline/manifest.json>

      - uses: Drichards124/costgate@v0.4.0
        with:
          baseline: baseline/manifest.json
          fail-on: fail # optional; unset defers to .costgate.yml
```

The Action needs a Python environment (your dbt setup provides one) and compiled
dbt artifacts. It runs `check --format markdown`; every `check` flag is an input
(`baseline`, `config`, `select`, `base`, `fail-on`, `max-usd-per-run`, `max-pct`,
`max-usd-per-month`, `region`, `usd-per-tib`, `project`, `threads`), plus:

| Input | Default | Purpose |
|---|---|---|
| `comment` | `true` | Post/update the sticky PR comment. |
| `fail-on-operational` | `false` | Fail the job on an operational error (exit 2). Default is alert-only. |
| `github-token` | `${{ github.token }}` | Token for the comment (needs `pull-requests: write`). |

Outputs: `exit-code` (0/1/2) and `status` (`ok`/`failed`/`error`). The exact
PASS/WARN/FAIL verdict is in the comment body.

## Incremental models

Incrementals are first-class. Compiled in a fresh target, `is_incremental()` is
false, so dbt emits the **full-refresh** query (no `{{ this }}` reference), which
dry-runs cleanly. costgate labels these "full-refresh scan": the number is the
cost to rebuild the table, and the baseline→PR diff of it reliably catches
structural regressions (bad joins, lost partition pruning, widened scans).

Recommended CI compile so upstream refs resolve to production:

```bash
dbt compile --defer --state path/to/prod/artifacts --favor-state
```

The true per-incremental-run cost (a dynamic `WHERE ts > MAX(ts)` predicate) is
not knowable from a dry-run — BigQuery reports the worst case. costgate does not
fake it; it flags it.

## Accuracy notes

- **Change detection catches SQL-body changes, not config- or macro-only ones.**
  Both the `git diff` local default and the checksum diff (`--baseline` /
  `--against`) select a model when its SQL body changes or it's newly added —
  mirroring the common core of dbt's `state:modified`. A change that touches *only* a model's config (e.g.
  `materialized`, `partition_by`) or *only* an upstream macro won't be picked up
  automatically. Price those explicitly with `--select`, or pipe
  `dbt ls --select state:modified` for dbt-authoritative selection.
- **Dynamic filters** (`CURRENT_DATE()`, subquery predicates) make BigQuery
  dry-runs report a full-table scan. costgate flags these ("dry-run may be
  worst-case") and lets you `exclude`/`warn_only` heavily-partitioned models.
- **Region-aware pricing.** The applied region, rate, and its source appear in
  every report. Unlisted regions fall back to a disclosed default; override a
  negotiated/editions rate flatly with `pricing.usd_per_tib`, or per region with
  `pricing.regions` (see below). When a report spans regions with different
  sources, each region is tagged (`override` / `table` / `fallback`).

## Configuration (`.costgate.yml`)

```yaml
pricing:
  region: europe-west3          # force a region (default: auto-detect)
  usd_per_tib: 5.00             # flat negotiated / editions override (all regions)
  regions:                      # per-region rates (patch/extend the built-in table)
    europe-west3: 4.80          #   region keys match case-insensitively
    US: 6.00                    #   0.00 is valid (e.g. flat-rate slots); negative is rejected
thresholds:
  max_usd_increase_per_run: 5.00     # delta vs baseline (needs --baseline/--against)
  max_pct_increase: 25
  max_usd_increase_per_month: 100.00
  max_usd_total: 20.00              # absolute $/run cap — no baseline; gates local runs too
  max_tib_total: 3.00              # absolute TiB/run cap — no baseline; gates local runs too
run_frequency:
  default: 30                   # runs/month, for the $/month estimate
  models:
    fct_orders_daily: 30
exclude:                        # reported, never gated
  - events_partitioned
warn_only:                      # shown as a warning, not gated
  - sessions_rolling
renames:                        # pair a renamed model to its baseline (current: baseline)
  fct_orders_daily: fct_orders_monthly
report:
  format: terminal              # terminal | markdown | json
fail_on: fail                   # never | warn | fail
```

CLI flags (`--region`, `--usd-per-tib`, `--max-usd-per-run`, `--fail-on`, …)
override the file.

For the full, always-current list of keys — type, default, and what each does —
run `costgate config` (add `--format json` for a machine-readable version).

**Rate precedence** (most specific first): CLI `--usd-per-tib` → config
`pricing.regions[region]` → config `pricing.usd_per_tib` → built-in table →
disclosed default. A region not named in `pricing.regions` still falls through to
the flat override, then the table.

## Least-privilege IAM

The gate's identity needs only to dry-run — no table data, no writes:

- **BigQuery Job User** (`roles/bigquery.jobUser`) — to create dry-run jobs.
- **BigQuery Metadata Viewer** (`roles/bigquery.metadataViewer`) — to read the
  table metadata a dry-run consults.

In CI, prefer keyless
[Workload Identity Federation](https://github.com/google-github-actions/auth)
over a long-lived service-account key.
