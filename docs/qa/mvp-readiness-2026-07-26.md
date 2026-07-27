<!-- SPDX-License-Identifier: Apache-2.0 -->

# MVP readiness review — v0.10.0, 2026-07-26

A manual QA pass over the whole CLI, driven the way an analytics engineer drives
it: from a project directory, through `init`, a config file, a real `dbt
compile`, and a gate — across every materialization and every failure mode.
110 invocations of the **packaged binary**, not the library — 74 exited 0,
18 exited 1, 18 exited 2. The full transcript is in
[session-2026-07-26.txt](session-2026-07-26.txt), so every claim below traces to
a command and its real output.

Nothing found here was fixed. That is on purpose: this document is the input to
a fixing session. Every confirmed defect also has a test in the suite, marked
`xfail(strict=True)`, so CI stays green today and turns red the moment a fix
lands with the marker still attached.

**Verdict: 6.5 / 10. Not yet ready to drop MVP status.** The reporting and
pricing layers are better than most 1.0 releases. The gate — the part that
decides whether a pull request merges — has four independent ways to report
`PASS` while checking nothing, and the config file is unvalidated in both
directions. Those are narrow, well-localised fixes, not a redesign.

---

## How this was tested, and what it could not test

- **The real binary.** A wheel built from this repo, installed next to
  `dbt-duckdb` in a throwaway virtualenv — the shape a user actually has. Worth
  stating because every existing test injects a fake through
  `main(argv, runner=...)`, and the console script never passes one, so the
  shipped entry point had not been run end to end before this.
- **Real dbt artifacts.** A real dbt project in a real git repo: `dbt seed`,
  `dbt run`, `dbt compile`, real `manifest.json` (schema v12, dbt 1.12.0), real
  `target/compiled/` files. Both incremental shapes were produced by dbt itself,
  not hand-written.
- **Only the network was faked.** `google.cloud.bigquery.Client` was swapped for
  a stand-in driven by a scenario file, so the real `BigQueryDryRunner`, the real
  error classification, the real retry object and the real exit codes all stayed
  in the path. Recorded from the client: `dry_run=True` and
  `use_query_cache=False` on every call, the retry object passed every time, and
  four models dry-run across four threads.

**What this pass cannot tell you:** whether real BigQuery responses match the
stand-in's shape, and whether the retry predicate fires against genuine 429/503s
from Google. Both need one run against a real project. Also: the artifacts came
from `dbt-duckdb`, so relation names are double-quoted where dbt-bigquery uses
backticks. One finding (F15) depends on that quoting; it was re-run against a
manifest rewritten to BigQuery backticks, and says so.

---

## Findings by severity

