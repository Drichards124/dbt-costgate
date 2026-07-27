# Changelog

All notable, user-visible changes to `dbt-costgate` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
From 1.0 on, a breaking change means a new major version; releases up to and
including 0.11.0 were pre-1.0, where minor versions could and did break things.

## [Unreleased]

### Fixed

- **`--select` no longer throws away the whole report because one name cannot be
  priced.** Naming a seed, a snapshot or an ephemeral model alongside real models
  used to end the run at exit 2 with nothing printed — even when sixteen other
  models had answers. That is reachable from an ordinary CI line, because
  `dbt ls --select state:modified --resource-type model` includes ephemerals.

  Those names are now reported on stderr and the run carries on, which is what
  the change-detection path has always done:

  ```
  dbt-costgate: int_names_00 was selected but is not priced — ephemeral models
    have no relation of their own; their SQL is inlined into the models that
    select from them, and the cost shows up there.
  ```

  **Two things deliberately did not change.** A `--select` naming *only* unpriced
  nodes is still exit 2 — nothing was gated, and that has to stay loud. A name
  nobody recognises is still exit 2 with a spelling suggestion, so a stale list
  cannot quietly check nothing.

- **A BigQuery outage no longer hangs your CI job for up to forty minutes per
  model.** `deadline_seconds` (default 60) was bounding nothing. `Client.query`
  takes two independent retries and dbt-costgate set only one, leaving
  `job_retry` at its 2400-second default to re-drive the whole thing underneath.
  Measured against real BigQuery, a 5-second deadline ran for **179 seconds over
  42 attempts**. It now gives up inside the deadline — the same case takes 2.1
  seconds.

- **A dry-run that BigQuery could not serve now says so.** When the retries run
  out, the client raises a `RetryError` wrapping the real failure, which fell
  through every check and was reported as `the dry-run failed`. You now get the
  message that was always meant for this:

  ```
  not estimated — BigQuery was unavailable and the retries ran out
  ```

  This affects the reported reason only. That kind was already treated as an
  operational failure, so no exit code or verdict changes.

## [1.0.0] - 2026-07-27

**1.0 because the last thing standing in its way is done.** The v0.11.0 notes
said the number was blocked on one thing: every test faked
`google.cloud.bigquery.Client`, so the one part of this tool that talks to a
warehouse had never met a warehouse. It has now — see
[docs/qa/live-bigquery-2026-07-27.md](docs/qa/live-bigquery-2026-07-27.md). The
dry-run response parses, the error classification holds against live BigQuery
exceptions, and an end-to-end run's byte counts match a direct dry-run exactly.
It also turned up the bug below, which is the argument for doing it.

Nothing here changes a gated figure, a threshold, an exit code or a verdict.
1.0 is a statement about confidence and about the promise that comes with it —
from here, breaking changes wait for 2.0.

### Fixed

- **A mistyped dataset name no longer tells you to log in again.** BigQuery
  answers `403` — not `404` — for a dataset or project that does not exist, so a
  typo used to end the run with `could not estimate any model (check
  credentials/permissions). Try gcloud auth application-default login` and exit
  2. The exit code is unchanged, but both messages now name the other cause:

  ```
  not estimated — BigQuery refused the dry-run — either it is not allowed, or
  the dataset or project does not exist
  ```

  Check the names in your `--select` and your model SQL before reaching for IAM.

- **`max_tib_total` and `max_usd_total` breaches now name numbers you can tell
  apart.** Both sides were rendered at two decimal places, so a small cap
  produced a breach line that compared a number to itself:

  ```
  - fct_names: 0.00 TiB/run exceeds cap 0.00 TiB
  - fct_names: USD 0.00/run exceeds cap USD 0.00
  ```

  A 1 GiB ceiling (`max_tib_total: 0.001`) is an ordinary thing to set and lands
  right in that range. Byte figures now use the same units as the table, and
  money widens its decimals only when two places would round an amount away:

  ```
  - fct_names: 147.61 MiB/run exceeds cap 104.86 MiB
  - fct_names: USD 0.0009/run exceeds cap USD 0.0001
  ```

  Ordinary caps are unchanged — `USD 0.84/run exceeds cap USD 0.50` still reads
  at two places. Only the wording moved; gating, exit codes and verdicts are
  identical.

## [0.11.0] - 2026-07-26

### Added

