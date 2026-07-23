<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

> Skeleton — filled in as the MVP lands. The README carries the user-facing
> story; this document carries the engineering shape and its invariants.

## Components

- **`costgate` CLI (Python, `src/costgate/`)** — all logic lives here. Finds
  changed models, compiles both versions, dry-runs them, prices the diff,
  renders reports (terminal markdown, JSON), applies threshold policy via
  exit code. CI-agnostic: GitHub Actions, GitLab CI, and local runs share
  this one code path.
- **GitHub Action wrapper (planned, separate top-level `action.yml`)** — thin
  composite action: install the CLI, run it, post/update a single sticky PR
  comment. No logic of its own.

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
