<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

The README carries the user-facing story and [usage.md](usage.md) the how-to;
this document is the engineering shape and its invariants.

## Modules (`src/costgate/`)

A **pure core** plus two side-effecting edges. Everything except `bigquery.py`
(network) and `gitdiff.py` (a git subprocess) is pure Python over dataclasses,
so the pipeline is unit-tested end to end without a warehouse or credentials.

| Module | Responsibility | Edge? |
|---|---|---|
| `cli.py` | argparse `check`, wire the pipeline, own the exit codes | — |
| `config.py` | load `.costgate.yml`, merge CLI overrides (CLI wins) | — |
| `artifacts.py` | load manifest, filter to cost-bearing models, resolve compiled SQL, checksum-diff, basis + warning heuristics | — |
| `gitdiff.py` | local selection via `git diff` (git only, no dbt) | git |
| `pricing.py` + `data/pricing.json` | region → $/TiB with disclosed source | — |
| `bigquery.py` | `DryRunner` protocol + `BigQueryDryRunner` (ADC, retry) | network |
| `estimate.py` | drive dry-runs, categorize errors, build priced deltas | — |
| `policy.py` | threshold evaluation → verdict + exit code | — |
| `report.py` | render terminal / markdown / JSON | — |
| `models.py` | shared dataclasses and enums | — |

The **GitHub Action wrapper** (separate `action.yml`, next PR) is a thin
composite: install the CLI, run it, post/update one sticky PR comment. No logic.

## Data flow

```
compiled artifacts ──┐
  --baseline (main)   ├─→ select changed (─select > checksum-diff > git-diff)
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
5. No telemetry; BigQuery API (and opt-in Billing Catalog API) are the only
   network endpoints.
6. Documented CI patterns are fork-safe (`pull_request` trigger; secrets are
   never exposed to fork PRs).

## Pricing model

Versioned `pricing.json` (region → on-demand $/TiB, `last_verified` date,
table version) bundled with the package. Region auto-detected from the dry-run
job / dbt profile; overridable (`pricing.region`, `pricing.usd_per_tib`).
Free tier (1 TiB/month) not modeled by default. Editions/slot pricing: bytes
is a documented proxy, not an invoice prediction.

## Known hard edges (design drivers)

- **Incremental models**: full-refresh and incremental runs scan very
  different bytes. Plan: report both where derivable, flag ambiguity rather
  than guess silently.
- **`dbt compile` can touch the warehouse**: macros may run introspective
  queries at compile time. Documented honestly; least-privilege SA bounds it.
- **Dry-run needs compilable SQL** with real target credentials — the gate
  runs where dbt already runs, using the auth dbt already has.

## Open questions (tracked in decision records)

- Baseline acquisition UX for local runs (artifact download helper vs. BYO
  manifest path).
- $/month modeling: per-model run-frequency config format.
