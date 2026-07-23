# costgate

**BigQuery cost gate for dbt pull requests.** Dry-run the models a PR changes,
diff the bytes against production, and post the dollar impact on the PR —
*before* it merges, not on next month's bill.

> **Status: pre-MVP.** This repository currently contains the project
> definition and scaffold. Output shown below is an illustrative mock of the
> target design.

![How costgate works](docs/assets/flow.svg)

## The problem

On dbt + BigQuery teams, SQL changes merge with zero visibility into their
cost impact. A changed join, a dropped partition filter, or a widened
incremental window can multiply a model's bytes scanned — and the team finds
out days later on the bill, or when finance escalates.

BigQuery's dry-run API returns the *exact* bytes a query would scan, for
free, before running anything. Excellent tools exist to analyze what your
warehouse *did* cost (see [dbt-bigquery-monitoring](https://github.com/bqbooster/dbt-bigquery-monitoring)).
costgate covers the missing half: what a change is *about* to cost, at the
moment it can still be reviewed.

## What you get on every PR

![Illustrative mock of the costgate PR comment](docs/assets/pr-comment-mock.svg)

And the same check runs locally, before you even open the PR:

```text
$ costgate check --baseline .costgate/prod-manifest.json
costgate — region: US (multi-region) · on-demand $6.25/TiB · source: built-in table 2026.07

  model                     baseline      this branch       Δ per run    est. Δ per month
  ─────────────────────────────────────────────────────────────────────────────────────
  fct_orders_daily          68.2 MiB   →  2.91 TiB          +$18.16      +$544.80  (30 runs)
  dim_customers  (new)      —          →  412.5 MiB         +$0.003      +$0.08    (30 runs)
  stg_payments              1.10 GiB   →  1.10 GiB           $0.00       —

  GATE: FAIL — fct_orders_daily exceeds max increase per run ($5.00)
```

*(Illustrative output — format may change before the first release.)*

## How it works

1. **Find what changed** — dbt's `state:modified` selector against a baseline
   manifest (your production artifacts), with a git-diff fallback.
2. **Compile both versions** — the baseline and the PR branch versions of each
   changed model.
3. **Dry-run each** — BigQuery's `dryRun=true` returns exact bytes scanned.
   Dry-runs are free, execute nothing, and read no table data.
4. **Price the diff** — region-aware on-demand rates (see below), optionally
   multiplied by each model's run frequency to express $/month.
5. **Gate** — a markdown report (PR comment via the GitHub Action, terminal
   locally), machine-readable JSON, and a policy-driven exit code
   (fail on absolute $ increase and/or % increase).

## Accurate, transparent pricing

BigQuery on-demand rates differ by region. costgate ships a versioned
per-region pricing table with a `last_verified` date, auto-detects your
region from the job/profile, and **every report states the region, the rate
applied, and where that rate came from** — never a silent assumption.
Negotiated or editions pricing? Override with `pricing.usd_per_tib`.
Known limitation, stated up front: under capacity/editions pricing, bytes
scanned is a proxy signal, not your invoice.

## Security posture

This tool runs in CI next to warehouse credentials, so the design is
deliberately boring:

- **Dry-run only.** The single warehouse interaction is `jobs.insert` with
  `dryRun=true` — free, nothing executed, no table data read.
- **No credential handling.** Auth delegates entirely to Google
  [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
  — the same chain dbt-bigquery uses. Locally that's
  `gcloud auth application-default login`; in CI the documented path is
  keyless [Workload Identity Federation](https://github.com/google-github-actions/auth).
  There are no credential flags to misuse.
- **Least privilege.** The gate needs BigQuery Job User plus metadata read —
  no data access, no writes. Docs will ship the exact IAM setup.
- **Fork-safe by default.** Documented workflows use the `pull_request`
  trigger; fork PRs degrade to "no cost report", never to exposed secrets.
- **No secrets in reports.** Compiled SQL (which can embed `env_var()`
  values) never appears in comments or logs; snippets are strictly opt-in.
- **No telemetry.** The only network call is to the BigQuery API.

## Non-goals

- **Not a monitoring tool.** For retrospective cost observability, use
  [dbt-bigquery-monitoring](https://github.com/bqbooster/dbt-bigquery-monitoring) —
  costgate is the preventive half, not a replacement.
- **BigQuery only.** No Snowflake/Databricks support planned; doing one
  warehouse accurately beats doing three approximately.
- **Never runs billable queries.** Features that require executing real
  queries are out of scope by design.
- No IDE/editor integration (for now).

## Roadmap

- [ ] MVP: `costgate check` (local + CI), region-aware pricing, threshold policy
- [ ] GitHub Action wrapper with sticky PR comment
- [ ] `pre-commit` hook entry
- [ ] Docker image on ghcr.io (GitLab CI–friendly)
- [ ] Opt-in live pricing via the Cloud Billing Catalog API

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — DCO sign-off required, and note the
hard invariants: dry-run only, no credential handling, no telemetry.

## License

[Apache-2.0](LICENSE). See also [NOTICE](NOTICE).