- **You can declare your BigQuery free tier.** `pricing.free_tib_per_month` tells
  dbt-costgate what your on-demand allowance is, and the report shows where the
  change lands against it:

  ```yaml
  pricing:
    free_tib_per_month: 1
  run_frequency:
    default: 30      # required — an allowance is per month, so it needs a month
  ```

  ```
  dbt-costgate — region: US · on-demand USD 6.25/TiB · built-in table · first 1 TiB/month free

    Net increase: USD 13.19/run · USD 395.63/month
    Monthly scan for these models: 85.35 TiB — past the 1 TiB/month you declared free
  ```

  **It subtracts nothing.** No cost figure, threshold, verdict or exit code
  changes — set it and the gate behaves exactly as it did. The allowance belongs
  to your whole billing account and is drawn down by every other query anyone
  runs, which dbt-costgate cannot see, so deducting it would mean assuming it is
  still unspent. A gate that forgives the first TiB of a regression on an
  unverified assumption is worse than one that over-reports honestly.

  Three things worth knowing before you set it. *"For these models"* is literal —
  a pull request covers a handful of models, so the figure is not your project's
  monthly scan and certainly not your account's. Without a `run_frequency` there
  is no monthly figure to compare against, and the new
  `free-tier-needs-run-frequency` notice says so rather than letting the setting
  sit there doing nothing. And the tier is an on-demand allowance, so all of this
  is suppressed under capacity/Editions pricing.

  Unset — the default — changes nothing anywhere.

- **`--format json` gains two fields**: `pricing.free_tib_per_month` (what you
  declared, or `null`) and `net.monthly_scan_bytes` (raw bytes these models are
  projected to scan in a month, or `null` when no run frequency is set). Unlike
  the other `net` figures, the monthly total is absolute rather than a difference,
  so it is present in absolute mode too.

---

A manual QA pass drove the packaged CLI end to end against a real dbt project
and found 21 defects. This release fixes all of them, and rebuilds the terminal
report around a readable table.

**Read the breaking changes first if you run dbt-costgate in CI** — a gate that
could not check a model now fails instead of passing, and that is deliberate.

### Breaking

- **A gate that could not check a model now fails the run (exit 1).** Previously
  it reported `GATE: PASS` and exit 0. There were four ways into this and all of
  them are ordinary mistakes: a baseline compiled a different way from the
  branch, a model whose dry-run returned no size, a baseline with no compiled
  SQL, and a run where nothing at all could be estimated. The report warned in
  some of these cases, but CI reads the exit code.

  In practice this fires when a threshold is configured and a model could not be
  measured. If that is expected for a particular model — an external table your
  service account cannot see, say — accept it by name:

  ```yaml
  exclude:
    - external_events
  ```

  To stop blocking entirely while you sort it out, `fail_on: never` reports the
  breaches and exits 0. There is no new setting to learn.

  One case fails regardless of thresholds: a run where **every** selected model
  failed to estimate. `PASS` there does not mean "I checked and found nothing
  wrong" — it means the gate never ran. The usual causes are an unbuilt dev
  schema, the wrong `--project`, or a deferred build that never happened.

- **A config error now exits 2 instead of 1.** ADR-0008 reserves 1 for a
  threshold breach; a malformed `.dbt-costgate.yml` was exiting 1 with a Python
  stack trace, so CI reported a YAML typo as a cost regression. A failed
  `--output` write moved for the same reason.

- **`.dbt-costgate.yml` is now validated, and rejects what it used to ignore.**
  A file that was quietly half-applied will now fail with a message naming the
  key. Three things change:
  - `exclude: my_model` written as a bare string used to become a list of single
    characters and match nothing. A scalar is now read as a one-item list, so
    this works as written.
  - `fail_on: no` is YAML for the boolean false. It matched neither `never` nor
    `warn` and fell through to the strictest setting — the opposite of what
    someone writing "no" means. `fail_on` and `report.format` are now checked
    against their allowed values.
  - An unknown key is refused, with the nearest documented key as a hint. A typo
    like `thresholds.max_usd_totl` used to leave you with no threshold at all.

- **A manifest compiled for another warehouse is refused (exit 2).** Nothing
  checked `metadata.adapter_type`, so pointing dbt-costgate at a Snowflake,
  Postgres or duckdb project produced confident BigQuery dollar figures for SQL
  BigQuery was never going to run. Manifests without the field still run.

### Changed

- **The terminal report is a table.** It used to write each model as a sentence,
  so figures started at a different column on every row, nothing lined up, and a
  priced diff row ran to 108 characters and wrapped:

  ```
    fct_orders_daily  (full-refresh): 819.20 GiB → 2.91 TiB   +264%   USD +13.19/run   USD +316.46/month (24 runs)
  ```

  Now:

  ```
    MODEL                             BASELINE     CURRENT    Δ %     Δ / RUN    Δ / MONTH  RUNS
    ────────────────  ────────────  ──────────  ──────────  ─────  ──────────  ───────────  ────
    fct_orders_daily  full-refresh  819.20 GiB    2.91 TiB  +264%  USD +13.19  USD +395.63    30
  ```

  Per-model warnings moved below the table, into a `NOTES` block keyed by model.
  Nothing was removed — `(24 runs)` became the `RUNS` column.

  It adapts to the terminal width, giving up columns in a fixed order and saying
  which it hid; the model name and the per-run cost are never among them. Below
  60 columns it prints one block per model instead. Output redirected to a file
  or a pipe always renders at a fixed width, so a captured report does not depend
  on the window that produced it.

- **Percentages are printed at the precision they need.** A 0.4% increase over a
  `max_pct_increase: 0.3` limit used to produce the breach line `+0% exceeds 0%`,
  which reads like a bug rather than a correct failure.

