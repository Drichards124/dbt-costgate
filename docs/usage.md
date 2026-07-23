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

- **Dynamic filters** (`CURRENT_DATE()`, subquery predicates) make BigQuery
  dry-runs report a full-table scan. costgate flags these ("dry-run may be
  worst-case") and lets you `exclude`/`warn_only` heavily-partitioned models.
- **Region-aware pricing.** The applied region, rate, and its source appear in
  every report. Unlisted regions fall back to a disclosed default; override a
  negotiated/editions rate with `pricing.usd_per_tib`.

## Configuration (`.costgate.yml`)

```yaml
pricing:
  region: europe-west3          # force a region (default: auto-detect)
  usd_per_tib: 5.00             # negotiated / editions override
thresholds:
  max_usd_increase_per_run: 5.00
  max_pct_increase: 25
  max_usd_increase_per_month: 100.00
run_frequency:
  default: 30                   # runs/month, for the $/month estimate
  models:
    fct_orders_daily: 30
exclude:                        # reported, never gated
  - events_partitioned
warn_only:                      # shown as a warning, not gated
  - sessions_rolling
report:
  format: terminal              # terminal | markdown | json
fail_on: fail                   # never | warn | fail
```

CLI flags (`--region`, `--usd-per-tib`, `--max-usd-per-run`, `--fail-on`, …)
override the file.

## Least-privilege IAM

The gate's identity needs only to dry-run — no table data, no writes:

- **BigQuery Job User** (`roles/bigquery.jobUser`) — to create dry-run jobs.
- **BigQuery Metadata Viewer** (`roles/bigquery.metadataViewer`) — to read the
  table metadata a dry-run consults.

In CI, prefer keyless
[Workload Identity Federation](https://github.com/google-github-actions/auth)
over a long-lived service-account key.
