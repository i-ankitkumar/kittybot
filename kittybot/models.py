"""Core domain models: splits, expenses, orders and approvals.

The central idea: nothing is ever written to Kittysplit without an explicit
human approval. Market orders arrive as :class:`ExpenseDraft` proposals, the
user confirms (and adjusts) the split, and only then does the writer run.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

SplitMode = Literal["equal", "weight", "percent", "amount"]


class ApprovalStatus(str, enum.Enum):
    """Lifecycle of a proposed expense."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class Participant:
    """A person in the kitty."""

    name: str
    party_id: int | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


@dataclass
class Share:
    """One participant's slice of an expense.

    Exactly one of ``weight``/``percent``/``amount`` carries the value that
    matters for the active split mode; the others stay ``None``.
    """

    participant: str
    weight: Decimal | None = None
    percent: Decimal | None = None
    amount: Decimal | None = None
    involved: bool = True

    @property
    def key(self) -> str:
        return self.participant.strip().lower()


@dataclass
class Split:
    """How an expense is divided.

    ``equal``     every involved participant carries an equal share.
    ``weight``    shares are proportional to :attr:`Share.weight`.
    ``percent``   shares are given directly in percent (must total 100).
    ``amount``    shares are given as fixed currency amounts (must total the
                  expense amount).
    """

    mode: SplitMode
    shares: list[Share] = field(default_factory=list)

    @property
    def involved(self) -> list[Share]:
        return [s for s in self.shares if s.involved]

    def participant_names(self) -> list[str]:
        return [s.participant for s in self.involved]

    def validate(self, total: Decimal) -> None:
        """Raise :class:`ValueError` if the split is not internally consistent."""
        people = self.involved
        if not people:
            raise ValueError("split has no involved participants")
        if len(people) == 1:
            # A single participant always eats the whole amount, whatever the
            # mode says. Kittysplit accepts this (verified: personal expense).
            return
        if self.mode == "weight":
            total_weight = sum((s.weight or Decimal(0)) for s in people)
            if total_weight <= 0:
                raise ValueError("weights must sum to a positive number")
        elif self.mode == "percent":
            total_percent = sum((s.percent or Decimal(0)) for s in people)
            if abs(total_percent - Decimal(100)) > Decimal("0.01"):
                raise ValueError(f"percents must total 100, got {total_percent}")
        elif self.mode == "amount":
            total_amount = sum((s.amount or Decimal(0)) for s in people)
            if abs(total_amount - total) > Decimal("0.01"):
                raise ValueError(
                    f"fixed amounts must total the expense amount ({total}), got {total_amount}"
                )


@dataclass
class Expense:
    """An expense as it exists in Kittysplit."""

    description: str
    amount: Decimal
    paid_by: str
    split: Split
    date: str  # ISO yyyy-mm-dd
    category_id: int | None = None
    entry_id: int | None = None
    url: str | None = None


@dataclass
class LineItem:
    """A single item inside a market order."""

    name: str
    quantity: Decimal = Decimal(1)
    price: Decimal | None = None  # per-unit or line total, source dependent

    @property
    def line_total(self) -> Decimal | None:
        if self.price is None:
            return None
        return self.price * self.quantity


@dataclass
class MarketOrder:
    """A normalised order pulled from a grocery marketplace."""

    source: str
    order_id: str
    total: Decimal
    placed_at: str | None = None  # ISO datetime
    items: list[LineItem] = field(default_factory=list)
    currency: str = "INR"
    raw: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}:{self.order_id}"

    def summary(self) -> str:
        if self.items:
            return ", ".join(i.name for i in self.items[:6])
        return "(no item breakdown available)"


@dataclass
class ExpenseDraft:
    """A proposed expense awaiting human approval.

    This is the only thing that ever reaches the Kittysplit writer, and it
    always arrives with a decision attached.
    """

    description: str
    amount: Decimal
    paid_by: str
    split: Split
    date: str
    category_id: int | None = None
    source: str = "manual"
    source_ref: str | None = None
    items: list[LineItem] = field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    kitty_entry_id: int | None = None
    error: str | None = None
    #: set by the store when the draft is persisted
    id: int | None = None
    kitty_url: str | None = None

    @property
    def dedupe_key(self) -> str | None:
        return f"{self.source}:{self.source_ref}" if self.source_ref else None

    def split_summary(self) -> str:
        people = self.split.involved
        if len(people) == 1:
            return f"100% {people[0].participant}"
        if self.split.mode == "equal":
            return f"equal between {', '.join(p.participant for p in people)}"
        if self.split.mode == "percent":
            return ", ".join(
                f"{p.participant} {p.percent:g}%" for p in people if p.percent is not None
            )
        if self.split.mode == "weight":
            return ", ".join(
                f"{p.participant} {p.weight:g}" for p in people if p.weight is not None
            )
        if self.split.mode == "amount":
            return ", ".join(
                f"{p.participant} ₹{p.amount:g}" for p in people if p.amount is not None
            )
        return "unknown split"


@dataclass
class KittySummary:
    """Snapshot of a kitty: totals plus how to settle up."""

    kitty_name: str
    currency_symbol: str
    total_cost: Decimal
    your_cost: Decimal
    you_paid: Decimal
    you_are_owed: Decimal
    settlements: list[tuple[str, str, Decimal]] = field(default_factory=list)
