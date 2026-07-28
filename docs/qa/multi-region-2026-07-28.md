<!-- SPDX-License-Identifier: Apache-2.0 -->

# Multi-region pricing, driven end to end — 2026-07-28

[The live BigQuery run](live-bigquery-2026-07-27.md) closed with a caveat it was
careful to state plainly:

> **EU pricing driven by an EU-resident table.** `job.location` round-trips
> `'EU'`, but with no EU dataset available the rate lookup was not driven end to
> end by a genuinely EU-hosted query.

That is now closed, and closing it found a defect in the most prominent line of
the report.

## How it was driven

Two datasets were created in the test project, one in `EU` and one in
`asia-northeast1`, each holding a 500-row table. **Nothing here ran a billable
query.** Creating a dataset is a metadata call; the rows arrived through a load
job, which BigQuery does not bill per byte; and every measurement below is a
`dry_run`. The datasets were deleted afterwards — the project has no datasets of
its own, as before.

**`asia-northeast1` is the region that matters, and EU alone would have proved
nothing.** EU is priced at USD 6.25/TiB — identical to the US default — so an
EU-only check cannot tell a working region lookup from one that silently falls
back. `asia-northeast1` is 7.50. That is the difference between a measurement and
a coincidence, and it is the same trap this project has hit before: a test that
passes because it cannot distinguish success from the default.

## What passed

Three real regions, three real dry-runs, rates resolved from the built-in table:

| Query resident in | `job.location` | Rate applied | Source |
|---|---|--:|---|
| `EU` | `EU` | USD 6.25 | `region-table` |
| `asia-northeast1` | `asia-northeast1` | **USD 7.50** | `region-table` |
| `US` (public dataset, control) | `US` | USD 6.25 | `region-table` |

End to end through the packaged CLI, one model per region against a baseline:

```
dbt-costgate — region: EU, US, asia-northeast1 · on-demand USD 6.25–7.50/TiB · built-in table

  MODEL      BASELINE     CURRENT    Δ %    Δ / RUN
  ────────  ─────────  ──────────  ─────  ─────────
  fct_us    62.88 MiB  105.24 MiB   +67%  USD +0.00
  fct_asia   3.91 KiB   12.10 KiB  +210%  USD +0.00
  fct_eu     3.91 KiB   12.10 KiB  +210%  USD +0.00

  Pricing: EU USD 6.25/TiB · US USD 6.25/TiB · asia-northeast1 USD 7.50/TiB · built-in table
```

Each region detected from its own job, each priced at its own rate, in one
report.

## What it found

**The header stated one rate for every region it named.**

The line above reads `USD 6.25–7.50/TiB` because of this run. Before it, the
header took the *first* region's rate and presented it as the rate — beside a
list naming all three:

```
dbt-costgate — region: EU, US, asia-northeast1 · on-demand USD 6.25/TiB · built-in table
```

The built-in table spans **1.8×**, from US at 6.25 to `southamerica-east1` at
11.25. So a change touching both announced `USD 6.25/TiB` in the report's most
prominent line while nearly half of it was priced at nearly double.

The footer had always been right. `_disclosure_line` breaks the rate out per
region and has done since regions were added; only the summary above it claimed
a single number, and nothing compared the two. The fix is a range when the rates
disagree and a single figure when they do not, so the common case is untouched.

Four tests pin it, including one asserting that the header and the footer can
never disagree about which rates apply.

## Still not proved

**A region priced from a `fallback` source.** Every region here resolved from the
built-in table. The fallback path — an unlisted region taking the disclosed
default — is covered by unit tests but has not been driven by a real job in a
region the table does not list.

**A genuinely closed table.** Unchanged from the earlier run: every 403 obtainable
here still comes from a name that does not exist rather than from a table that
exists and is denied.
