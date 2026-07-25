# SPDX-License-Identifier: Apache-2.0
"""The one network edge: BigQuery dry-run, behind a ``DryRunner`` protocol.

dbt-costgate's only warehouse interaction. `dryRun=true` executes nothing, reads no
table data, and is never billed. Credentials are never handled here — the client
resolves them through Application Default Credentials, exactly like dbt-bigquery.
"""

from __future__ import annotations

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
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    if "notfound" in name.lower() or "not found" in low:
        if self_relation and _relation_in_message(self_relation, msg):
            return ErrorKind.DESTINATION_MISSING, msg
        return ErrorKind.UPSTREAM_MISSING, msg
    if "forbidden" in name.lower() or "permissiondenied" in name.lower() or "permission" in low:
        return ErrorKind.PERMISSION, msg
    if (
        "serviceunavailable" in name.lower()
        or "internalserver" in name.lower()
        or "toomanyrequests" in name.lower()
        or any(code in msg for code in ("429", "500", "503"))
    ):
        return ErrorKind.TRANSIENT, msg
    if "badrequest" in name.lower() or "invalid" in low or "syntax" in low:
        return ErrorKind.INVALID_SQL, msg
    return ErrorKind.OTHER, msg


def _relation_in_message(relation: str, msg: str) -> bool:
    """A dbt relation_name looks like `project`.`dataset`.`table`; BigQuery's
    404 text uses `project:dataset.table` or `dataset.table`. Compare on the bare
    table token so backticks/separators don't matter."""
    table = relation.replace("`", "").split(".")[-1]
    return bool(table) and table in msg


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
        job_config = self._bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self._client.query(sql, job_config=job_config, retry=self._retry)
            return DryRunResult(total_bytes=int(job.total_bytes_processed), location=job.location)
        except Exception as exc:
            kind, detail = categorize(exc, self_relation)
            return DryRunResult(error_kind=kind, error_detail=detail)
