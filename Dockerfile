# SPDX-License-Identifier: Apache-2.0
#
# A container for teams whose CI is not GitHub Actions. Everything it runs is the
# published console script — same code, same exit codes, same reports.
#
#   docker build -t dbt-costgate .
#   docker run --rm -v "$PWD:/workspace" dbt-costgate check
#
# It needs compiled dbt artifacts and BigQuery credentials from the host; see
# docs/usage.md. dbt itself is deliberately not in here — this image estimates
# cost, it does not build your project.

# Build the wheel in a stage that is thrown away, so no build tooling and no
# source tree reaches the published image.
FROM python:3.14-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY . .
RUN python -m build --wheel --outdir /dist

FROM python:3.14-slim

LABEL org.opencontainers.image.title="dbt-costgate" \
      org.opencontainers.image.description="BigQuery cost gate for dbt pull requests." \
      org.opencontainers.image.source="https://github.com/Drichards124/dbt-costgate" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Nothing in here needs root, and this image is pointed at a mounted copy of
# somebody's repository. If the mounted project is owned by a different uid on
# the host, run with `--user "$(id -u):$(id -g)"` — the CLI only reads.
RUN useradd --create-home --uid 1000 costgate
USER costgate

# Mount the dbt project here.
WORKDIR /workspace

ENTRYPOINT ["dbt-costgate"]
CMD ["--help"]