- **Unpriced (slot-priced) reports are ordered by size.** With every rate at 0
  the sort had nothing to work with and rows came out in arrival order.

- **The `incremental` and `full-refresh` footnotes say which figure is the big
  one.** Every fact in the old wording was correct — "the figure is one run
  against the table as already built, so it does not gate rebuild cost" — and it
  took a second read to work out that the number on screen is the cheap case and
  the expensive one is not in the report at all. Each now says what the figure
  is, what it is not, and which of the two is larger, in that order:

  > incremental — rows tagged incremental show one run against a table that
  > already exists. A full rebuild scans far more, and nothing here measures it,
  > so no threshold on this report can catch a rebuild getting expensive.

### Added

- **`--color auto|always|never`** (default `auto`): colour when stdout is a
  terminal, off when piped, and off whenever `NO_COLOR` is set.

- **Deleting a model is reported as the saving it is.** Removing a model is the
  most direct cost reduction a change can make, and it produced no row and no
  credit — a branch deleting a 411 GiB/run model reported `Net change: none`.
  Deleted models are dry-run from the baseline, tagged `deleted`, and never
  gated: a removal cannot raise cost.

- **A change to an ephemeral model now selects the models that inline it.** Its
  SQL ends up inside them, so widening a filter there is a real cost change —
  but on the local path, which is what the pre-commit hook runs, it selected
  nothing at all.

- **A changed seed, snapshot, Python model or ephemeral model is named.** These
  are out of scope for pricing, which is fine; being indistinguishable from
  "nothing changed" is not, and a snapshot really does run a `MERGE`.

- **A `--select` name that matches nothing is an error (exit 2)**, listing the
  names with a suggestion for near misses, and saying when the model exists but
  is a kind dbt-costgate does not price. It used to select nothing in silence, so
  a CI job building its list from `dbt ls` checked nothing the day that list went
  stale.

- **A materialized view says its figure is not the recurring cost.** It is priced
  like a plain view, but BigQuery bills each automatic refresh separately.

- **New notice `new-models-not-percentage-gated`.** `max_pct_increase` needs a
  before and an after, so it cannot cover a model that has no baseline. If it is
  your only threshold, the report now names the new models that went through
  ungated and points at `max_usd_total` / `max_tib_total`, which need no
  baseline. Silence it like any other notice.

- **JSON gains `skip_reason` and `is_deleted` per model.** `gateable` alone could
  not tell a consumer whether you excluded a model or the gate could not measure
  it, and those mean opposite things on a dashboard.

### Fixed

- **A model that scanned 0 B on the baseline now breaches `max_pct_increase`.**
  There is no ratio to a zero baseline, so the threshold silently did not apply
  to exactly the model it should catch hardest.

- **The net line no longer totals a comparison the report just called invalid.**
  Having warned that a model's two figures cannot be subtracted, it went on to
  headline the subtraction. Naming that model in `exclude:` does not bring the
  total back either — an exclusion says "do not fail the build over this", not
  "the two figures are comparable after all".

- **A permanent SQL error is no longer reported as a transient one.** The status
  code was matched anywhere in the message, so `400 Syntax error … at [500:3]`
  came back as "BigQuery was unavailable and the retries ran out" — and would
  have been retried until the deadline. A table called `orders_500` did the same.

- **A 404 on a different dataset's same-named table is no longer read as the
  model's own.** With a staging/marts layout that misread a broken run as an
  expected one, and the advice printed was about incremental models.

- **Standard SQL is requested explicitly.** It held only because the client
  library defaults it; the REST API's own default is the opposite.

- **A missing `--baseline` file is explained in terms of `--baseline`**, not
  `--current` — it used to tell you to fix a flag you had got right.

- **Per-model deltas are `null` in absolute mode**, matching the run-level `net`
  block. There is no baseline there to have a delta from.

### Docs

- `fail_on`'s description now says what it does: `warn` and `fail` both exit 1 on
  a breach and differ only in the printed label. Warnings are not an input to the
  gate.
- `usage.md` no longer says a fresh `target/` produces the full-refresh form of
  an incremental. dbt decides that from whether the relation exists in the
  warehouse; `dbt compile --full-refresh` is what actually produces it.

## [0.10.0] - 2026-07-26

### Security

- **Reports no longer reproduce BigQuery's error message.** A model that could
  not be dry-run is now described by what went wrong, not by what BigQuery said:

  ```
  > • **fct_orders** — not estimated — BigQuery rejected the compiled SQL
  ```

  where it previously printed the message verbatim. BigQuery quotes the query it
  was given, and compiled SQL can embed secrets templated through `env_var()` or
  vars — the same reason reports have always excluded compiled SQL. Because a
  report becomes a pull-request comment, that message could carry query text,
  dataset and table names, and the service-account identity into a comment that
  is public on a public repository.

  **The full message is still available**, printed to stderr and so kept in the
  job log:

  ```
  dbt-costgate: fct_orders: invalid_sql: 400 Syntax error: Unexpected ...
  ```

  This changes report text in all three formats, including the JSON `error`
  field. If you match on that string, match on the new wording — or read
  `models[].error` alongside the job log rather than parsing the warehouse
  message out of a comment.

