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

## The second pass — the whole walkthrough, not just the defects

The table above re-ran the fourteen cases that had been *defects*. That leaves
the other ninety-odd commands of the original walkthrough — the ones that were
already fine — unaccounted for, and those are exactly where a regression would
hide. So the walkthrough was driven again end to end: 94 commands, all three
exit codes, every scenario file in the rig.

Everything that worked before still works. Two things did not, and both are in
code these fixes introduced:

- **`exclude:` handed an invalid comparison back to the headline.** `exclude:`
  has to overwrite the skip reason — that is what makes it the escape hatch for
  a model the gate cannot check. It was overwriting `basis_mismatch` too, and
  the net total was reading the skip reason to decide what it could add up. So
  the same report that said "their two figures answer different questions" then
  printed `Net increase: USD 13.19/run` — F6 again, through a different door,
  and only for someone who had reached for the escape hatch. Comparability is
  now its own flag, set before the exclusion is applied.

- **A plural count met a singular pronoun.** The run-level breach reuses the
  per-model reason text, and one of those reasons began "its dry-run…", giving
  `none of the 2 selected models could be gated — its dry-run did not return a
  size`. The reasons are now written without a pronoun so they read in both
  frames.

Both are fixed, tested and re-checked against a rebuilt wheel.

Two cases the *harness* failed to reach on the first attempt, worth writing down
because neither failure looked like a failure: a `git checkout` was blocked by
dbt's own untracked `.user.yml`, so the materialized-view branch silently stayed
on the previous branch and reported "no such model"; and a `baselines:` entry
written as a bare path instead of `manifest:` was correctly rejected, which meant
the named-baseline happy path never ran. Both were re-run afterwards and pass.

## Still not proved

Unchanged from the first pass: everything except the network was real, and the
network is the one thing a stand-in cannot vouch for. Whether BigQuery's actual
responses match the fake's shape, and whether the retry predicate fires on
genuine 429/503s, needs one run against a real project before calling this 1.0.

**Answered on 2026-07-27** — see
[live-bigquery-2026-07-27.md](live-bigquery-2026-07-27.md). The response shape
and the error classification are confirmed against real BigQuery, and one defect
came out of it that no fake could have produced. A real 429 or 503 in flight is
still unobserved: you cannot make BigQuery rate-limit you on demand, so the
retry predicate is checked against real exception instances instead. That is the
one caveat that survives.

One narrower caveat from the second pass: `--against <ref>` compiles the ref with
the project's own adapter, which in this rig is duckdb, so the baseline it
produces is not shaped like the branch and the run reports a basis mismatch.
That is the harness, not the tool — but it is also a fair demonstration that the
mismatch is no longer something the gate stays quiet about.