| # | Severity | What happens |
|---|---|---|
| [F7](#f7) | **high** | A baseline compiled the other way disarms the gate — every threshold, silently |
| [F14](#f14) | **high** | A run that estimated nothing at all still reports `PASS` and exit 0 |
| [F9](#f9) | **high** | Any config error is a Python traceback and **exit 1** — the code CI reads as "cost regression" |
| [F1](#f1) | medium | A typo in `--select` gates nothing, exit 0 |
| [F3](#f3) | medium | A change to an ephemeral model is invisible on the local path |
| [F11](#f11) | medium | `exclude: my_model` written as a string is silently ignored |
| [F12](#f12) | medium | `fail_on: no` silently becomes the strictest setting |
| [F18](#f18) | medium | Deleting a model is invisible — no row, no credit |
| [F19](#f19) | medium | A zero-byte baseline silently switches off the percentage gate |
| [F20](#f20) | medium | A brand-new model can never breach `max_pct_increase` |
| [F5](#f5) | medium | A materialized view is priced as a plain view, with no caveat |
| [F6](#f6) | medium | The net line is computed from a comparison the tool just called invalid |
| [F15](#f15) | medium | A 404 on a same-named upstream is read as the model's own table |
| [F16](#f16) | medium | `429`/`500`/`503` anywhere in an error message means "transient" |
| [F17](#f17) | medium | Nothing checks the manifest came from a BigQuery project |
| [F2](#f2) | medium | Ephemeral, seed, snapshot and Python nodes vanish without a word |
| [F10](#f10) | medium | Unknown config keys are ignored, so a misspelled threshold does nothing |
| [F13](#f13) | low | `report.format: markdwn` falls back to terminal, silently |
| [F21](#f21) | low | A sub-1% threshold prints `+0% exceeds 0%` |
| [F22](#f22) | low | `--output` to an unwritable path discards a completed run as a traceback |
| [F23](#f23) | low | `use_legacy_sql` is never set; standard SQL holds by library default |
| [F24](#f24) | low | Bytes-only reports are not ordered by size |

---

## The gate can report PASS while checking nothing

Four separate routes to the same outcome. Each is reachable through an ordinary
mistake, none of them changes the exit code, and in every case the report says
`GATE: PASS`.

### F7 — a basis mismatch disarms the gate instead of failing it {#f7}

**Severity: high.** Baseline compiled fresh (full-refresh shape), branch compiled
against the built table (incremental shape). Same model, **+264%**, **USD
+13.19/run**:

```bash
dbt-costgate check --baseline main-manifest.json --select fct_orders_daily \
  --max-usd-per-run 0.01 --max-pct 1 --max-usd-total 0.01 --max-tib-total 0.001 --fail-on fail
```

- **Expected:** exit 1. Five thresholds, all set to nearly zero, all breached.
- **Actual:** `GATE: PASS`, exit 0.
- **Control:** recompile with `--full-refresh` so both sides share a shape →
  `GATE: FAIL`, exit 1. So the numbers are right; the gate simply stops applying.
- **Cause:** `estimate.py:125` sets `gateable=False` on a basis mismatch, and
  `policy.evaluate` skips non-gateable rows. **No threshold and no `fail_on`
  value can make this block.**

The report does print `⚠ mixed basis — recompile the baseline the same way`. But
the verdict is `PASS` and the exit code is 0, and CI reads the exit code.

What makes this serious is that the setup [usage.md](../usage.md) recommends
walks straight into it: "a one-time job on merge to `main` compiles main and
stashes its `manifest.json`". If that job is a production run rather than a fresh
compile, every incremental model in the project is permanently ungated, and the
only sign is a warning line in a comment nobody re-reads.

### F14 — a run that estimated nothing still reports PASS {#f14}

**Severity: high.** Every model failing with a 404:

```
dim_new_metric  (new): — → —   not estimated
    • not estimated — an upstream table it reads has not been materialized
fct_orders_daily  (full-refresh): — → —   not estimated
GATE: PASS                                          exit 0
```

`DESTINATION_MISSING` and `UPSTREAM_MISSING` are deliberately non-operational
(`models.py:65-73`), and `cli.py:392` only escalates when **zero** models
succeeded **and** some failure was operational. So an all-404 run is a pass.

This is the most likely failure of the lot: a fresh dev schema, a wrong
`--project`, or a deferred build that never ran. The partial version is just as
quiet — 1 of 2 models denied by IAM, or rejected as invalid SQL, still gives
`PASS`/exit 0 because the other model succeeded. There is no setting that makes
"I could not estimate this model" fail the gate.

### F19 / F20 — the percentage gate has two blind spots {#f19} {#f20}

`pct_delta` returns `None` whenever the baseline is falsy (`models.py:250`),
which covers both `None` and `0`:

- **F19:** a model that scanned **0 B** on main and **2.91 TiB** on the branch
  renders `—` in the percent column, and `--max-pct 1` does not fire.
- **F20:** a **brand-new** model has no baseline, so the same thing happens.

Both are severity medium on their own and worse together: a team gating only on
`max_pct_increase` — a reasonable choice, and the *only* one available under slot
pricing, where `usd_per_tib: 0.00` makes every dollar threshold inert — is not
gating new models or zero-baseline models at all. The codebase already has the
machinery for exactly this ("this threshold cannot fire" notices,
`policy.unpriced_threshold_notice`) and does not use it here.

### F6 — the net line is built from a comparison just declared invalid {#f6}

Same run as F7. Having warned `⚠ mixed basis` and dropped the model from gating,
the report still prints:

```
Net saving: USD 4.31/run
```

That figure is a single incremental run subtracted from a full rebuild. It is
the headline number in the pull-request comment, and it is meaningless. `Report.estimated`
(`models.py:312-316`) filters on `bytes_delta` alone, so a row excluded for being
incomparable still lands in the total. The docstring's reasoning — that models
excluded from gating still spend money, so they belong in the net — is right for
`exclude`/`warn_only` and wrong for a basis mismatch, which is not a policy
choice but a statement that the two numbers cannot be subtracted.

### F1 — a `--select` typo gates nothing {#f1}

```bash
dbt-costgate check --select does_not_exist   # → "No changed models to estimate", exit 0
```

`cli.py:237-239` returns an empty list with no error. A CI job that builds its
selection list from a script — the pattern the docs recommend, piping
`dbt ls --select state:modified` — silently checks nothing the day that command
returns a stale or misspelled name.

---

## The exit-code contract

ADR-0008 gives 0 = pass, 1 = a threshold was breached, 2 = could not run. That
distinction is load-bearing: CI blocks a pull request on 1 (the author caused a
cost regression) and alerts on 2 (the gate itself is broken). Most of the CLI
honours it well — 14 distinct operational failures were checked and all returned
2 with a precise message. Two paths do not.

### F9 — every config error exits 1 with a stack trace {#f9}

**Severity: high.** Five reproductions, all `dbt-costgate check` with a bad
`.dbt-costgate.yml`:

| Config | Result |
|---|---|
| `thresholds.max_usd_increase_per_run: "five dollars"` | `ValueError`, exit 1 |
| `thresholds: [a, list]` | `AttributeError`, exit 1 |
| `pricing: 5` | `AttributeError`, exit 1 |
| `pricing.currency: EURO` | `ValueError`, exit 1 |
| malformed YAML (`  : broken`) | yaml scanner error, exit 1 |

The user sees a Python traceback naming `site-packages` paths, and CI is told the
pull request has a cost regression when it has a typo.

The fix is already in the file, applied to the wrong place: `cli.py:212-231`
validates the currency *after* config load and returns a clean exit 2, and the
comment at `cli.py:217` says it exists because this class of error "produced a
traceback instead of the documented exit 2". `Config.load` at `cli.py:301` sits
outside that try/except and raises the same class of error.

### F22 — `--output` to an unwritable path throws the run away {#f22}

Every dry-run succeeded, the report rendered, and then `cli.py:430` wrote it
unguarded: `FileNotFoundError`, exit 1. Low severity, same shape as F9.

---

## The config file is unvalidated in both directions

Wrong *types* crash (F9). Wrong *values* are silently ignored.

### F11 — a scalar `exclude` / `warn_only` is silently dropped {#f11}

`config.py:104-105` does `list(raw.get("exclude") or [])`. A string iterates one
character at a time, so `exclude: dim_new_metric` becomes
`['d','i','m','_',...]` and matches nothing.

- `exclude: dim_new_metric` → the model comes back `"gateable": true` with no
  "excluded from gating" warning. Fails safe, but ignores the user.
- `warn_only: fct_orders_daily` → **`GATE: FAIL`, exit 1**. The user asked for a
  warning and got a blocked build. That direction is not safe.

The list form works correctly in both cases.

### F12 — `fail_on: no` becomes the strictest setting {#f12}

`fail_on` is taken raw from YAML (`config.py:109`). `no` parses as the boolean
`False`, which matches neither `"never"` nor `"warn"`, so `policy.evaluate:36`
falls through to `FAIL`. Verified: `fail_on: never` → exit 0 with the breach
shown (correct); `fail_on: no` → exit 1. Someone writing `no` to mean "don't fail
my build" gets the opposite of what they asked for. `yes`, `true`, `off`, `FAIL`
and `warning` are all accepted just as silently.

### F10 — unknown keys are ignored {#f10}

`thresholds.max_usd_totl: 20` (a typo for `max_usd_total`) and a made-up
top-level key both run to exit 0 with no warning. The threshold the user believes
they configured simply is not applied.

### F13 — an unknown `report.format` falls back to terminal {#f13}

`report: {format: markdwn}` prints terminal output, no error.
`report.render:480-485` treats any unrecognised string as terminal. argparse
validates the equivalent flag; the file is not validated at all.

---

## What the tool does not see

### F3 — an ephemeral-only change is invisible locally {#f3}

**Severity: medium, and the most surprising result of the session.** A branch
that changes only `models/intermediate/int_order_items.sql` (ephemeral):

| Command | Result |
|---|---|
| `dbt-costgate check` | "No changed models to estimate", exit 0 |
| `dbt-costgate check --baseline …` | correctly selects the two downstream models and labels them |

The ephemeral model's SQL is inlined into `dim_customers` and
`fct_orders_daily`, so the cost change is real — the baseline path proves it by
finding it. Local selection follows the macro closure and the patching YAML
(`artifacts.py:205-247`) but never model-to-model dependencies, and the ephemeral
node itself was already dropped at `artifacts.py:63`.

The pre-commit hook runs exactly this path, so a push that touches only `int_*`
models passes with no signal. Intermediate models are one of the most common
places a filter gets widened.

### F2 — nodes dropped by type leave no trace {#f2}

Ephemeral, seed, snapshot and Python nodes are filtered out at
`artifacts.py:58-64` before selection, so they cannot appear in a report or a
count. A seed-only branch and a snapshot-only branch both print "No changed
models to estimate" in **both** local and baseline mode. `--select
orders_snapshot` is silent. `--select` on a Python model is silent.

Being out of scope is defensible. Being indistinguishable from "nothing changed"
is not — and snapshots do run a `MERGE` on BigQuery and do scan bytes.

### F18 — deleting a model is invisible {#f18}

A branch that deletes `dim_customers` (411.24 GiB/run, USD 2.51/run) reports
**"Net change: none"** in diff mode and "No changed models to estimate" locally.
`select_changed` (`artifacts.py:157`) iterates the current manifest only.

The README sells "when a change *lowers* cost" as a first-class story. The most
common deliberate cost reduction there is — removing a model — cannot be reported
at all.

### F5 — a materialized view is priced as a plain view {#f5}

`materialized_view` appears nowhere in `src/`. A model configured that way is
reported with `basis: direct`, no tag, no warning, and the same treatment a view
gets. But a BigQuery materialized view refreshes on its own schedule and bills
for each refresh, so the figure shown describes the initial build and not the
recurring cost — which is the number a reader of that row wants.

### F17 — nothing checks the manifest is a BigQuery project {#f17}

`metadata.adapter_type` was `"duckdb"` for this entire session and dbt-costgate
priced every model at BigQuery US on-demand rates without a word. Nothing in
`src/` reads `adapter_type`. Point it at a Snowflake, Postgres or duckdb project
and you get confident dollar figures for SQL BigQuery would never run.

---

## Error classification

### F15 — a 404 on a same-named upstream is misread {#f15}

Verified against a BigQuery-shaped manifest (backtick quoting). Table model
`dim_customers`, upstream missing at `jaffle:raw_landing.dim_customers`:

- Classified `destination_missing` — which is non-operational, so the run exits 0.
- The report says: *"incremental target not built; compile with `--defer
  --state` in a fresh target for the full-refresh estimate"* — advice about
  incremental models, printed for a table model.

`_relation_in_message` (`bigquery.py:60-65`) compares only the bare table token,
so any upstream sharing the model's table name in a different dataset trips it.

### F16 — a status code inside the message means "transient" {#f16}

`bigquery.py:52` matches the substrings `429`, `500` and `503` anywhere in the
error text. A plain syntax error whose line number happens to be 500:

```
dbt-costgate: dim_customers: transient: 400 Syntax error: … at [500:3]
dbt-costgate: could not estimate any model (check credentials/permissions).
              Try `gcloud auth application-default login`.
```

Three things wrong about one syntax error: the classification, the reported
reason ("BigQuery was unavailable and the retries ran out"), and the remediation.
In production the retry predicate would also keep retrying a permanent error
until the 60-second deadline. A table called `orders_500` or a column
`ssn_503` does the same thing.

---

## Smaller things

### F21 — `+0% exceeds 0%` {#f21}

`--max-pct 0.3` breached by a 0.4% increase produces exactly that breach line.
Both numbers render at zero decimal places (`report.py:169`, `policy.py:88`), so
a correct gate failure reads like a bug.

### F23 — standard SQL is inherited, not requested {#f23}

Recorded from the client: `use_legacy_sql` is never set on the job config. It
ends up false only because the client library defaults it; the BigQuery REST
API's own default for `useLegacySql` is true. Nothing is broken today — it is a
safety property held by a library internal rather than asserted.

### F24 — bytes-only reports are not ordered by size {#f24}

The priced markdown table puts the most expensive model first. The unpriced
(slots) table lists `dim_new_metric` (412 MiB) above `fct_orders_daily`
(2.91 TiB).

---

## Working as intended, but worth a second look

- **`fail_on: warn` is behaviourally identical to `fail`.** Both return exit 1
  on a breach; only the printed label differs (`policy.py:35-36`). The config
  template describes `warn` as "fails on warnings", but warnings are never an
  input to `policy.evaluate` at all. The documented three-way choice is really
  a two-way one.
- **A missing `--baseline` file is explained in terms of `--current`.**
  `dbt-costgate check --baseline ../nope.json` answers "No manifest.json at
  ../nope.json. Run `dbt compile` first, then point `--current` at the target/
  directory" — telling the user to fix a flag they got right.
- **"Compiled in a fresh target" is not what decides the basis.**
  [usage.md](../usage.md) says an incremental compiles full-refresh "in a fresh
  target". Deleting `target/` and recompiling still produced the incremental
  shape, because dbt's `is_incremental()` keys off the relation existing in the
  warehouse. `dbt compile --full-refresh` is what actually produces the
  full-refresh shape.
- **Model-level `usd_per_run_delta` is populated in absolute mode**, where there
  is no baseline to have a delta from. The run-level `net` block is correctly
  `null` there; the per-model field is not.

## Checked and found correct

Worth recording so the next session does not re-chase them.

- `--threads 0` and `--threads -5` degrade to serial (`estimate.py:44` guards on
  `threads <= 1`). Verified with three models.
- A truncated `manifest.json` gives a clean exit 2 with a precise message,
  despite the branch being marked `# pragma: no cover - defensive`.
- Non-ASCII (CJK, accented) and 120-character model names render correctly in all
  three formats. The terminal renderer writes free-form rows, not padded columns.
- `net.*` being `null` in JSON absolute mode is deliberate and documented.
- All four rename failure modes (valid, unresolvable, ambiguous, many-to-one)
  behave correctly, exit 2, with actionable messages.
- Named baselines resolve in the documented precedence, with good errors for an
  unknown or malformed target.
- **Secret redaction holds.** An error carrying `sk-live-…` appears zero times in
  terminal, markdown and JSON stdout, and once on stderr.
- **The retry path works.** Two 503s then success went through the production
  `Retry` object and returned the right bytes.
- Region auto-detection, the EU rate, the unknown-region fallback notice, the
  slots/bytes-only mode, the dead-money-thresholds notice and the
  currency-soundness refusal (exit 2) all behaved exactly as documented.
- All four markdown table shapes render correctly.
- `dbt-costgate config`, `config --format json` and `init` (including its refusal
  to overwrite) are complete and well written.

---

## What changed in the test suite

No `src/` change. Five new test files, 292 → 378 tests: 48 new passing tests and
38 marked `xfail(strict=True)`, one per confirmed defect, each asserting the
behaviour we want rather than the behaviour we have. `ruff check`, `ruff format
--check` and the sample-drift gate all pass.

| File | Covers |
|---|---|
| `tests/test_materializations.py` | every materialization, including `table`, `materialized_view` and custom ones, which had no coverage at all; basis detection driven by compiled shape |
| `tests/test_cli_edges.py` | the four silent-disarm paths; the thread pool, which no test had ever entered; a partly-failed run; `--output` |
| `tests/test_config_validation.py` | six wrong-type configs and five wrong-value ones, plus controls that must keep passing |
| `tests/test_report_edges.py` | petabyte figures, zero bytes, unicode and 120-char names, sub-1% thresholds, a sign invariant over a spread of byte pairs |
| `tests/test_bigquery_client.py` | `BigQueryDryRunner` itself — the request it builds, both `except` clauses, the retry object, and the two misclassifications |

Also worth knowing: the suite has no coverage measurement and no type checking,
and `tests/conftest.py:126` defines a `fake_runner` fixture that no test uses.

---

## Score

| Area | Score | Why |
|---|---|---|
| Pricing and cost maths | 8.5 | Region precedence, provenance on every figure, currency soundness, slots mode, free-tier disclosure — all correct and unusually well reasoned. Petabyte figures priced exactly. |
| Reporting | 8.5 | Four table shapes, basis tags, collapsed footnotes, net line, notices with stable ids. Readable and honest about its own limits. |
| Security | 9 | Redaction verified across all three formats. No `shell=True` anywhere, ADC only, least-privilege IAM documented, `S602` locked on by a test. |
| Documentation | 8 | Excellent, and the generated samples really are generated. Docked for the "fresh target" claim, `fail_on: warn`, and never stating that seeds/snapshots/ephemerals are out of scope. |
| CLI and messages | 7.5 | Where messages exist they are among the best I have read. Docked for the ones that misdirect, and for the silent drops. |
| Packaging | 8.5 | Built and installed as a wheel next to dbt and ran correctly, `pricing.json` included. Release machinery not exercised here. |
| Error handling and exit codes | 4 | 14 operational paths return a clean 2; config errors and `--output` return 1 with a traceback, colliding with the cost-regression code. |
| Config handling | 4 | Unvalidated in both directions: wrong types crash, wrong values are ignored. |
| **Gate correctness** | **4** | Four independent ways to report `PASS` while gating nothing. This is the product's one job. |
| **Overall** | **6.5** | |

### Is it ready to leave MVP?

Not yet — but it is closer than the score suggests, because the weak areas are
narrow and the strong ones are structural. Nothing here calls for a redesign.

**Three blockers.**

1. **The gate must not silently stop gating** (F7, F14, F1). A basis mismatch, a
   run that estimated nothing, and an unmatched `--select` should each be either
   a failure or an explicit, configurable decision — never a quiet `PASS`. The
   minimum credible fix is a strictness setting that treats "could not check
   this model" as a breach, defaulting to on.
2. **Honour the exit-code contract** (F9, F22). Wrap `Config.load` and the
   `--output` write the way the currency check is already wrapped. This is a
   handful of lines and it is the difference between CI blaming the author and CI
   blaming the pipeline.
3. **Validate the config file** (F11, F12, F10, F13). Coerce a scalar to a
   one-item list, reject a `fail_on`/`report.format` that is not in the allowed
   set, and reject unknown keys. The registry to check against —
   `CONFIG_REFERENCE` — already exists and is already complete.

**Should follow soon, but need not block:** deletions in the diff (F18), the two
percentage blind spots (F19/F20), `materialized_view` (F5), a word when a node is
dropped by type (F2/F3), the two misclassifications (F15/F16), and an
`adapter_type` check (F17).

**Before calling it 1.0, do one run against real BigQuery.** Everything except
the network was exercised here, and the network is the one thing a stand-in
cannot vouch for.
