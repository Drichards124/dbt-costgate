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


def format_pct(value: float | None, *, signed: bool = True) -> str:
    """Render a percentage at whatever precision it actually needs: `+264%`,
    `+0.4%`, `0.35%`.

    Here beside `format_money`, and for the same reason: the report renders a
    model's growth and `policy.py` renders the limit it exceeded, and the two
    have to agree. Fixed at zero decimal places in both places, they produced the
    breach line `+0% exceeds 0%` for a real 0.4% increase over a 0.3% limit —
    a correct gate failure that read like a bug.

    Precision follows the magnitude, because decimals earn their place at one end
    of the range and are noise at the other: `+264%` says everything `+263.75%`
    does, while `+0%` throws away the whole number. Under 1 gets two places, under
    10 gets one, and everything above is whole.

    Trailing zeros are trimmed by splitting on the decimal point rather than by
    `rstrip`, which would eat the zeros out of `1,200.00` as well.
    """
    if value is None:
        return "—"
    size = abs(value)
    places = 2 if size < 1 else 1 if size < 10 else 0
    text = f"{value:+,.{places}f}" if signed else f"{value:,.{places}f}"
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac}%" if frac else f"{whole}%"


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


class SkipReason(str, Enum):
    """Why a model was not gated — and, crucially, which kind of "not gated".

    `gateable=False` used to mean two unrelated things at once. *The user told us
    not to gate this model* is a decision the gate should respect. *We could not
    gate this model* is the gate failing at its one job, and treating the two the
    same is how a +264% regression passed with five thresholds set to nearly
    zero: the baseline had been compiled a different way, the comparison was
    dropped, and the verdict was `PASS`.

    The two groups below are what tells them apart. `is_unchecked` is the line.
    """

    # Nothing to enforce here.
    EXCLUDED = "excluded"
    WARN_ONLY = "warn_only"
    DELETED = "deleted"  # a removal can only lower cost, so no threshold applies
    # Failure: the gate wanted to check and could not.
    NO_COMPILED_SQL = "no_compiled_sql"
    DRY_RUN_FAILED = "dry_run_failed"
    NO_BASELINE_SQL = "no_baseline_sql"
    BASIS_MISMATCH = "basis_mismatch"

    @property
    def is_unchecked(self) -> bool:
        return self not in (SkipReason.EXCLUDED, SkipReason.WARN_ONLY, SkipReason.DELETED)


# What a report says about a model the gate could not check. Plain language, and
# each one ends in the thing the reader can do about it.
#
# Written without a pronoun for the model, because each of these is used in two
# frames: after one model's name ("fct_orders_daily: not checked — …") and after
# a count ("none of the 2 selected models could be gated — …"). "its dry-run"
# reads correctly in the first and disagrees with the plural in the second.
SKIP_REASON_MESSAGES: dict[SkipReason, str] = {
    SkipReason.NO_COMPILED_SQL: "there is no compiled SQL to measure — run `dbt compile`",
    SkipReason.DRY_RUN_FAILED: (
        "the dry-run did not return a size, so there is no figure to compare against a threshold"
    ),
    SkipReason.NO_BASELINE_SQL: (
        "the baseline has no compiled SQL to compare against — it must come from "
        "`dbt compile`, not `dbt parse`"
    ),
    SkipReason.BASIS_MISMATCH: (
        "the baseline and this branch were compiled differently, so their two figures answer "
        "different questions — recompile the baseline the same way"
    ),
}


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


