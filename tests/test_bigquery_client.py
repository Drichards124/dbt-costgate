# SPDX-License-Identifier: Apache-2.0
"""`BigQueryDryRunner` itself — the class the shipped binary always uses.

Every other test injects a `FakeDryRunner` through `main(..., runner=...)`, a
seam the console script never uses, so the real dry-runner's body had no
coverage: not the request it builds, not its two `except` clauses, not the
retry object it hands the client.

The client is stubbed by assigning `_client` / `_bigquery` directly, which is
what `_ensure_client` would have set. Nothing here imports `google`, so the
suite still loads without the client installed — the property `bigquery.py`
exists to preserve.
"""

import pytest

from dbt_costgate.bigquery import BigQueryDryRunner, categorize
from dbt_costgate.models import ErrorKind

SELF_RELATION = "`proj`.`marts`.`orders`"


class _Job:
    def __init__(self, total_bytes, location):
        self.total_bytes_processed = total_bytes
        self.location = location


class _JobConfig:
    """Stands in for `bigquery.QueryJobConfig` and remembers its kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Bigquery:
    QueryJobConfig = _JobConfig


class _Client:
    def __init__(self, job=None, raises=None):
        self._job = job
        self._raises = raises
        self.calls = []

    def query(self, sql, job_config=None, retry=None, **kwargs):
        # kwargs recorded too: `job_retry` lives there, and it going unrecorded is
        # part of why a 2400-second default outranked our deadline unnoticed.
        self.calls.append({"sql": sql, "job_config": job_config, "retry": retry, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._job


def _runner(client):
    runner = BigQueryDryRunner(project="proj")
    runner._client = client
    runner._bigquery = _Bigquery
    runner._retry = object()
    return runner


# Named to match the google.api_core classes `categorize` matches on by type
# name, so the stand-ins classify exactly as the real ones do.
class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


class ServiceUnavailable(Exception):
    pass


class RetryError(Exception):
    """What google-api-core raises when a Retry gives up — the shape the client
    actually produces, as opposed to the bare exceptions above.

    Every stand-in here is a class the *client* raises. This one was missing, and
    its absence is why a transient failure that survived the retries classified
    as OTHER for as long as the retry has existed: the tests fed `categorize` a
    bare `ServiceUnavailable`, which is what fails *inside* the retry, never what
    escapes it.
    """

    def __init__(self, message, cause):
        super().__init__(message)
        self.cause = cause


def _gave_up(last):
    return RetryError(
        f"Deadline of 60.0s exceeded while calling target function, last exception: {last}", last
    )


def test_a_transient_failure_that_outlives_the_retries_is_transient():
    result = _runner(_Client(raises=_gave_up(ServiceUnavailable("503 backend error")))).dry_run(
        "select 1", SELF_RELATION
    )
    assert result.error_kind is ErrorKind.TRANSIENT


def test_a_retry_error_is_classified_by_what_it_wrapped():
    # Not hardcoded to TRANSIENT: the wrapped exception is the real answer, and
    # saying so keeps this correct if the predicate ever widens.
    for last, expected in (
        (ServiceUnavailable("503 backend error"), ErrorKind.TRANSIENT),
        (NotFound("404 Not found: Table proj:raw.upstream"), ErrorKind.UPSTREAM_MISSING),
        (Forbidden("403 Access Denied"), ErrorKind.PERMISSION),
    ):
        assert categorize(_gave_up(last), SELF_RELATION)[0] is expected


def test_a_retry_error_with_nothing_wrapped_is_still_transient():
    err = RetryError("Deadline of 60.0s exceeded while calling target function", None)
    assert categorize(err, None)[0] is ErrorKind.TRANSIENT


def test_the_dry_run_disables_job_level_retry():
    """`Client.query` carries two retries, and only one of them is ours.

    `job_retry` defaults to a 2400-second deadline and re-drives the whole job,
    which silently outranked `deadline_seconds`: measured against real BigQuery,
    a 5-second deadline ran for 179 seconds over 42 attempts. A dry-run creates
    no job to re-drive, so the answer is to switch it off rather than to give it
    a matching deadline.

    Asserted on the call rather than on `_retry._deadline`, because asserting the
    attribute is exactly what passed while the behaviour was unbounded.
    """
    client = _Client(job=_Job(4096, "US"))
    _runner(client).dry_run("select 1")
    assert "job_retry" in client.calls[0], "job_retry was not passed at all"
    assert client.calls[0]["job_retry"] is None


def test_a_successful_dry_run_returns_bytes_and_location():
    client = _Client(job=_Job(4096, "europe-west3"))
    result = _runner(client).dry_run("select 1", SELF_RELATION)
    assert result.ok
    assert result.total_bytes == 4096
    assert result.location == "europe-west3"


def test_the_request_is_always_a_dry_run_with_the_cache_off():
    client = _Client(job=_Job(1, "US"))
    _runner(client).dry_run("select 1")
    config = client.calls[0]["job_config"]
    assert config.kwargs["dry_run"] is True
    assert config.kwargs["use_query_cache"] is False


def test_the_retry_object_is_handed_to_the_client():
    client = _Client(job=_Job(1, "US"))
    runner = _runner(client)
    runner.dry_run("select 1")
    assert client.calls[0]["retry"] is runner._retry


def test_a_client_that_cannot_be_built_is_an_operational_failure():
    class Broken(BigQueryDryRunner):
        def _ensure_client(self):
            raise RuntimeError("could not find default credentials")

    result = Broken().dry_run("select 1")
    assert result.ok is False
    assert result.error_kind is ErrorKind.OTHER
    assert "default credentials" in result.error_detail


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (Forbidden("403 Access Denied"), ErrorKind.PERMISSION),
        (NotFound("404 Not found: Table proj:raw.upstream"), ErrorKind.UPSTREAM_MISSING),
        (NotFound("404 Not found: Table proj:marts.orders"), ErrorKind.DESTINATION_MISSING),
        (ServiceUnavailable("503 backend error"), ErrorKind.TRANSIENT),
        (BadRequest("400 Syntax error: Unexpected keyword WHERE"), ErrorKind.INVALID_SQL),
    ],
)
def test_client_exceptions_are_classified(exc, expected):
    result = _runner(_Client(raises=exc)).dry_run("select 1", SELF_RELATION)
    assert result.error_kind is expected


def test_a_failed_dry_run_reports_no_bytes_and_no_location():
    result = _runner(_Client(raises=Forbidden("403"))).dry_run("select 1", SELF_RELATION)
    assert result.total_bytes is None
    assert result.location is None
    assert result.ok is False


# --------------------------------------------------------------------------
# Confirmed defects.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "400 Syntax error: Expected end of input but got keyword WHERE at [500:3]",
        "400 Invalid value: table name orders_429 is not valid",
        "400 Column ssn_503 not found in table",
    ],
)
def test_a_status_code_inside_the_message_is_not_a_transient_failure(message):
    assert categorize(BadRequest(message), SELF_RELATION)[0] is ErrorKind.INVALID_SQL


def test_a_404_on_a_same_named_upstream_is_not_the_models_own_destination():
    exc = NotFound("404 Not found: Table proj:raw_landing.orders was not found in location US")
    assert categorize(exc, SELF_RELATION)[0] is ErrorKind.UPSTREAM_MISSING


def test_a_404_on_the_models_own_table_still_reads_as_its_destination():
    exc = NotFound("404 Not found: Table proj:marts.orders was not found in location US")
    assert categorize(exc, SELF_RELATION)[0] is ErrorKind.DESTINATION_MISSING


def test_the_exception_class_outranks_words_in_its_message():
    """`400 Column ssn_503 not found in table` is an ordinary column error that
    reads as a missing table *and* a transient failure if you go looking for
    words. The class the client library chose is the thing to trust."""
    assert (
        categorize(BadRequest("400 Column ssn_503 not found in table"), SELF_RELATION)[0]
        is ErrorKind.INVALID_SQL
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("503 Backend Error", ErrorKind.TRANSIENT),
        ("404 Not found: Table proj:raw.upstream", ErrorKind.UPSTREAM_MISSING),
        ("403 Access Denied: no permission", ErrorKind.PERMISSION),
        ("Syntax error near WHERE", ErrorKind.INVALID_SQL),
        ("something else entirely", ErrorKind.OTHER),
    ],
)
def test_an_exception_with_no_useful_class_falls_back_to_its_message(message, expected):
    assert categorize(RuntimeError(message), SELF_RELATION)[0] is expected


def test_standard_sql_is_requested_explicitly():
    client = _Client(job=_Job(1, "US"))
    _runner(client).dry_run("select 1")
    assert client.calls[0]["job_config"].kwargs["use_legacy_sql"] is False
