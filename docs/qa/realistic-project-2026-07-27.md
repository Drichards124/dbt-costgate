<!-- SPDX-License-Identifier: Apache-2.0 -->

# A realistic dbt project — 2026-07-27

> **Archived QA record — not current documentation.** Kept exactly as written
> on the date in the title. It records what was true then, is cited from the
> changelog as evidence for that, and is deliberately not updated as the tool
> changes. For how the tool behaves now see [the usage guide](../usage.md) and
> [the changelog](../../CHANGELOG.md); for what these files are, see
> [README.md](README.md).

The [live BigQuery run](live-bigquery-2026-07-27.md) proved the network edge but
ran on four toy models. That leaves the tool's subtlest logic untested: what an
*incremental* model costs. [models.py](../../src/dbt_costgate/models.py) carries
a comment about a past bug in exactly that area — "a model compiled in
incremental form was tagged `full-refresh`" — and nothing had ever checked it
against a warehouse.

So: a project shaped like one someone would actually have. 40 models — 10 views,
20 tables, 5 ephemeral, 5 incremental — plus a snapshot, a seed, and macros that
change the compiled SQL. All reading `bigquery-public-data.usa_names`.

Unlike every earlier pass, the models were **materialized**, because the
incremental branch only compiles once its target table exists. 35 models built in
18 seconds, roughly 2.6 GB processed. The dataset was deleted afterwards.

## The incremental question, both ways

`dbt compile` produces different SQL for an incremental model depending on
whether its target already exists. Both forms were checked against real BigQuery.

**Target absent** — dbt compiles the full-refresh form:

```
  MODEL                         SCANNED  COST / RUN
  fct_incr_00  full-refresh  105.90 MiB    USD 0.00

  ⚠ full-refresh — rows tagged full-refresh show what it costs to build the whole
    table from scratch. A normal incremental run scans much less, so read this as
    the ceiling rather than the nightly bill.
```

**Target present** — the `is_incremental()` branch compiles in, and the tag
follows the SQL rather than the config:

```
  MODEL                        SCANNED  COST / RUN
  fct_incr_00  incremental  105.94 MiB    USD 0.00

  ⚠ incremental — rows tagged incremental show one run against a table that
    already exists. A full rebuild scans far more, and nothing here measures it,
    so no threshold on this report can catch a rebuild getting expensive.
```

The tag flipped, and the caveat flipped with it. **The basis logic is correct**,
and it is reading the compiled SQL rather than trusting `materialized:
incremental` — which is the distinction the old bug got wrong.

**One result worth dwelling on.** The incremental estimate is *not smaller*:
105.94 MiB against 105.90 MiB. The filter is the standard dbt idiom —

```sql
where year > (select coalesce(max(year), 0) from {{ this }})
```

— and BigQuery cannot evaluate that subquery when planning, so it still scans the
whole source, plus the target table. The dry-run is telling the truth; the
incremental run really will scan that much. dbt-costgate flags it without being
asked:

```
  ⚠ fct_incr_00  dynamic filter — dry-run may be worst-case (overestimate)
```

That warning was written against a stand-in. It turns out to fire on the single
most common incremental pattern in dbt, and to be right when it does.

## The defect this found

**`--select` discarded the entire report if one named resource could not be
priced.**

| Selection | Exit | Rows reported |
|---|--:|--:|
| 16 real models | 0 | 16 |
| the same 16 **plus one ephemeral** | 2 | **0** |
| a snapshot on its own | 2 | 0 |
| a seed on its own | 2 | 0 |

Sixteen models had answers and none were shown. The change-detection path, given
the identical situation, names the unpriced node on stderr and carries on — so
the same ephemeral produced opposite outcomes depending on how it was selected.
Worse, the message says the cost "shows up there" in the downstream models, and
then `--select` declined to show you there, while change detection actually pulls
the downstream model in and labels it.

It is reachable from an ordinary CI line: `dbt ls --select state:modified
--resource-type model` emits ephemerals.

Fixed by splitting the two kinds of miss. A name nobody recognises is still a
usage error at exit 2 with a spelling suggestion; a name that resolves to a real
seed, snapshot or ephemeral is reported and the run continues. A selection
containing *only* unpriced names is still exit 2, because then nothing was gated
and that must stay loud.

## What else held up

- **Scale.** 35 models in **3.2 s** wall clock at 8 threads (4.9 s CPU — really
  parallel). No BigQuery concurrency limit reached, no degradation.
- **Macros.** A macro that rewrites a `where` clause changes the compiled SQL,
  and the affected models were detected and priced.
- **Ephemeral fan-out.** Changing an ephemeral selected the model that inlines
  it, labelled *"selected because an ephemeral model it inlines changed — the
  model's own file is untouched"*.
- **Unbuilt dev schema.** Marts reading a dataset that did not exist returned
  `UPSTREAM_MISSING`, were listed as not estimated, and did not fail the run.
- **Seeds and snapshots.** Both named with their own reason when changed.

## Still not covered

- A full rebuild of an incremental model is not measured by anything, and the
  report says so rather than implying otherwise. Gating a rebuild would need a
  second dry-run of the full-refresh SQL, which is a feature, not a fix.
- A real 429 or 503 retried in flight. 35 concurrent dry-runs were not enough to
  reach a quota limit.
- Multi-region pricing driven by a genuinely non-US table.
