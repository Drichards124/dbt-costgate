# SPDX-License-Identifier: Apache-2.0
"""Data contracts shared across the pipeline.

Deliberately plain dataclasses with no behavior beyond a few derived helpers, so
every stage (select -> dry-run -> price -> report -> gate) can be unit-tested in
isolation with hand-built instances and no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

TIB = 2**40


class EstimateBasis(str, Enum):
    """How a model's bytes were measured — surfaced so a diff never silently
    compares two different query shapes (see the basis-mismatch guard)."""

    DIRECT = "direct"  # table / view — compiled SQL is the model as-is
    FULL_REFRESH = "full_refresh"  # incremental compiled fresh (no self-reference)
    INCREMENTAL_FORM = "incremental_form"  # incremental compiled against an existing table


class ErrorKind(str, Enum):
    """Why a dry-run did not yield bytes. Only *operational* kinds can make a
    whole run fail with exit 2; ``DESTINATION_MISSING`` is expected in fresh
    schemas and never does."""

    DESTINATION_MISSING = "destination_missing"  # 404 on the model's own relation
    UPSTREAM_MISSING = "upstream_missing"  # a referenced upstream is not materialized
    PERMISSION = "permission"
    INVALID_SQL = "invalid_sql"
    TRANSIENT = "transient"  # survived retries
    OTHER = "other"

    @property
    def is_operational(self) -> bool:
        """Operational failures indicate dbt-costgate (or auth/permissions) could not
        run — as opposed to an expected, model-specific condition."""
        return self in {
            ErrorKind.PERMISSION,
            ErrorKind.INVALID_SQL,
            ErrorKind.TRANSIENT,
            ErrorKind.OTHER,
        }


@dataclass
class ModelNode:
    """The subset of a dbt manifest model node that dbt-costgate reads."""

    unique_id: str
    name: str
    materialized: str
    language: str
    database: str | None
    schema: str | None
    relation_name: str | None
    checksum: str | None
    compiled_path: str | None
    compiled_code: str | None
    original_file_path: str | None = None
    patch_path: str | None = None
    depends_on_macros: list[str] = field(default_factory=list)
    location: str | None = None

    @property
    def is_incremental(self) -> bool:
        return self.materialized == "incremental"


@dataclass
class ModelEstimate:
    """One model's before/after byte measurement plus any caveats."""

    node: ModelNode
    bytes_baseline: int | None = None
    bytes_current: int | None = None
    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    is_new: bool = False
    basis_baseline: EstimateBasis | None = None
    basis_current: EstimateBasis | None = None
    warnings: list[str] = field(default_factory=list)
    gateable: bool = True

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def is_incremental(self) -> bool:
        return self.node.is_incremental

    @property
    def basis_mismatch(self) -> bool:
        return (
            self.basis_baseline is not None
            and self.basis_current is not None
            and self.basis_baseline != self.basis_current
        )


@dataclass
class CostDelta:
    """A priced estimate: the numbers a report renders and the gate evaluates."""

    name: str
    unique_id: str
    is_incremental: bool
    is_new: bool
    gateable: bool
    bytes_baseline: int | None
    bytes_current: int | None
    usd_baseline: float | None
    usd_current: float | None
    region: str
    warnings: list[str] = field(default_factory=list)
    error: str | None = None  # human-readable "not estimated" reason
    runs_per_month: int | None = None

    @property
    def bytes_delta(self) -> int | None:
        if self.bytes_current is None:
            return None
        return self.bytes_current - (self.bytes_baseline or 0)

    @property
    def usd_per_run_delta(self) -> float | None:
        if self.usd_current is None:
            return None
        return self.usd_current - (self.usd_baseline or 0.0)

    @property
    def usd_per_month_delta(self) -> float | None:
        d = self.usd_per_run_delta
        if d is None or self.runs_per_month is None:
            return None
        return d * self.runs_per_month

    @property
    def pct_delta(self) -> float | None:
        """Growth in scanned bytes, as a percentage.

        Derived from bytes rather than dollars so it holds under any pricing
        model. Both sides of a delta are priced at the same regional rate, so
        that rate cancels in a ratio — this is identical to a dollar-based
        percentage whenever the rate is non-zero. It differs in exactly one
        case: a rate of 0, which is a valid setting for capacity/flat-rate
        slots, and which used to make this return None and silently disable the
        percentage threshold.
        """
        if self.bytes_current is None or not self.bytes_baseline:
            return None
        return (self.bytes_current - self.bytes_baseline) / self.bytes_baseline * 100.0


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Verdict:
    status: Status
    breaches: list[str] = field(default_factory=list)
    exit_code: int = 0


@dataclass
class PricingDisclosure:
    """The provenance line every report carries — region, rate, and where the
    rate came from — so a dollar figure is never an unattributed assertion."""

    regions: dict[str, float]  # region -> usd_per_tib actually applied
    source: str  # e.g. "built-in table v2026.07" / "user override" / "default fallback"
    table_version: str
    last_verified: str
    region_sources: dict[str, str] = field(default_factory=dict)  # region -> rate source


@dataclass
class Report:
    deltas: list[CostDelta]
    disclosure: PricingDisclosure
    verdict: Verdict
    mode: str  # "diff" | "absolute"
