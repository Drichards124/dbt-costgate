<!-- SPDX-License-Identifier: Apache-2.0 -->

# QA records

Point-in-time records of manual QA sessions. **None of these describe how the
tool behaves now**, and none of them are maintained. Each is kept exactly as it
was written on the date in its title.

For current behaviour: [the usage guide](../usage.md), [explained](../explained.md),
and [the changelog](../../CHANGELOG.md).

| Record | Date | Version under test | What it was |
|---|---|---|---|
| [mvp-readiness](mvp-readiness-2026-07-26.md) | 2026-07-26 | v0.10.0 | A full manual pass over the CLI; 110 invocations of the packaged binary. Found 21 defects and deliberately fixed none, so it could be the input to a fixing session. |
| [session-2026-07-26.txt](session-2026-07-26.txt) | 2026-07-26 | v0.10.0 | The raw terminal transcript behind the review above. |
| [fixes-verified](fixes-verified-2026-07-26.md) | 2026-07-26 | — | The companion: all 21 defects re-driven and confirmed fixed. |
| [live-bigquery](live-bigquery-2026-07-27.md) | 2026-07-27 | — | The first run against real BigQuery, closing the caveat that every test faked the client. |
| [realistic-project](realistic-project-2026-07-27.md) | 2026-07-27 | — | The same, on a project bigger than four toy models. |
| [retry-path](retry-path-2026-07-27.md) | 2026-07-27 | — | The retry path, driven rather than asserted in parts. |
| [multi-region](multi-region-2026-07-28.md) | 2026-07-28 | — | Multi-region pricing end to end. |

## Why these are not updated

They are evidence, and evidence loses its value the moment it is edited to agree
with the present.

Two of them are load-bearing. `CHANGELOG.md` cites
[live-bigquery-2026-07-27.md](live-bigquery-2026-07-27.md) as the reason 1.0 was
callable at all — "the one part of this tool that talks to a warehouse had never
met a warehouse. It has now." Rewriting that file to describe current behaviour
would leave the changelog pointing at a document that no longer records the run
it cites. `mvp-readiness-2026-07-26.md` is the same: its value is that it is a
snapshot of what was wrong before anyone fixed it, including its own verdict of
6.5/10. Bringing that "up to date" would delete the only record of the starting
point.

So they carry a banner instead. The banner is the fix for someone mistaking one
for current documentation; rewriting the body would not be a fix, it would be a
different and worse problem.

## Adding one

Name it `<topic>-<YYYY-MM-DD>.md`, put the date in the H1, open with the same
archived-record banner the others carry, and add a row above. If a QA pass finds
something, the fix belongs in the code and the changelog — this directory records
that the pass happened and what it saw.
