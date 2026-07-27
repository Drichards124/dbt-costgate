<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

The README carries the user-facing story and [usage.md](usage.md) the how-to;
this document is the engineering shape and its invariants.

## Modules (`src/dbt_costgate/`)

A **pure core** plus two side-effecting edges. Everything except `bigquery.py`
(network) and `gitdiff.py` (a git subprocess) is pure Python over dataclasses,
so the pipeline is unit-tested end to end without a warehouse or credentials.

| Module | Responsibility | Edge? |
|---|---|---|
| `cli.py` | argparse `check`, wire the pipeline, own the exit codes | — |
| `config.py` | load `.dbt-costgate.yml`, merge CLI overrides (CLI wins) | — |
| `artifacts.py` | load manifest, filter to cost-bearing models, resolve compiled SQL, change detection (body checksum, compiled SQL, macro/patch paths), basis + warning heuristics | — |
| `gitdiff.py` | changed paths via `git diff` (git only, no dbt) | git |
| `pricing.py` + `data/pricing.json` | region → $/TiB with disclosed source | — |
| `bigquery.py` | `DryRunner` protocol + `BigQueryDryRunner` (ADC, retry) | network |
| `estimate.py` | drive dry-runs, categorize errors, build priced deltas | — |
| `policy.py` | threshold evaluation → verdict + exit code | — |
| `report.py` | render terminal / markdown / JSON | — |
| `models.py` | shared dataclasses and enums | — |

The **GitHub Action wrapper** (separate `action.yml`) is a thin composite:
install the CLI, run it, post/update one sticky PR comment. No logic.

## Data flow

```
compiled artifacts ──┐
  --baseline (main)   ├─→ select changed (─select > baseline-diff > git-diff)
  --current (target)  │         │  filter: resource_type=model, language=sql,
                      │         │          non-ephemeral
                      │         ▼
                      │   resolve compiled SQL (compiled_path file, else compiled_code)
                      │         │
                      │         ▼
                      │   BigQuery dry-run (dryRun=true) — free, per model, retried
                      │         │  categorize failures (destination_missing … operational)
                      │         ▼
                      └─→ region-aware pricing ─→ report (md/term/json) ─→ gate (exit code)
```

## Data flow (MVP)

```
baseline manifest ──┐
                    ├─→ changed models (state:modified) ─→ dbt compile ×2
PR working tree  ───┘                                          │
                                                               ▼
                              BigQuery jobs.insert(dryRun=true) — free
                                                               │
                                                               ▼
                     bytes diff ─→ region-aware pricing ─→ report + gate
```

## Invariants (enforced in review; see also SECURITY.md)

1. Dry-run is the only warehouse interaction. No billable queries, ever.
2. No credential acceptance, storage, or logging — ADC only.
3. Reports exclude compiled SQL by default (secrets can be templated in).
4. Every dollar figure is traceable: region + rate + rate source in output.
5. No telemetry; the BigQuery API is the only network endpoint.
6. Documented CI patterns are fork-safe (`pull_request` trigger; secrets are
   never exposed to fork PRs).

## Pricing model

Versioned `pricing.json` (region → on-demand $/TiB, `last_verified` date,
table version) bundled with the package. Region auto-detected from the dry-run
job / dbt profile; overridable (`pricing.region`, `pricing.usd_per_tib`).
Compute only, by scope: BigQuery meters compute and storage separately, and a
dry-run reports bytes a query would scan — a compute figure carrying no storage
information. Storage is a non-goal, not an unimplemented feature.

Free tier (1 TiB/month, per billing account) is declared, never deducted:
`pricing.free_tib_per_month` lets a team state their allowance and the report
shows this change's projected monthly scan against it, but nothing is subtracted
from any figure the gate reads. Consumption is account-wide and invisible from a
dry-run, so subtracting would mean guessing, and guessing low is the wrong
direction for a gate. Editions/slot pricing: bytes is a documented proxy, not an
invoice prediction.

## Known hard edges (design drivers)

- **Incremental models**: full-refresh and incremental runs scan very
  different bytes. Plan: report both where derivable, flag ambiguity rather
  than guess silently.
- **`dbt compile` can touch the warehouse**: macros may run introspective
  queries at compile time. Documented honestly; least-privilege SA bounds it.
- **Dry-run needs compilable SQL** with real target credentials — the gate
  runs where dbt already runs, using the auth dbt already has.

## Roadmap notes

- Per-region custom pricing overrides in config.

Shipped: absolute cost ceilings (`max_usd_total` / `--max-usd-total`,
`max_tib_total` / `--max-tib-total`) — gate a model's *total* per-run scan, not
just the before/after delta. Unlike the three increase thresholds, an absolute
ceiling needs no baseline, so it also gates the zero-setup local (`absolute`) mode.
Caveat: for incremental models what the reported `$/run` *is* depends on the
estimate basis, so an absolute cap gates rebuild cost only on a `full-refresh`
row — paired with the per-row basis labeling, which is derived from the basis
rather than from `is_incremental` so the tag and the figure cannot disagree.

Shipped: one-command local diff (`--against <ref>`) — checks the ref out into an
isolated git worktree, `dbt compile`s it as the baseline, and removes the worktree
after. The one edge that needs both git and dbt, so it lives outside the pure core
and outside git-only `gitdiff.py`.
