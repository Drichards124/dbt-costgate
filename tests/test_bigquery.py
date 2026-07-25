# SPDX-License-Identifier: Apache-2.0
"""Categorization is pure and testable with stand-in exceptions that mimic the
google client's type names and messages — no real client required."""

from dbt_costgate.bigquery import DryRunResult, categorize
from dbt_costgate.models import ErrorKind


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


class ServiceUnavailable(Exception):
    pass


def test_destination_missing_when_own_relation_in_message():
    exc = NotFound("Not found: Table proj:analytics.fct_orders was not found")
    kind, _ = categorize(exc, "`proj`.`analytics`.`fct_orders`")
    assert kind == ErrorKind.DESTINATION_MISSING


def test_upstream_missing_when_other_table_in_message():
    exc = NotFound("Not found: Table proj:analytics.some_upstream was not found")
    kind, _ = categorize(exc, "`proj`.`analytics`.`fct_orders`")
    assert kind == ErrorKind.UPSTREAM_MISSING


def test_permission_and_invalid_and_transient():
    assert (
        categorize(Forbidden("Access Denied: permission bigquery.jobs.create"), None)[0]
        == ErrorKind.PERMISSION
    )
    assert (
        categorize(BadRequest("Syntax error: unexpected token"), None)[0] == ErrorKind.INVALID_SQL
    )
    assert categorize(ServiceUnavailable("503 backend error"), None)[0] == ErrorKind.TRANSIENT


def test_dryrunresult_ok():
    assert DryRunResult(total_bytes=10).ok
    assert not DryRunResult(error_kind=ErrorKind.OTHER).ok