### Fixed

- **A report long enough to be truncated now posts.** The sticky comment is
  capped at GitHub's size limit; the cut could land inside a multi-byte
  character, and the resulting comment was rejected. It is now shortened to a
  whole line.

## [0.9.0] - 2026-07-26

### Added

- **`models[].basis` in the JSON report** — `full_refresh`, `incremental_form`,
  `direct`, or `null` when it could not be established. It says which query shape
  was dry-run, and so whether that model's figure is a rebuild or a single run.
  `is_incremental` cannot answer this: it is true for both. Additive — every
  existing field is unchanged.

- **`dbt-costgate init`** — writes a starter `.dbt-costgate.yml`:

  ```bash
  dbt-costgate init
  ```

  Config discovery has always worked, but nothing told you to create the file,
  and the documented example had every setting filled in — copy it and you had a
  configured project rather than a starting point. The written file documents
  every setting with its default, its type and an example value, and leaves all
  of them **commented out**, so it changes nothing until you uncomment one. It
  refuses to overwrite an existing config, including one under either of the
  other discovered names, and takes `--project-dir` when your dbt project is not
  the directory you are standing in.

- **A published container image**, so CI that isn't GitHub Actions can run the
  same check without a Python environment of its own:

  ```bash
  docker pull ghcr.io/drichards124/dbt-costgate:v0.9.0
  docker run --rm -v "$PWD:/workspace" ghcr.io/drichards124/dbt-costgate:v0.9.0 check
  ```

  Every release publishes both `:vX.Y.Z` and `:latest`. Pin the version tag —
  `:latest` moves under you, which is the one thing you do not want from the job
  that decides whether a pull request merges. The repository also ships the
  `Dockerfile` itself, so you can build it and push it to your own registry:

  ```bash
  docker build -t dbt-costgate .
  docker run --rm -v "$PWD:/workspace" dbt-costgate check
  ```

  The image runs as a non-root user, mounts your project at `/workspace`, and
  contains dbt-costgate only — not dbt, so compile in the image you already use
  for that and hand this one the `target/`. The
  [usage guide](https://github.com/Drichards124/dbt-costgate/blob/main/docs/usage.md#docker-and-ci-that-isnt-github-actions)
  has a GitLab pipeline that authenticates keylessly, with no service-account key
  anywhere. A published image on `ghcr.io` is wired but not switched on yet;
  build it yourself or push it to your own registry until it lands.

- **A `pre-commit` hook**, id `dbt-costgate`. If your team runs
  [pre-commit](https://pre-commit.com), adding this repository to your
  `.pre-commit-config.yaml` catches an expensive change on your own machine
  rather than in review. The config block to copy is in the
  [usage guide](https://github.com/Drichards124/dbt-costgate/blob/main/docs/usage.md#pre-commit-hook).

  It runs at the **pre-push** stage, so install it with the stage named:

  ```bash
  pre-commit install --hook-type pre-push
  ```

  A plain `pre-commit install` will not fire it. Pre-push rather than pre-commit
  because the check needs a compiled target and a BigQuery round-trip per changed
  model — too slow to sit in front of every commit. It needs the same two things
  the CLI does (`dbt compile` first, BigQuery via ADC), and every `check` flag is
  available through `args:`. Requires pre-commit 3.2.0 or newer.

### Changed

- **The incremental caveat is now one footnote instead of one line per model.**
  A change touching five incrementals printed the same sentence five times,
  which pushed the caveats that are about a *specific* model — a dynamic filter,
  a missing baseline — under a wall of repeats. Reports now tag the rows and
  explain the tag once:

  ```text
    fct_orders_daily  (full-refresh): 819.20 GiB → 2.91 TiB   +264%
    fct_events_hourly (full-refresh): 40.10 GiB → 44.02 GiB   +10%

    ⚠ full-refresh — for the rows tagged above, the figure is the full-refresh
      scan, not an incremental run.
  ```

  The per-row `full-refresh` tag is unchanged, so you can still see exactly
  which models it covers, and no other warning is collapsed. If you parse the
  terminal or markdown output, note the string `incremental — figure is the
  full-refresh scan` no longer appears in either. **The JSON payload is
  unchanged** — `models[].warnings` still carries that warning per model, since
  a machine reader has no repetition problem to solve.

- **Priced reports now disclose the free tier they do not deduct**, in the footer
  beside the rate they used:

  ```text
  Priced from the first byte scanned: BigQuery's 1 TiB/month on-demand free
  tier is per billing account, so it is disclosed here and never deducted.
  ```

  Not new behaviour — costs have always been priced from the first byte — but it
  was stated only in the docs, which is the wrong place for it: someone comparing
  a report against a bill has the report in front of them. The allowance is drawn
  down by every other query the billing account runs that month, which a dry-run
  cannot see, so deducting it would mean guessing. A figure therefore reads high
  by at most one TiB's worth, and a gate that over-reports is safer than one that
  lets a regression through.

  Not configurable, by design: a setting could only mean "assume the tier is
  still unspent", a claim about the whole billing account this tool cannot check.
  The line does not appear when you have set `pricing.usd_per_tib: 0.00` — that
  report quotes no money for it to adjust, and the tier is an on-demand allowance
  that does not apply under capacity/Editions pricing at all. Nothing about the
  gate, the breaches or the exit code changes; if you parse reports, note this
  adds a line to the terminal footer and a `<br/>` segment to the markdown one.
  The JSON payload is unchanged.

  The docs previously called the free tier "not modeled **by default**", which
  implied a setting that has never existed. That wording is gone.

- **The docs now say plainly that dbt-costgate prices compute, not storage.**
  BigQuery meters the two separately, and a dry-run reports the bytes a query
  would scan — a compute figure that carries no storage information. Nothing
  about what the tool measures has changed; it is now stated as the scope it has
  always been, under
  [what it will not do](https://github.com/Drichards124/dbt-costgate/blob/main/docs/explained.md#what-it-will-not-do),
  with the case worth knowing called out: a `view` becoming a `table`, or an
  `incremental` becoming a full `table`, moves real money on a meter nothing here
  watches.

### Fixed

- **A baseline with no compiled SQL is no longer reported as a basis mismatch.**
  When the baseline manifest had no compiled code for a model, it was still
  assigned a query shape — and if your branch happened to compile to the other
  one, the report said:

  ```text
  ⚠ mixed basis — baseline is full_refresh, current is incremental_form;
    recompile the baseline the same way
  ```

  naming a shape for SQL that was never compiled, directly beside the warning
  saying the baseline had no compiled SQL at all. Recompiling could not fix it,
  because the mismatch was not real.

  Such a model is still reported and still **not gated** — with no baseline
  bytes, the whole current scan reads as an increase, and a threshold firing on
  that is firing on a missing measurement rather than a regression. That
  outcome is now decided where the missing baseline is detected. Previously it
  fell out of the bogus mismatch, which meant it only applied when your branch
  compiled to the *other* shape: the same missing baseline gated or did not gate
  depending on something unrelated to it. If you have such a model, expect the
  spurious `mixed basis` line to disappear and the gating to stay off in cases
  where it was previously inconsistent.

  Unestimated rows also no longer carry a `full-refresh` / `incremental` tag,
  and `models[].basis` is `null` for them in JSON — there is no figure for a
  basis to describe.

- **An incremental model compiled against its existing table is no longer
  labelled `full-refresh`.** An incremental has two prices — the cost to rebuild
  the table, and the cost of one run against the table as it already stands —
  and which one a dry-run measures is decided by how the model was compiled.
  Every incremental row was tagged `full-refresh` and told "figure is the
  full-refresh scan" regardless, so a figure that was one incremental run read
  as rebuild cost. On a large fact table those differ by orders of magnitude,
  and the mislabelled one reads **low**.

  This was not a rare case: a prod-run manifest captures incrementals in their
  incremental form, which the usage guide already documents.

  Rows are now labelled from the basis actually measured — `full-refresh` or the
  new `incremental` tag — with a footnote under the table for each one present:

  ```text
    fct_orders_daily  (incremental): 92.16 GiB → 112.64 GiB   +22%   USD +0.13/run

    ⚠ incremental — for the rows tagged above, the figure is one run against the
      table as already built, so it does not gate rebuild cost.
  ```

  If you have a `max_usd_total` / `max_tib_total` ceiling on an incremental
  believing it capped rebuild cost, check the tag: on an `incremental` row it
  does not, and never did — the label was what said otherwise. The same applies
  to `run_frequency`, which should count rebuilds for a `full-refresh` row and
  runs for an `incremental` one.

  Two things change in output. The `incremental` tag is new, so anything matching
  on the literal `full-refresh` will no longer see these rows. And in JSON,
  `models[].warnings` carries a different sentence for an incremental-form model.
  `models[].is_incremental` is **unchanged** — it is true for both shapes, which
  is why it could never have answered this.

## [0.8.0] - 2026-07-25

### Added

- **Reports now warn when a threshold cannot fire.** Setting the rate to `0.00`
  is the documented way to run on capacity/slots, and it also makes every dollar
  threshold inert — each figure they compare against is `0.00`, so nothing
  exceeds anything and the gate passes while looking configured. Reports now say
  so and name the specific settings:

  ```text
  ⚠ thresholds.max_usd_total cannot fire: no per-byte price is configured, so
    every cost on this run is 0.00 and no dollar figure can exceed a limit.
  ```

  It points at the thresholds that do work without a rate — `max_pct_increase`
  and `max_tib_total` — and is **advisory only**: the gate, the breaches and the
  exit code are unchanged. Pricing at `0.00` and keeping the dollar thresholds is
  a valid way to work; it just no longer goes unstated.

- **Reports now warn when a region was priced from the default rate**, naming the
  location and which way the guess errs. The default is the lowest rate BigQuery
  charges anywhere, so a location the bundled table has no verified rate for —
  one opened after the table was last checked — is likely **under**-reported. The
  warning gives the three ways to set the rate you actually pay
  (`pricing.regions`, `pricing.usd_per_tib`, `pricing.region`); your value always
  wins over the built-in table. Advisory only, like the above.

- **`notices.silence`** — stop reporting a notice you have read and decided
  about, by id:

  ```yaml
  notices:
    silence:
      - dead-money-thresholds
  ```

  Every notice prints its id as the first thing on the line, so there is nothing
  to look up, and `dbt-costgate config` lists the valid ones. Silencing is **per
  notice — there is no blanket off-switch** — so turning one off can never hide a
  different notice you have not seen, including one added in a later release. An
  unknown id exits 2 rather than being ignored, because a typo would otherwise
  leave a warning on that you believe you turned off.

- **`--format json` gains a top-level `notices` array**, each entry
  `{"id": ..., "message": ...}`. The `id` is the stable key — the same one
  `notices.silence` accepts — so a consumer can match on it without parsing
  prose. Empty on a cleanly-configured run, and never affects `verdict`.

- **A net impact line, so a change that *lowers* cost is reported as an outcome.**
  Diff reports now end with one line naming the overall direction in words:

  ```text
  Net saving: USD 43.75/run · USD 1,312.50/month
  Net increase: USD 13.19/run · USD 395.63/month
  ```

  Reductions were always calculated correctly, but they showed only as a minus
  sign on a row and a `Gate: PASS` identical to a change that did nothing — so
  optimisation work looked like the absence of a failure. On a pull request
  touching several models, the totals also had to be added up by hand.

  The net is a **measurement, not a verdict**: it counts every estimated model,
  including ones excluded from gating (their cost is real either way), and it can
  report a net saving while the gate still fails on an individual model that
  breached its own threshold. If some models could not be estimated the line says
  so rather than presenting a partial sum as a total, and the monthly figure is
  omitted entirely unless every model contributed one. With a rate of `0` it
  reports scanned bytes. `--format json` gains a signed `net` block, where
  negative means a saving.

- **`pricing.currency` / `--currency`** — label reported amounts with an ISO 4217
  code of your choice, e.g. `EUR`. Pair it with your own rate
  (`pricing.usd_per_tib` or `pricing.regions`) when you are billed in something
  other than USD.

  **dbt-costgate labels; it never converts.** The setting means "the rate I gave
  you is denominated in this", not "convert into this". Because the built-in table
  is USD, setting a non-USD currency while any region is still priced *from that
  table* now exits 2 with an explanation, rather than printing a USD number with
  your label on it. The Action gains a matching `currency` input.

### Changed

- **Amounts now carry an ISO 4217 code instead of a `$` symbol** —
  `USD 6.25/TiB`, `+USD 43.75/run`, `fct_orders: +USD 43.75/run exceeds USD 10.00`.
  `$` is also CAD, AUD and SGD, so a bare symbol was ambiguous the moment anyone
  reported in anything but US dollars. The code appears on each amount rather than
  once in a column header, so a single quoted row is never ambiguous; the markdown
  cost columns are therefore now headed `Cost / run` and `Cost / month`.

  If you scrape terminal or markdown output, this changes what you parse.
  `--format json` is unaffected — its `usd_*` field names are a published contract
  and keep their names — and it gains `pricing.currency` and `pricing.priced`.

- **Diff reports now show percentage growth (`Δ %`) alongside the amounts.** It is
  often the quicker read for spotting a regression, and it is the threshold most
  teams set first. A model with no baseline (a new one) shows `—`, since there is
  nothing to take a ratio against.

- **A rate of `0` now drops money from reports entirely.** Setting
  `pricing.usd_per_tib: 0.00` is the documented way to run under capacity/Editions
  (slot) pricing, where there is no per-byte price and bytes scanned is a work
  signal rather than an invoice. Reports previously showed a column of `USD 0.00`,
  which read as broken output. They now omit amounts and show scanned bytes plus
  percentage growth instead, and say why in the disclosure line. No new flag or
  config key: a zero rate already says everything needed.

- **Region-aware pricing now covers 48 BigQuery locations instead of 2.** The
  built-in rate table previously held only the `US` and `EU` multi-regions, so
  every regional location fell back to the US rate of $6.25/TiB and was reported
  as `default-fallback`. Published on-demand rates vary far more than that —
  from $6.25/TiB up to $11.25/TiB in `southamerica-east1` — so if your models run
  outside the US/EU multi-regions, dbt-costgate was under-reporting their cost,
  by up to 80% in the worst case.

  Your reported figures will change accordingly, and the rate source for those
  regions now reads `region-table` rather than `default-fallback`. A few examples:

  | location | before | now |
  |---|---|---|
  | `europe-west3` (Frankfurt) | $6.25 | $8.125 |
  | `asia-northeast1` (Tokyo) | $6.25 | $7.50 |
  | `australia-southeast1` (Sydney) | $6.25 | $8.125 |
  | `southamerica-east1` (São Paulo) | $6.25 | $11.25 |

  Note that US regions are not uniform either: `us-south1` is $7.50/TiB and
  `us-west2`/`us-west3` are $8.4375/TiB, not $6.25. Rates are unchanged for the
  `US` and `EU` multi-regions.

  If you set your own rate with `pricing.usd_per_tib` or `pricing.regions`, your
  override still wins and nothing changes for you. Any location still not listed
  continues to fall back to $6.25/TiB and to say so in the report.

### Fixed

- **A currency mismatch now exits 2 with its explanation, instead of a traceback.**
  The check that refuses to print a US dollar figure under a non-USD label raised
  from a place nothing caught, so the documented behaviour never actually
  happened.

- **`max_pct_increase` no longer stops working when you set a rate of `0`.** A
  rate of `0.00` is documented for capacity/flat-rate slots, where bytes scanned
  is a work signal rather than an invoice. Setting it makes every dollar figure
  `0.00`, which correctly neutralises the three USD thresholds — but it also
  silently disabled the percentage threshold, because the percentage was computed
  from dollars. `max_tib_total` was left as the only gate that still fired, so a
  model whose scan grew tenfold could pass with exit code 0 and no warning.

  The percentage is now computed from scanned bytes. A percentage has no
  currency, so it holds under any pricing model.

  **This can newly fail a build that previously passed** — that is the point, but
  worth knowing before you upgrade if you run with `usd_per_tib: 0`. If you are
  on on-demand pricing with any non-zero rate, nothing changes: both sides of a
  comparison are priced at the same regional rate, so the rate cancels out of a
  ratio and the reported percentage is identical to before.

  `pct_delta` in `--format json` output is affected the same way — it reported
  `null` at a zero rate and now reports the real percentage.

## [0.7.1] - 2026-07-25

### Added

- **Published to PyPI.** `pip install dbt-costgate` now works, alongside
  `pipx install` and `uv tool install`. Releases are published from CI using
  PyPI trusted publishing (OIDC), so no API token exists for this project.
  Every release still attaches a wheel, an sdist, and `SHA256SUMS` to its GitHub
  Release if you prefer to pin to a checksummed artifact.

## [0.7.0] - 2026-07-25

### Changed

- **Renamed from `costgate` to `dbt-costgate`.** The name `costgate` is taken on
  PyPI by an unrelated project, so `pip install costgate` would have installed
  someone else's package. Everything follows the new name:

  | | before | after |
  |---|---|---|
  | command | `costgate check` | `dbt-costgate check` |
  | config file | `.costgate.yml` | `.dbt-costgate.yml` |
  | Action | `Drichards124/costgate@v` | `Drichards124/dbt-costgate@v` |
  | import | `costgate` | `dbt_costgate` |

  To migrate: rename your config file, update the `uses:` line in any workflow,
  and reinstall. Flags, config keys, report format, and exit codes are unchanged.
  The old repository URL redirects permanently, so existing clones keep working.

  Releases v0.1.0–v0.6.0 shipped under the old name and their changelog entries
  below are left as-is, as a record of what actually shipped.

## [0.6.0] - 2026-07-24

### Added

- **Config- and macro-only change detection.** `costgate check` now selects a model
  when a change it doesn't own reaches it, instead of silently skipping it:
  - With a baseline (`--baseline` / `--against`), when the model's **compiled SQL**
    differs even though its own `.sql` file is unchanged — what an upstream macro
    edit or a config change actually does. These models are labelled in the report
    ("compiled SQL changed but the model file didn't"), so a model never shows up
    without a reason.
  - In the local `git diff` default (no baseline), when the change touches a
    **macro** anywhere in the model's macro closure, or the **YAML file that
    patches it** (its `schema.yml`).

  A changed `dbt_project.yml` is reported on stderr rather than guessed at —
  project-wide config can't be traced to individual models from a diff.

### Fixed

- **Local change detection now works when the dbt project is in a repo
  subdirectory** (monorepos). `git diff` reports paths from the repository root
  while a dbt manifest records them relative to the project, so nothing matched and
  `costgate check` reported zero models and exited 0 — indistinguishable from
  "nothing changed". Changes in sibling directories are no longer considered
  either. Projects at the repository root are unaffected.

### Changed

- A config change that doesn't alter a model's compiled SQL (`partition_by`,
  `cluster_by`) is still not selected: it doesn't change that model's own scan
  either. `docs/usage.md` now states this precisely instead of describing all
  config changes as undetected.

## [0.5.0] - 2026-07-24

### Added

- **`--max-usd-total` / `--max-tib-total`** on `costgate check` (and
  `thresholds.max_usd_total` / `thresholds.max_tib_total` in `.costgate.yml`) —
  absolute per-run ceilings that fail the gate when a single model's *total* cost
  or scan exceeds the cap, regardless of how much it changed. They need no
  baseline, so they gate the zero-setup local run and also catch an
  already-expensive model that a before/after diff would pass because it barely
  changed. For incrementals the figure is the full-refresh scan.
- **GitHub Action** now exposes `max-usd-total`, `max-tib-total`, and
  `project-dir` inputs (the latter catching the Action up to the `--project-dir`
  flag), forwarding each to `costgate check`.
- **`renames`** config key (`.costgate.yml`) — pair a renamed model to its
  baseline (`current: baseline`, by model name or `unique_id`) so costgate diffs
  across a model rename instead of reporting the model as new. Requires a
  baseline; a bad mapping fails the run with an actionable error.
- **Named baselines** — a `baselines:` map in `.costgate.yml` (each entry a
  `manifest:` path or an `against:` git ref) plus `default_baseline`, selected with
  `--baseline-target <name>` (and the Action's `baseline-target` input). Set a
  default once and `costgate check` diffs without a baseline flag; switch
  environments (main / ple / prod) by name.

### Docs

- New "Audit / monitor-only" guide — track cost without ever blocking a deploy
  (`fail_on: never`, no thresholds, or `--format json` for a record).

## [0.4.0] - 2026-07-24

### Added

- **`--project-dir <dir>`** on `costgate check` — point costgate at the directory
  containing your `dbt_project.yml` when it can't be inferred from `--current`
  (for example a custom `target-path` or a copied manifest).

### Changed

- **`--against <ref>`** now works when your dbt project lives in a repo
  subdirectory, not just at the git repo root. costgate detects the repo root and
  compiles the project in its actual location.

## [0.3.0] - 2026-07-24

### Added

- **`costgate check --against <ref>`** — get a before/after cost diff locally in
  one command, with no manual baseline. costgate checks `<ref>` out into a
  throwaway git worktree, runs `dbt compile` there, uses it as the baseline, and
  removes the worktree when done. Compile your own branch once; `--against` handles
  the other side.
  - Mutually exclusive with `--baseline`.
  - Reuses your installed `dbt_packages/` (no `dbt deps` step); assumes the dbt
    project is at the git repo root and resolves `dbt` from your active
    environment.
  - On any failure (unknown ref, `dbt` not found, compile error) it exits `2` with
    an actionable message and always cleans up the worktree.

## [0.2.0] - 2026-07-24

### Added

- **GitHub Action** — gate pull requests with a single `uses:` step. The Action
  runs `costgate check` and posts one **sticky** comment with the cost report,
  updating that same comment on each new commit instead of stacking new ones.
  - Fails the check when the gate is breached (exit `1`). Operational errors
    (exit `2`, e.g. missing credentials) are **alert-only** by default; set
    `fail-on-operational: true` to hard-fail instead.
  - On an operational error the comment says costgate couldn't run, so a stale
    passing report never lingers over a commit that wasn't evaluated.
  - Fork pull requests degrade to no comment (never a failed check).
  - Inputs mirror the `check` flags (`baseline`, `config`, `select`, `fail-on`,
    `max-usd-per-run`, `max-pct`, `max-usd-per-month`, `region`, `usd-per-tib`,
    `project`, …); outputs `exit-code` and `status`.
  - See [docs/usage.md](docs/usage.md) for a ready-to-copy workflow.

## [0.1.0] - 2026-07-24

### Added

- `costgate check` — estimate the BigQuery cost impact of changed dbt models
  from compiled artifacts, using free BigQuery dry-runs.
  - **Local mode** (zero setup): run in a compiled dbt project to see the scan
    cost of the models you changed versus `main` (selected via `git diff`).
  - **Diff mode** (`--baseline <manifest>`): before/after cost per model, with
    threshold gating.
  - Selection: `--select`, else baseline checksum-diff, else `git diff`.
  - Region-aware pricing that discloses the region, rate, and source in every
    report; overridable via config or `--usd-per-tib`/`--region`.
  - Per-region pricing map: set `pricing.regions` in `.costgate.yml` to patch or
    extend the built-in rate table one region at a time (region keys match
    case-insensitively; `0.00` is allowed for flat-rate slots). Reports tag each
    region's source when a run spans regions of differing provenance, and the
    `json` output gains a `pricing.region_sources` map. Rate precedence, most
    specific first: `--usd-per-tib` → `pricing.regions` → `pricing.usd_per_tib` →
    built-in table → disclosed default.
  - Incremental models estimated via their full-refresh form and labeled as such.
  - Output formats: `terminal`, `markdown`, `json` (`--format`, `--output`).
  - Thresholds and behavior via `.costgate.yml` or flags; `--fail-on`.
  - Exit codes: `0` pass, `1` gate failed, `2` costgate couldn't run.
  - Credentials are never handled — Application Default Credentials only.
- `costgate config` — list every `.costgate.yml` key with its type, default, and a
  plain-English explanation; `--format json` for a machine-readable reference.
- Documentation: `docs/usage.md`.
