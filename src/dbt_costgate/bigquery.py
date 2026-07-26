# SPDX-License-Identifier: Apache-2.0
"""The one network edge: BigQuery dry-run, behind a ``DryRunner`` protocol.

dbt-costgate's only warehouse interaction. `dryRun=true` executes nothing, reads no
table data, and is never billed. Credentials are never handled here — the client
resolves them through Application Default Credentials, exactly like dbt-bigquery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from dbt_costgate.models import ErrorKind


@dataclass
class DryRunResult:
    total_bytes: int | None = None
    location: str | None = None
    error_kind: ErrorKind | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_kind is None and self.total_bytes is not None


class DryRunner(Protocol):
    def dry_run(self, sql: str, self_relation: str | None = None) -> DryRunResult: ...


def categorize(exc: Exception, self_relation: str | None) -> tuple[ErrorKind, str]:
    """Map a BigQuery client exception to a dbt-costgate error kind.

    Matches on exception type name and message rather than importing google
    exception classes, so the pure test suite can exercise it with stand-ins.
    """
    name = type(exc).__name__.lower()
    msg = str(exc)
    low = msg.lower()

    # The exception class first, its message only as a fallback. The class is
    # what the client library actually determined; the message is prose, and
    # prose contains anything — `400 Column ssn_503 not found in table` is an
    # ordinary column error that reads as both a missing table and a transient
    # failure if you go looking for words in it.
    if "badrequest" in name:
        return ErrorKind.INVALID_SQL, msg
    if "notfound" in name:
        return _missing_kind(msg, self_relation), msg
    if "forbidden" in name or "permissiondenied" in name:
        return ErrorKind.PERMISSION, msg
    if "serviceunavailable" in name or "internalserver" in name or "toomanyrequests" in name:
        return ErrorKind.TRANSIENT, msg

    # No class worth trusting — an unwrapped RuntimeError, say. Read the message.
    if "not found" in low:
        return _missing_kind(msg, self_relation), msg
    if "permission" in low:
        return ErrorKind.PERMISSION, msg
    if _LEADING_STATUS.match(msg):
        return ErrorKind.TRANSIENT, msg
    if "invalid" in low or "syntax" in low:
        return ErrorKind.INVALID_SQL, msg
    return ErrorKind.OTHER, msg


def _missing_kind(msg: str, self_relation: str | None) -> ErrorKind:
    if self_relation and _relation_in_message(self_relation, msg):
        return ErrorKind.DESTINATION_MISSING
    return ErrorKind.UPSTREAM_MISSING


# The status code, where a status code actually goes: at the front. This used to
# be a bare substring scan for "429"/"500"/"503" anywhere in the message, so
# `400 Syntax error … at [500:3]` — a permanent error with a line number in it —
# was classified transient. That is three wrong answers from one match: the wrong
# kind, the wrong reported reason ("BigQuery was unavailable and the retries ran
# out"), and the wrong remediation. In production the retry predicate would also
# have kept retrying a permanent failure until the deadline. A table named
# `orders_500` or a column `ssn_503` did the same thing.
_LEADING_STATUS = re.compile(r"^\s*(429|500|503)\b")


def _relation_in_message(relation: str, msg: str) -> bool:
    """Whether a 404 is about the model's own table rather than something upstream.

    A dbt relation_name looks like `project`.`dataset`.`table`; BigQuery's 404
    text uses `project:dataset.table` or `dataset.table`. Both halves of
    dataset.table are compared, not the bare table token: on the token alone, a
    404 for `raw_landing.orders` read as the model's own `marts.orders`, and
    "destination missing" is the non-operational kind — so a genuinely broken run
    could still exit 0. Same-named tables in different datasets are the norm in a
    staging/marts layout, not an edge case.
    """
    parts = [p for p in relation.replace("`", "").replace(":", ".").split(".") if p]
    if not parts:
        return False
    table = parts[-1]
    dataset = parts[-2] if len(parts) > 1 else None
    normalised = msg.replace("`", "").replace(":", ".")
    if dataset is None:
        return table in normalised
    return f"{dataset}.{table}" in normalised


class BigQueryDryRunner:
    """Real dry-runner. Imports google-cloud-bigquery lazily so the module (and
    the pure test suite) load without credentials or the client installed."""

    def __init__(self, project: str | None = None, deadline_seconds: float = 60.0):
        self._project = project
        self._deadline = deadline_seconds
        self._client = None
        self._retry = None

    def _ensure_client(self):
        if self._client is not None:
            return
        from google.api_core import exceptions as gexc
        from google.api_core.retry import Retry
        from google.cloud import bigquery

        self._bigquery = bigquery
        self._client = bigquery.Client(project=self._project)
        self._retry = Retry(
            predicate=lambda e: isinstance(
                e,
                (
                    gexc.TooManyRequests,
                    gexc.ServiceUnavailable,
                    gexc.InternalServerError,
                ),
            ),
            deadline=self._deadline,
        )

    def dry_run(self, sql: str, self_relation: str | None = None) -> DryRunResult:
        try:
            self._ensure_client()
        except Exception as exc:  # ADC / import failure — operational, surfaced by caller
            return DryRunResult(error_kind=ErrorKind.OTHER, error_detail=str(exc))
        # `use_legacy_sql=False` is stated rather than inherited. The client
        # library happens to default it that way, but the BigQuery REST API's own
        # default for `useLegacySql` is true — so standard SQL was a property held
        # by a library internal instead of asserted, and dbt compiles standard SQL.
        job_config = self._bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, use_legacy_sql=False
        )
        try:
            job = self._client.query(sql, job_config=job_config, retry=self._retry)
            return DryRunResult(total_bytes=int(job.total_bytes_processed), location=job.location)
        except Exception as exc:
            kind, detail = categorize(exc, self_relation)
            return DryRunResult(error_kind=kind, error_detail=detail)
