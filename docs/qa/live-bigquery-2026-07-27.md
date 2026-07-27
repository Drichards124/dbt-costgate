<!-- SPDX-License-Identifier: Apache-2.0 -->

# Live BigQuery verification — 2026-07-27

Every test in the suite fakes `google.cloud.bigquery.Client`. That proves the
logic and proves nothing about the thing being faked. This is the run against
real BigQuery that closes the caveat left at the end of
[fixes-verified-2026-07-26.md](fixes-verified-2026-07-26.md):

> Whether BigQuery's actual responses match the fake's shape, and whether the
> retry predicate fires on genuine 429/503s, needs one run against a real project
> before calling this 1.0.

The first half is now answered. The second half is answered as far as it can be,
and the remainder is written down below rather than papered over.

Reproduce it with:

```bash
python scripts/verify_live.py --project <your-project>
```

**Setup.** Application Default Credentials against a personal GCP project with
no datasets of its own — every query reads `bigquery-public-data.usa_names`. All
25-odd queries were `dry_run=True`: nothing executed, no bytes billed. The
project is deliberately not named here: it is an identifier of someone's
infrastructure, it tells a reader nothing they need, and this file is public.

## What it found

One defect, and it is the kind only a real warehouse produces.

**BigQuery answers 403 — not 404 — for a dataset or project that does not
exist.** It does this deliberately, so nobody can map an account by watching 404s
turn into 403s. Its own error text hedges accordingly: *"User does not have
permission to query table X, or perhaps it does not exist."*

dbt-costgate dropped the hedge. A single mistyped dataset name produced:

```
dbt-costgate: could not estimate any model (check credentials/permissions).
Try `gcloud auth application-default login`.
exit code: 2
```

Wrong advice, at the loudest exit code the tool has. `PERMISSION.is_operational`
is `True`, so one typo in one model failed the whole run and told the reader to
go and fix their credentials. In CI that is a wild goose chase through IAM for
what is a spelling mistake.

The *classification* was right and is unchanged — 403 genuinely is ambiguous and
BigQuery will not disambiguate it. What was wrong was the two sentences built on
top of it, both now naming the other cause:
[models.py](../../src/dbt_costgate/models.py) and
[cli.py](../../src/dbt_costgate/cli.py). Two tests pin it.

## What passed

**The response parses.** A real dry-run of
`SELECT name, number, state FROM usa_names.usa_1910_2013 WHERE state = 'TX'`
returned 110,355,534 bytes at location `US`, straight into `DryRunResult`.

**The byte count is measured, not decorative.** One column reads 22,209,808
bytes; every column reads 171,432,506. A constant or a parse artefact would have
given the same number twice.

**`categorize()` maps real `google.api_core.exceptions` classes.** Every row here
is a live exception object, not a stand-in:

| What was asked of BigQuery | Class it raised | Kind |
|---|---|---|
| a missing table in a readable dataset | `NotFound` (404) | `UPSTREAM_MISSING` |
| the same 404, relation matching the model's own | `NotFound` (404) | `DESTINATION_MISSING` |
| the same 404, same table name in another dataset | `NotFound` (404) | `UPSTREAM_MISSING` — not the model's own |
| `SELECT FROM FROM t` | `BadRequest` (400) | `INVALID_SQL` |
| `SELECT ssn_503 FROM t` | `BadRequest` (400) | `INVALID_SQL` — not a missing table |
| a syntax error padded to column 503 | `BadRequest` (400) | `INVALID_SQL` — **not** `TRANSIENT` |
| a dataset that does not exist | `Forbidden` (403) | `PERMISSION` |

The sixth row is the one worth the trip. A real message reading
`Syntax error: Unexpected keyword FROM at [1:503]` is a permanent failure
carrying a transient-looking number. The old substring scan for `503` anywhere in
the text classified it transient, which meant the wrong kind, the wrong stated
reason, and a retry loop grinding against a permanent error until the deadline.
Real BigQuery produces exactly that message, and the fix holds against it.

**Standard SQL is asserted, not inherited.** A CTE — valid standard SQL, invalid
legacy SQL — dry-runs fine. The identical query with `use_legacy_sql=True` raises
`BadRequest`. So the flag is load-bearing rather than a comment: the REST API's
own default for `useLegacySql` is true, and dbt compiles standard SQL.

**A missing credential is reported, not raised.** With
`GOOGLE_APPLICATION_CREDENTIALS` pointed at a path that does not exist, the
runner returns `ErrorKind.OTHER` and no traceback reaches the user.

**Location round-trips a non-US value.** `job.location` came back `'EU'` for an
EU-pinned job, so the field is read from the job rather than assumed to be `US`.

## End to end

The packaged CLI, hand-built manifests, real dry-runs, a real verdict. The
baseline scans one column and the head scans four, so the regression is measured
by BigQuery, not asserted by a fixture:

```
dbt-costgate — region: US · on-demand USD 6.25/TiB · built-in table · first 1 TiB/month free

  MODEL       BASELINE     CURRENT    Δ %    Δ / RUN  Δ / MONTH  RUNS
  ─────────  ─────────  ──────────  ─────  ─────────  ─────────  ────
  fct_names  21.18 MiB  147.61 MiB  +597%  USD +0.00  USD +0.02    30

  Net increase: USD 0.00/run · USD 0.02/month
  Monthly scan for these models: 4.32 GiB — inside the 1 TiB/month you declared free, if nothing
    else has drawn on it

  GATE: FAIL
    - fct_names: +597% exceeds 25%
```

- The reported bytes — 22,209,808 and 154,775,150 — are **byte-identical** to
  what a direct dry-run of the same two queries returns. Nothing in the CLI's
  plumbing dropped, cached or reused a value.
- The region was auto-detected as `US` from the live job and priced from the
  built-in table at USD 6.25/TiB.
- `monthly_scan_bytes` came out to exactly `bytes_current × 30`, and the
  declared-free-tier line rendered from real numbers.
- Exit code 1, `GATE: FAIL`.

**18 of 18 checks passed** after the 403 wording fix.

## What this run could not prove

Stated plainly, because a verification document that implies more than it
established is worse than no document.

**A real 429 or 503, retried and recovered.** You cannot make BigQuery
rate-limit you on demand. What was checked instead is the retry predicate against
real `TooManyRequests`, `ServiceUnavailable` and `InternalServerError`
*instances* — it retries all three and refuses `BadRequest`, `NotFound` and
`Forbidden` — plus the 60-second deadline. That is a strong argument and not an
observation, and the difference matters: nobody has watched this code recover
from a transient failure in flight.

**EU pricing driven by an EU-resident table.** `job.location` round-trips `'EU'`,
but with no EU dataset available the rate lookup was not driven end to end by a
genuinely EU-hosted query. The lookup itself is covered by unit tests.

**A genuine permission denial.** Every 403 obtainable here came from a name that
does not exist rather than from a table that exists and is closed to us. Same
exception class either way, so the classification is proven; the distinction is
exactly the one the tool can no longer claim to make, which is the point of the
fix above.