# Why a model could not be estimated, in words a report is allowed to print.
# BigQuery's own message is deliberately absent: it quotes the query it was given,
# and compiled SQL can embed secrets templated via env_var()/vars — the same reason
# SECURITY.md keeps compiled SQL out of reports. Reports reach pull-request
# comments, so the raw message goes to stderr instead, where it stays in the job
# log. Every kind must appear here (asserted in tests): a kind with no entry would
# otherwise fall back to printing the raw message again.
ERROR_KIND_REASONS: dict[ErrorKind, str] = {
    ErrorKind.DESTINATION_MISSING: (
        "incremental target not built; compile with --defer --state in a fresh "
        "target for the full-refresh estimate"
    ),
    ErrorKind.UPSTREAM_MISSING: "an upstream table it reads has not been materialized",
    # BigQuery answers 403 both for a table it will not let you read and for a
    # dataset or project that does not exist — deliberately, so a stranger cannot
    # map an account by watching 404s turn into 403s. Its own message hedges
    # ("or perhaps it does not exist"); this one used to drop the hedge and say
    # only "denied permission", which sends someone to IAM when the real fix is a
    # typo in a dataset name. Verified against real BigQuery, 2026-07-27.
    ErrorKind.PERMISSION: (
        "BigQuery refused the dry-run — either it is not allowed, or the dataset "
        "or project does not exist"
    ),
    ErrorKind.INVALID_SQL: "BigQuery rejected the compiled SQL",
    ErrorKind.TRANSIENT: "BigQuery was unavailable and the retries ran out",
    ErrorKind.OTHER: "the dry-run failed",
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
    is_deleted: bool = False
    basis_baseline: EstimateBasis | None = None
    basis_current: EstimateBasis | None = None
    warnings: list[str] = field(default_factory=list)
    # Why this model will not be gated, if it will not. `gateable` is derived
    # from it rather than set alongside it, so the two cannot disagree about a
    # model — which is exactly how the reason for skipping got lost.
    skip_reason: SkipReason | None = None

    @property
    def gateable(self) -> bool:
        return self.skip_reason is None

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


@dataclass(frozen=True)
class BasisLabel:
    """Everything a reader is told about one estimate basis, in one place.

    A basis surfaces in three forms that must agree: a tag on the row, a warning
    on the model, and the footnote a report prints once instead of repeating the
    warning. Split across the modules that render them, they drifted — the tag
    and the warning were each derived from `is_incremental` alone, so a model
    compiled in incremental form was tagged `full-refresh` and told its figure
    was a rebuild, which was simply untrue.

    One entry per basis, so a new one cannot be added while quietly leaving a
    renderer to guess. `EstimateBasis.DIRECT` is deliberately absent: a table or
    a view compiles one way, so there is no basis to disambiguate and nothing to
    say. Absence from this mapping is what "needs no label" means.
    """

    tag: str  # the per-row marker
    warning: str  # the per-model warning `artifacts.sql_warnings` emits
    footnote: str  # the collapsed explanation a report prints once


# Written to be read once. The earlier footnotes said "for the rows tagged above,
# the figure is one run against the table as already built, so it does not gate
# rebuild cost" — every fact correct, and it takes a second pass to work out that
# the number on screen is the cheap case and the expensive one is not here at all.
# Each footnote below now says what the number is, what it is not, and which of
# the two is bigger, in that order.
BASIS_LABELS: dict[EstimateBasis, BasisLabel] = {
    EstimateBasis.FULL_REFRESH: BasisLabel(
        tag="full-refresh",
        warning="full-refresh — this figure is a full rebuild, not one incremental run",
        footnote=(
            "full-refresh — rows tagged full-refresh show what it costs to build the whole "
            "table from scratch. A normal incremental run scans much less, so read this as "
            "the ceiling rather than the nightly bill."
        ),
    ),
    EstimateBasis.INCREMENTAL_FORM: BasisLabel(
        tag="incremental",
        warning="incremental — this figure is one incremental run, not a full rebuild",
        footnote=(
            "incremental — rows tagged incremental show one run against a table that "
            "already exists. A full rebuild scans far more, and nothing here measures it, "
            "so no threshold on this report can catch a rebuild getting expensive."
        ),
    ),
}


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
    # How this model's bytes were measured. `is_incremental` says the model *is*
    # incremental; this says which shape was actually dry-run, which is what
    # decides whether the figure is a rebuild or a single run. Defaulted so a
    # hand-built delta stays constructible, and so an unknown basis leaves a row
    # unlabelled rather than mislabelled.
    basis: EstimateBasis | None = None
    # Why this model is not gated. `gateable` says *whether*; this says *which
    # kind*, and the gate needs the difference: a model the user excluded is a
    # decision, a model that could not be measured is a hole.
    skip_reason: SkipReason | None = None
    # In the baseline and gone from the branch. `bytes_current` is 0, so the
    # delta is the whole of what it used to scan — a saving.
    is_deleted: bool = False
    # Whether the two figures can be subtracted at all. Separate from
    # `skip_reason` because `exclude:` overwrites that one, and it must: a model
    # whose dry-run always fails has to be acceptable by name. But excluding a
    # model from *gating* says nothing about whether its baseline and branch were
    # compiled the same way, and letting the exclusion answer that question put
    # the incomparable figure straight back into the headline net.
    comparable: bool = True

    @property
    def unchecked(self) -> bool:
        """The gate wanted to check this model and could not."""
        return self.skip_reason is not None and self.skip_reason.is_unchecked

    @property
    def grew_from_zero(self) -> bool:
        """The baseline scanned nothing and this change scans something.

        `pct_delta` is `None` here, because there is no ratio to a zero baseline
        — which quietly took `max_pct_increase` out of play for exactly the model
        it should catch hardest. `math.inf` would say it numerically, but
        `Infinity` is not valid JSON (RFC 8259), so a report carrying it would
        fail `jq` and any strict parser. The fact travels as this flag instead,
        and `bytes_baseline: 0` beside `bytes_current: N` already says the rest.
        """
        return self.bytes_baseline == 0 and (self.bytes_current or 0) > 0

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


def estimated(deltas: list[CostDelta]) -> list[CostDelta]:
    """Models with a usable before/after. Bytes, not dollars, decide this — an
    unpriced run has real byte deltas and zero-valued money ones.

    A basis mismatch is excluded, and it is the one exclusion that is not about
    policy. `exclude` and `warn_only` say a model must not *fail the gate*; the
    model still spends money, so it belongs in the total. A basis mismatch says
    the two numbers cannot be subtracted at all — and having printed exactly that
    warning, the report went on to headline `Net saving: USD 4.31/run`, a single
    incremental run taken off a full rebuild.

    Read off `comparable`, not `skip_reason`, because `exclude:` overwrites the
    reason — so asking the reason let a user who silenced the gate for one model
    put that model's incomparable figure back in the total.

    A function over deltas rather than only a `Report` property, because
    `notices.collect` runs before a `Report` exists.
    """
    return [d for d in deltas if d.bytes_delta is not None and d.comparable]


def monthly_scan_bytes(deltas: list[CostDelta]) -> int | None:
    """What these models are projected to scan in a month, or None when that
    cannot be said.

    The one absolute run-level figure in the codebase: every other total here is
    a delta. It exists so a team that has declared a free-tier allowance
    (`pricing.free_tib_per_month`) can be told where this change sits relative to
    it. Nothing subtracts it — see `PricingDisclosure.free_tib_per_month`.

    None rather than a partial sum when any row is missing a piece, following
    `net_usd_per_month`: a total that quietly omits some of its rows reads as
    complete and understates, which is the one direction this tool does not go.
    In practice the missing piece is `runs_per_month`, i.e. no `run_frequency` in
    the config, and a notice says so. The `bytes_current` guard is defensive
    rather than live — `estimated` already drops those rows, since `bytes_delta`
    is None exactly when `bytes_current` is — but it keeps this correct on its
    own terms instead of by depending on a filter defined elsewhere.

    None on empty input, matching `net_bytes`. Zero would claim this change
    scans nothing in a month; None says there is nothing to tell you.
    """
    rows = estimated(deltas)
    if not rows:
        return None
    total = 0
    for d in rows:
        if d.bytes_current is None or not d.runs_per_month:
            return None
        total += d.bytes_current * d.runs_per_month
    return total


class Status(str, Enum):
    PASS = "pass"  # noqa: S105 — a gate verdict, not a credential
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
    # TiB/month the user has declared free, from `pricing.free_tib_per_month`.
    # None means undeclared, which is the default and prints exactly what it
    # always did. It rides on the disclosure because it is provenance about the
    # price rather than a measurement, and because both renderers already read
    # the disclosure for the header and the footer.
    #
    # Declared, never deducted: BigQuery's allowance belongs to the whole billing
    # account and is drawn down by every query anyone runs, which a dry-run
    # cannot see. Subtracting it could only mean assuming it is still unspent,
    # and a gate that forgives the first TiB of a regression on an unverified
    # assumption is worse than one that over-reports. So this changes what a
    # report *says* and never what the gate reads.
    free_tib_per_month: float | None = None

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
        """Models with a usable before/after — see the module-level `estimated`."""
        return estimated(self.deltas)

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
    def monthly_scan_bytes(self) -> int | None:
        """What these models are projected to scan in a month — see the
        module-level `monthly_scan_bytes`."""
        return monthly_scan_bytes(self.deltas)

    @property
    def unestimated_count(self) -> int:
        return len(self.deltas) - len(self.estimated)
