<!--
  Pull requests target the `ple` branch, not `main`. See CONTRIBUTING.md.
-->

## What and why

<!-- What does this change do, and what problem does it solve? -->

## Related issues

<!-- e.g. Closes #12 -->

## Cost-safety invariants

If this touches warehouse interaction, pricing, or reporting, confirm:

- [ ] The only query type issued is a BigQuery **dry-run** (`dryRun=true`) — nothing billable, no table data read.
- [ ] No credential handling was added; auth still delegates entirely to Application Default Credentials.
- [ ] Reports still exclude compiled SQL by default (secrets can be templated into SQL).
- [ ] Pricing table changes cite Google's published pricing page and update `last_verified`.

## Checklist

- [ ] Targets the `ple` branch.
- [ ] All commits are signed off for the DCO (`git commit -s`).
- [ ] Every new source file has the SPDX header (`SPDX-License-Identifier: Apache-2.0`).
- [ ] Tests pass locally (`python -m pytest -q`).
- [ ] Lint passes (`ruff check . && ruff format --check .`).
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if this is user-visible.
