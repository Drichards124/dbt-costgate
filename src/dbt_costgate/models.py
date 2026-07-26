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


def format_money(value: float | None, currency: str, *, signed: bool = False) -> str:
    """Render an amount with its ISO 4217 code: `USD 6.25`, `USD +43.75`, `USD -43.75`.

    Lives here, beside `TIB`, because both the report and the threshold breach
    messages render money and the two must agree. A second copy of the format
    string in `policy.py` is exactly how they drifted apart before — the sign
    moved onto the number in one place and not the other.
    """
    if value is None:
        return "—"
    # The sign belongs to the number, not to the currency code.
    return f"{currency} {value:+,.2f}" if signed else f"{currency} {value:,.2f}"


@dataclass(frozen=True)
class Notice:
    """One run-level advisory, and the id that identifies it in config.

    The id is stable and user-facing: it is what goes in `notices.silence`, and
    reports print it beside the message so the way to turn a notice off is
    visible from the notice itself.
    """

    id: str
    message: str


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


# The one warning that says the same thing about every model it lands on: it
# explains what the `full-refresh` tag on a row means, so a run touching five
# incremental models printed it five times and buried the warnings that were
# genuinely about one model. `artifacts.sql_warnings` produces it and `report`
# collapses it into a single footnote, which means both have to agree on the
# exact string. It lives here, in the vocabulary both already import, rather
# than as a literal matched in the renderer: a second copy could drift from this
# one in silence — the renderer would simply stop recognising it and go back to
# repeating it per row, with nothing failing to say so.
INCREMENTAL_BASIS_WARNING = "incremental — figure is the full-refresh scan"


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

    regions: dict[str, float]  # region -> rate per TiB actually applied
    source: str  # e.g. "built-in table v2026.07" / "user override" / "default fallback"
    table_version: str
    last_verified: str
    region_sources: dict[str, str] = field(default_factory=dict)  # region -> rate source
    currency: str = "USD"  # ISO 4217 code the applied rates are denominated in

    @property
    def priced(self) -> bool:
        """Whether money figures carry any information.

        Every applied rate being 0 is a valid, documented configuration for
        capacity/flat-rate slots, where there is no per-byte price to report. In
        that state a currency column is a column of zeroes, so reports drop money
        entirely and show scanned bytes instead.
        """
        return any(rate != 0 for rate in self.regions.values())


@dataclass
class Report:
    deltas: list[CostDelta]
    disclosure: PricingDisclosure
    verdict: Verdict
    mode: str  # "diff" | "absolute"
    # Run-level notes about the *configuration* rather than about a model — a
    # setting that cannot do what it looks like it does. Advisory by
    # construction: they are never consulted by `policy.evaluate`, so a notice
    # can never change a verdict or an exit code. Per-model caveats belong on
    # `CostDelta.warnings` instead.
    notices: list[Notice] = field(default_factory=list)

    # --- net impact -------------------------------------------------------
    # The per-model rows say what each model did; nothing said what the change
    # did overall, so a reviewer had to add a column up by hand — and a change
    # that *lowers* cost read as merely "not a failure". These are a
    # measurement, not a verdict: models the config excluded from gating still
    # spend money, so they count here even though they cannot fail the gate.

    @property
    def estimated(self) -> list[CostDelta]:
        """Models with a usable before/after. Bytes, not dollars, decide this —
        an unpriced run has real byte deltas and zero-valued money ones."""
        return [d for d in self.deltas if d.bytes_delta is not None]

    @property
    def net_bytes(self) -> int | None:
        rows = self.estimated
        return sum(d.bytes_delta or 0 for d in rows) if rows else None

    @property
    def net_usd_per_run(self) -> float | None:
        rows = self.estimated
        return sum(d.usd_per_run_delta or 0.0 for d in rows) if rows else None

    @property
    def net_usd_per_month(self) -> float | None:
        """None unless *every* estimated model contributed a monthly figure — a
        partial sum across models with and without a run frequency would read as
        a total while quietly omitting some of them."""
        values = [d.usd_per_month_delta for d in self.estimated]
        if not values or any(v is None for v in values):
            return None
        return sum(values)

    @property
    def unestimated_count(self) -> int:
        return len(self.deltas) - len(self.estimated)
