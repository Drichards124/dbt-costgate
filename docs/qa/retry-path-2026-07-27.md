<!-- SPDX-License-Identifier: Apache-2.0 -->

# The retry path — 2026-07-27

> **Archived QA record — not current documentation.** Kept exactly as written
> on the date in the title. It records what was true then, is cited from the
> changelog as evidence for that, and is deliberately not updated as the tool
> changes. For how the tool behaves now see [the usage guide](../usage.md) and
> [the changelog](../../CHANGELOG.md); for what these files are, see
> [README.md](README.md).

[The live BigQuery run](live-bigquery-2026-07-27.md) checked the retry by
asserting its parts: the predicate retries real `TooManyRequests`,
`ServiceUnavailable` and `InternalServerError` instances and refuses the
permanent ones, and `_retry._deadline` is the documented 60 seconds. Both passed,
and that document was careful to call it an argument rather than an observation.

The argument was wrong. Driving the machinery instead of inspecting it found two
defects, both shipped in 1.0.0, and both invisible to every test that existed.

## How it was driven

BigQuery cannot be made to rate-limit on demand, so the failure was injected one
layer lower: a synthetic HTTP response returned by the client's own
`requests.Session`. google-cloud-bigquery then builds the exception itself
through its normal `from_http_response` path, so everything above the socket —
the exception class, the retry loop, `categorize`, `DryRunResult` — is the real
thing.

That level matters. Patching `dry_run` or the client would only have tested the
stub. Injecting at the wire is what made the two defects visible.

*(An early version of the fixture omitted `errors[].reason` from the error body,
which made the client raise `KeyError` before dbt-costgate saw anything. That
looked exactly like a tool defect and was not one — a real BigQuery error body
always carries `reason`. Recorded because the next person to write one of these
will hit it.)*

## Defect 1 — `deadline_seconds` bounded nothing

`BigQueryDryRunner(deadline_seconds=5.0)` against an unending 503:

```
wall clock    : 179.0s   (deadline_seconds was 5.0)
wire attempts : 42
error_kind    : ErrorKind.OTHER
```

`Client.query` takes **two independent retries**, and dbt-costgate was setting
one:

| | deadline |
|---|--:|
| `retry` — governs the API call (what we passed) | 60 s |
| `job_retry` — re-drives the whole job (left at its default) | **2400 s** |

So the real worst case per model was forty minutes, not sixty seconds. During a
BigQuery incident a CI job would hang for hours rather than failing fast, and
with a thread pool that multiplies across every model in the change.

Measured, not reasoned:

```
deadline is 5.0s in every row:
  retry only (what 1.0.0 ships)        70.0s  attempts= 27  -> STILL GOING at 70s
  retry + job_retry=None                3.9s  attempts=  4  -> RetryError
  retry + job_retry=None + timeout      4.2s  attempts=  4  -> RetryError
```

`job_retry=None` rather than a second `Retry` with a matching deadline: a dry-run
creates no job to re-drive — it returns statistics inline — so job-level retry
had nothing to retry and was only ever multiplying the API-level one. `timeout`
bought nothing and was left out.

## Defect 2 — `ErrorKind.TRANSIENT` was unreachable in production

A retry that gives up does not re-raise what failed. google-api-core wraps it:

```
RetryError("Deadline of 60.0s exceeded while calling target function,
            last exception: 503 POST https://bigquery.googleapis.com/...")
```

Every branch in `categorize` missed. The class is `RetryError`, which matches
none of the name checks. The status code sits mid-sentence, and `_LEADING_STATUS`
deliberately anchors at the front — a decision that is right, and that was made
to fix a *different* misclassification. So an exhausted transient failure fell
through to `OTHER`:

| | |
|---|---|
| what the user saw | *"the dry-run failed"* |
| what it should say | *"BigQuery was unavailable and the retries ran out"* |

**`TRANSIENT` had been dead code in production since it was written** — its
message, and its `is_operational` status, unreachable through the real client.

The tests missed it for a specific and instructive reason: they hand `categorize`
a bare `ServiceUnavailable`. That is what fails *inside* the retry, and never
what escapes it. A stand-in for `RetryError` was the one nobody wrote, because
nobody knew it was the class the client actually produces.

This is the same shape as the defect the live BigQuery run existed to find: a
test passing because it constructs the input the code expects rather than the
input production delivers.

## The fixes

`categorize` unwraps a `RetryError` first and classifies by what it wrapped —
recursing rather than hardcoding `TRANSIENT`, so it stays correct if the
predicate ever widens. Matching stays name-based, so the pure suite keeps working
with stand-ins.

`dry_run` passes `job_retry=None`.

## Verified after the fix

Through the real client, at the wire, in
[scripts/verify_live.py](../../scripts/verify_live.py) so it stays checked:

```
== 4b. the retry machinery, actually running ==
  [PASS] a real 503 is retried and the dry-run recovers
         1 injected 503 then real BigQuery: 2 attempts, ok=True, bytes=22,209,808
  [PASS] a permanent 400 is not retried at all
         a permanent 400: 1 attempt(s) -> ErrorKind.INVALID_SQL
  [PASS] an unending 503 gives up inside the deadline, as TRANSIENT
         unending 503 on a 5s deadline: gave up after 2.1s / 3 attempts -> ErrorKind.TRANSIENT
```

179 seconds became 2.1. `OTHER` became `TRANSIENT`. Three 429s and a 500 also
retry and recover; a 403 is not retried and classifies as `PERMISSION`.

The unit tests gained a `RetryError` stand-in and a test that `job_retry=None`
reaches the call — asserted on the call rather than on `_retry._deadline`,
because asserting the attribute is exactly what passed while the behaviour was
unbounded.

## Still not proved

That BigQuery's own 429s and 503s look like the injected ones. The bodies are
shaped from real BigQuery error envelopes, but nobody has watched this recover
from an actual incident. That is a much smaller claim than the one this document
replaces.
