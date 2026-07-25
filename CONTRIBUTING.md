# Contributing to dbt-costgate

Thanks for your interest in improving `dbt-costgate`. This document covers the
Developer Certificate of Origin (which every commit must satisfy), the local
development setup, and how a change reaches a release.

## Ground rules

- **The gate never runs billable queries.** dbt-costgate's only warehouse
  interaction is BigQuery dry-run jobs (`dryRun=true`), which are free and
  execute nothing. A change that issues any other query type needs an issue
  and a design discussion first.
- **dbt-costgate never touches credentials.** Authentication is delegated
  entirely to Google's Application Default Credentials chain. Do not add
  credential flags, token parameters, or secret handling of any kind.
- **No telemetry, no phone-home.** The only network endpoint this tool talks
  to is the BigQuery API (and, if explicitly enabled, the Cloud Billing
  Catalog API for live pricing).
- **Dollar figures must be traceable.** Every reported cost states the
  region, the rate applied, and where that rate came from. Changes to the
  pricing table (`src/dbt_costgate/data/`) must cite Google's published pricing
  page and update the table's `last_verified` date.

## Developer Certificate of Origin (DCO)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a CLA. It is a lightweight statement that you have the right to submit
your contribution under the project's license.

**Every commit must be signed off.** Add the trailer automatically with the `-s`
flag:

```bash
git commit -s -m "pricing: add me-central2 on-demand rate"
```

That appends a line to your commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

The name and email must match the commit author. Pull requests with unsigned
commits are blocked by the DCO check. To fix an existing branch:

```bash
git rebase --signoff main
```

## Local development

Prerequisites: **Python ≥ 3.9**.

```bash
git clone https://github.com/Drichards124/dbt-costgate.git
cd dbt-costgate
python -m pip install -e ".[dev]"
```

Run the test suite (this is what CI runs):

```bash
python -m pytest -q
```

Lint **and** format-check before you push (CI runs both; `ruff check` alone
is not enough):

```bash
ruff check . && ruff format --check .
```

## Every source file carries an SPDX header

```python
# SPDX-License-Identifier: Apache-2.0
```

## How a change reaches a release

1. Branch from `main`, make your change, sign off your commits.
2. Open a pull request **targeting the `ple` branch** (not `main`). Fill out
   the PR template.
3. CI runs on the PR; the full **production-like-environment (PLE)** matrix
   runs once merged to `ple`, including a packaged-artifact smoke test.
4. Changes that pass PLE are promoted to `main` on the release schedule and
   cut into a signed release.

## Reporting security issues

Do **not** open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting — see [SECURITY.md](SECURITY.md).

## Code of Conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
