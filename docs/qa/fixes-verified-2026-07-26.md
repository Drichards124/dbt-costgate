<!-- SPDX-License-Identifier: Apache-2.0 -->

# Fixes verified — 2026-07-26

The companion to [mvp-readiness-2026-07-26.md](mvp-readiness-2026-07-26.md),
which found 21 defects and fixed none of them on purpose. All 21 are fixed. This
records how each was checked.

**Two layers of evidence.** Every defect has a test in the suite that asserts the
behaviour we want — those tests were written during the QA pass and carried
`xfail(strict=True)`, so each one turned red the moment its fix landed and the
marker came off. The suite now carries **zero** xfail markers, which is the
mechanical statement that nothing on the list is still outstanding: 292 tests at
the start of the QA pass, 461 now.

On top of that, the ones that were originally found by *driving the binary* were
re-run against the fixed binary in the same harness — a wheel built from this
repo, installed beside `dbt-duckdb`, with only `google.cloud.bigquery.Client`
faked.

## What the re-run showed

| # | Before | After (packaged binary) |
|---|---|---|
| F7 | `GATE: PASS`, exit 0, with five thresholds at nearly zero | `GATE: FAIL`, exit 1, naming the mismatch and how to fix it |
| F14 | every model 404s → `GATE: PASS`, exit 0 | `nothing was checked: none of the 2 selected models could be gated`, exit 1 |
| F1 | `--select fct_orders_dialy` → "No changed models", exit 0 | `--select matched nothing for: fct_orders_dialy (did you mean fct_orders_daily?)`, exit 2 |
| F2 | `--select orders_snapshot` → silence | `orders_snapshot — snapshots are not priced`, exit 2 |
| F3 | ephemeral-only branch → "No changed models" locally | both downstream models selected, each saying why |
| F6 | `Net saving: USD 4.31/run` from an invalid comparison | no net line at all |
| F9 | `ValueError` traceback, exit 1 | `thresholds.max_usd_increase_per_run: expected a number, got 'five dollars'`, exit 2 |
| F10 | `max_usd_totl` ignored | `unknown setting … Did you mean thresholds.max_usd_total?`, exit 2 |
| F11 | `exclude: fct_orders_daily` matched nothing | the model is excluded, as written |
| F12 | `fail_on: no` → strictest setting | rejected, naming the YAML-boolean trap, exit 2 |
| F13 | `report.format: markdwn` → terminal output | rejected with the allowed values, exit 2 |
| F17 | duckdb manifest priced at BigQuery rates | refused, exit 2 |
| F18 | deleting a model → "Net change: none" | `dim_customers deleted 0 B -100% USD -2.51`, and a net saving |
| F22 | unwritable `--output` → traceback, exit 1 | clean message, exit 2 |

The rest (F5, F15, F16, F19, F20, F21, F23, F24) are unit-level and are covered
by the suite; several are hard to reach through the CLI at all — F23 is a field
on a job config, F15 and F16 are error classification.

**F20 resolved differently from the way its test demanded.** A brand-new model
cannot be covered by `max_pct_increase` — no baseline, no percentage. Failing the
run was the first instinct and is wrong: adding a model is ordinary, and blocking
every pull request that does one teaches a team to switch the gate off. It is a
notice instead, naming the models that went through ungated and the two
thresholds that need no baseline. That test's assertion was rewritten rather than
its marker removed, and it says so.

## What changed in the harness, and why it matters

The demo project is `dbt-duckdb`, and the F17 fix means the tool now refuses a
manifest compiled for another warehouse. That is correct — pricing a duckdb
project at BigQuery rates is a confident wrong answer — so the harness gained a
shaping step (`bqify.py`) that rewrites a duckdb `target/` into the shape a
BigQuery project produces.

The first version of it rewrote `relation_name` to backticks and left the
compiled SQL double-quoted. `detect_basis` decides whether an incremental was
compiled fresh or against its existing table by looking for the relation inside
its own compiled SQL, so every incremental came out `full-refresh` and the
basis-mismatch case quietly became a no-op — a harness bug that reads exactly
like a passing product. Both halves have to move together. Worth recording
because the same trap is available to anyone rebuilding this rig.

## Still not proved

Unchanged from the first pass: everything except the network was real, and the
network is the one thing a stand-in cannot vouch for. Whether BigQuery's actual
responses match the fake's shape, and whether the retry predicate fires on
genuine 429/503s, needs one run against a real project before calling this 1.0.
