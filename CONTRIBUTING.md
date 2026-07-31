# Contributing to dbt-costgate

Thanks for your interest in improving `dbt-costgate`. This document covers the
ground rules, the local development setup, and how a change reaches a release.

## Ground rules

- **The gate never runs billable queries.** dbt-costgate's only warehouse
  interaction is BigQuery dry-run jobs (`dryRun=true`), which are free and
  execute nothing. A change that issues any other query type needs an issue
  and a design discussion first.
- **dbt-costgate never touches credentials.** Authentication is delegated
  entirely to Google's Application Default Credentials chain. Do not add
  credential flags, token parameters, or secret handling of any kind.
- **No telemetry, no phone-home.** The only network endpoint this tool talks
  to is the BigQuery API.
- **Dollar figures must be traceable.** Every reported cost states the
  region, the rate applied, and where that rate came from. Changes to the
  pricing table (`src/dbt_costgate/data/`) must cite Google's published pricing
  page and update the table's `last_verified` date.

## Licensing your contribution

There is no CLA and nothing to sign. Opening a pull request against this project
licenses your contribution under Apache-2.0, per section 5 of the
[LICENSE](LICENSE) — that is the default the license already sets, and this
project does not add anything on top of it.

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

If you touch the `Dockerfile`, build and run it — the test suite cannot. CI does
the same on every push to `ple`, and additionally checks that the image is not
running as root:

```bash
docker build -t dbt-costgate . && docker run --rm dbt-costgate --version
```

## If you change what a report looks like

Every example report in `README.md` and `docs/usage.md` is generated from the real
renderers, and the test suite fails if one drifts. Regenerate them:

```bash
python scripts/gen_samples.py
```

Add or edit an example in `scripts/samples.py`, then place
`<!-- BEGIN GENERATED: <name> -->` / `<!-- END GENERATED: <name> -->` where it
should appear. A sample that no example references is reported as unused, so the
file cannot accumulate examples nobody reads.

Please don't hand-edit the text inside those markers. An example that says
something the code does not is worse than no example: this repo shipped one that
priced 412.50 MiB at $2.51 — about a thousand times too high — and it survived
several releases precisely because nothing compared it to real output.

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
5. Tagging builds the artifacts, publishes to PyPI, and creates the GitHub
   Release automatically. **Publishing the Action to the Marketplace is manual
   and per-release** — the listing stays on whichever release last had the box
   ticked. The release run's summary links straight to the right page, and the
   `Marketplace drift` workflow fails weekly until the listing matches the
   latest release.

## Reporting security issues

Do **not** open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting — see [SECURITY.md](SECURITY.md).

## Code of Conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
