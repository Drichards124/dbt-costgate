<div align="center">

<img src="docs/assets/logo.svg" alt="costgate logo" width="96" height="96"/>

# costgate

**The BigQuery cost gate for dbt pull requests.**

Dry-run what changed, price the diff, and catch the $500-a-day model<br/>*before* it merges — not on next month's bill.

[![CI](https://github.com/Drichards124/costgate/actions/workflows/ci.yml/badge.svg)](https://github.com/Drichards124/costgate/actions/workflows/ci.yml)
[![PLE](https://github.com/Drichards124/costgate/actions/workflows/ple.yml/badge.svg)](https://github.com/Drichards124/costgate/actions/workflows/ple.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://docs.astral.sh/ruff/)
[![Status](https://img.shields.io/badge/status-MVP-brightgreen)](#roadmap)

[How it works](#how-it-works) ·
[What you get](#what-you-get-on-every-pr) ·
[Where it fits](#where-it-fits) ·
[Pricing accuracy](#accurate-transparent-pricing) ·
[Security](#security-model) ·
[Roadmap](#roadmap) ·
[Contributing](CONTRIBUTING.md)

</div>

> [!NOTE]
> **Working MVP.** `costgate check` and the **GitHub Action** are implemented and
> tested. The PR-comment image below is an illustrative mock of the comment's
> design; the terminal output further down is **real** costgate output. See the
> [usage guide](docs/usage.md) and [changelog](CHANGELOG.md).

---

## The problem

On dbt + BigQuery teams, SQL changes merge with **zero visibility into their cost
impact**. A changed join, a dropped partition filter, or a widened incremental
window can multiply a model's bytes scanned — and the team finds out days later
on the bill, or when finance escalates.

BigQuery's dry-run API returns the *exact* bytes a query would scan — **for
free, before running anything**. costgate packages that into a first-class PR
gate:

<div align="center">

![How costgate works: pull request → compile both versions → BigQuery dry-run → price the diff → gate](docs/assets/flow.svg)

</div>

## What you get on every PR

<div align="center">

![Illustrative mock of the costgate PR comment: a per-model cost-diff table with a failing gate verdict](docs/assets/pr-comment-mock.svg)

</div>

<details open>
<summary><b>💻 The same check, in your terminal (real output)</b></summary>
<br/>

```text
$ costgate check --baseline path/to/main/manifest.json

costgate — region: US · on-demand $6.25/TiB · built-in table

  fct_orders_daily  (full-refresh): 68.20 MiB → 2.91 TiB   +$18.19/run   +$545.61/month (30 runs)
      ⚠ incremental — figure is the full-refresh scan
  dim_customers  (new): — → 412.50 MiB   +$0.00/run   +$0.07/month (30 runs)

  GATE: FAIL
    - fct_orders_daily: +$18.19/run exceeds $5.00

  Pricing: US $6.25/TiB · built-in table (table 2026.07, verified 2026-07-23)
  Estimates from BigQuery dry-run — nothing executed, no bytes billed, no SQL shown.
```

Or run it with no baseline at all for an instant local read of what your changed
models scan — and fail the run there on an absolute `--max-usd-total` /
`--max-tib-total` ceiling (no baseline required) — or get the full before/after
locally in one command with `costgate check --against main` (costgate compiles
`main` for you in a throwaway worktree). See the [usage guide](docs/usage.md).

</details>

## How it works

| Step | What happens | Cost to you |
|------|--------------|-------------|
| 1 · **Find what changed** | dbt's `state:modified` selector against a baseline manifest (your production artifacts), with a git-diff fallback | free |
| 2 · **Compile both versions** | The baseline and PR-branch versions of each changed model | free |
| 3 · **Dry-run each** | BigQuery `dryRun=true` returns exact bytes scanned — executes nothing, reads no table data | **free** |
| 4 · **Price the diff** | Region-aware on-demand rates; optionally × run frequency for $/month | free |
| 5 · **Gate** | Markdown PR comment, machine-readable JSON, policy-driven exit code (fail on a $ and/or % increase, or an absolute $/run or TiB/run ceiling) | free |

## Where it fits

costgate is the **preventive** half of BigQuery cost control — it deliberately
does not compete with the excellent retrospective tools:

| The question you're asking | Reach for |
|---|---|
| "What *did* our warehouse cost, by model / user / query?" | [dbt-bigquery-monitoring](https://github.com/bqbooster/dbt-bigquery-monitoring) |
| "What does the dbt platform estimate my models cost?" | [dbt Cost Insights](https://docs.getdbt.com/docs/explore/cost-insights) |
| "What is **this PR about to do** to our bill?" | **costgate** |

## Accurate, transparent pricing

BigQuery on-demand rates differ by region — a gate that prices every byte at
the US rate is silently wrong for half the world. costgate treats pricing
accuracy as a feature:

- 🌍 **Versioned per-region pricing table** with a `last_verified` date, auto-selected from your job's detected region.
- 🧾 **Every report discloses its math** — region, rate, and rate source. Never a silent assumption:

  ```text
  region: US (multi-region) · on-demand $6.25/TiB · source: built-in table 2026.07
  ```

- ⚙️ **Overridable** — `pricing.region` to force a region, `pricing.usd_per_tib` for negotiated or editions rates.
- ⚠️ **Honest limits, stated up front** — under capacity/editions pricing, bytes scanned is a proxy signal, not your invoice; the 1 TiB/month free tier is not modeled by default.

## Security model

This tool runs in CI next to warehouse credentials, so the design is
deliberately boring:

| Threat | Design answer |
|---|---|
| Billable or data-reading queries | **Dry-run only.** The single warehouse interaction is `jobs.insert` with `dryRun=true` — free, executes nothing |
| Credential theft / mishandling | **No credential surface.** Auth delegates entirely to [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials); in CI the documented path is keyless [Workload Identity Federation](https://github.com/google-github-actions/auth). There are no credential flags to misuse |
| Compromised CI runner | **Least privilege.** BigQuery Job User + metadata read — no data access, no writes; docs ship the exact IAM setup |
| Malicious fork PRs | **Fork-safe by default.** Documented workflows use the `pull_request` trigger; fork PRs degrade to "no report", never to exposed secrets |
| Secrets templated into SQL | **No compiled SQL in reports** — model names, bytes, and dollars only; snippets are strictly opt-in |
| Phone-home | **No telemetry.** The only network call is to the BigQuery API |

Details in [SECURITY.md](SECURITY.md) · deeper design notes in [docs/architecture.md](docs/architecture.md).

## Roadmap

- [x] **`costgate check`** — local (zero-setup) + CI diff, region-aware pricing, threshold gating
- [x] **One-command local diff** — `costgate check --against main` (isolated git worktree)
- [x] **GitHub Action** wrapper with a sticky PR comment
- [x] **Absolute cost ceilings** — gate on total `$/run` or `TiB/run`, not just the increase (works without a baseline, so it gates local mode too)
- [x] **Config- and macro-only change detection** — catch a change that reaches a model without touching its `.sql` file
- [ ] **`pre-commit` hook** entry
- [ ] **Docker image** on ghcr.io (GitLab CI–friendly)
- [ ] **Live pricing** (opt-in) via the Cloud Billing Catalog API

## Non-goals

- **Not a monitoring tool** — retrospective observability belongs to [dbt-bigquery-monitoring](https://github.com/bqbooster/dbt-bigquery-monitoring).
- **BigQuery only** — one warehouse done accurately beats three done approximately.
- **Never runs billable queries** — features that require executing real queries are out of scope by design.
- **No IDE/editor integration** (for now).

---

<div align="center">

[Contributing](CONTRIBUTING.md) ·
[Security policy](SECURITY.md) ·
[Changelog](CHANGELOG.md) ·
[Code of Conduct](CODE_OF_CONDUCT.md) ·
[Apache-2.0](LICENSE) · [NOTICE](NOTICE)

Built by [Dashan Richards](https://github.com/Drichards124) — DCO sign-off required, hard invariants apply:<br/>
**dry-run only · no credential handling · no telemetry**

</div>
