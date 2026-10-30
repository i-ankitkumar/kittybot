"""The approval gate.

This module is the reason the tool is safe to run unattended: a market order
becomes a *proposal*, and only an explicit human decision turns it into an
expense. There is no code path that writes to Kittysplit without one.

Flow:

    order  ->  ExpenseDraft (pending)  ->  approve/split/reject  ->  kitty

The default proposed split is deliberately the *most conservative* one for a
shared kitty: an equal split between everyone, which the user then narrows.
Personal orders are recognised as such and proposed as 100% one person.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from .kittysplit import CATEGORY_FOOD, KittysplitClient, KittysplitError
from .models import (
    ApprovalStatus,
    ExpenseDraft,
    MarketOrder,
    Participant,
    Share,
    Split,
)
from .splits import parse_split
from .store import Store

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """Raised when an approval cannot be carried out."""


def suggest_split(
    order: MarketOrder,
    participants: list[str],
    *,
    payer: str,
    default_mode: str = "equal",
) -> Split:
    """Propose a split for an imported order.

    Equal is the default because it is the only defensible guess, and the
    user is asked to confirm it every single time. Personal orders (a single
    clearly personal item) are proposed as 100% the payer.
    """
    if default_mode == "personal" or len(participants) == 1:
        return Split(mode="equal", shares=[Share(participant=payer)])

    if default_mode == "personal-items" and _looks_personal(order):
        return Split(mode="equal", shares=[Share(participant=payer)])

    return Split(mode="equal", shares=[Share(participant=p) for p in participants])


def _looks_personal(order: MarketOrder) -> bool:
    """Heuristic: is this order clearly one person's stuff?

    Only used to *pre-select* a proposal, never to auto-approve. A single
    item with a name that reads like a personal purchase is a hint, not a
    decision.
    """
    if len(order.items) != 1:
        return False
    name = order.items[0].name.lower()
    personal_words = (
        "headphone", "earphone", "shampoo", "soap", "toothpaste", "razor",
        "deodorant", "perfume", "cosmetic", "skincare", "laptop", "phone",
        "charger", "cable", "book", "novel", "gym", "medicine", "tablet",
        "watch", "sunglass", "wallet", "belt",
    )
    return any(word in name for word in personal_words)


def describe_order(order: MarketOrder, symbol: str = "₹") -> str:
    """A readable summary of an order for a chat message."""
    lines = [f"{order.source} · {symbol}{order.total:.2f}"]
    if order.placed_at:
        lines.append(f"   {order.placed_at}")
    for item in order.items[:8]:
        price = f"{symbol}{item.price:.2f}" if item.price is not None else "?"
        lines.append(f"   · {item.name} ×{item.quantity:g} — {price}")
    if len(order.items) > 8:
        lines.append(f"   … and {len(order.items) - 8} more")
    return "\n".join(lines)


class ApprovalEngine:
    """Owns the proposal lifecycle and the Kittysplit write."""

    def __init__(self, client: KittysplitClient, store: Store) -> None:
        self.client = client
        self.store = store
        # A proposal claimed by a process that died mid-write would be stuck
        # in "approving" for ever. Release anything abandoned before this
        # process started claiming things of its own.
        released = self.store.release_stale_claims()
        if released:
            logger.warning(
                "released %s proposal(s) left in 'approving' by a previous run",
                released,
            )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def participants(self) -> list[Participant]:
        return self.client.participants()

    def participant_names(self) -> list[str]:
        return [p.name for p in self.client.participants()]

    # ------------------------------------------------------------------
    # proposing
    # ------------------------------------------------------------------
    def propose_order(
        self,
        order: MarketOrder,
        *,
        payer: str,
        split: Split | None = None,
        category_id: int | None = CATEGORY_FOOD,
        description: str | None = None,
    ) -> tuple[ExpenseDraft, bool]:
        """Turn an order into a proposal. Returns (draft, created)."""
        if split is None:
            split = suggest_split(order, self.participant_names(), payer=payer)

        when = _order_date(order)
        label = description or _order_description(order)

        draft = ExpenseDraft(
            description=label,
            amount=order.total,
            paid_by=payer,
            split=split,
            date=when,
            category_id=category_id,
            source=order.source,
            source_ref=order.order_id,
            items=list(order.items),
        )
        draft, created = self.store.add_proposal(draft)
        self.store.mark_order_seen(
            order.dedupe_key,
            order.source,
            order.order_id,
            order.total,
            order.placed_at,
            order.raw,
        )
        return draft, created

    def propose_manual(
        self,
        amount: Decimal,
        description: str,
        *,
        paid_by: str,
        split: Split,
        date_str: str | None = None,
        category_id: int | None = None,
    ) -> ExpenseDraft:
        """A proposal typed by hand in Telegram."""
        draft = ExpenseDraft(
            description=description,
            amount=amount,
            paid_by=paid_by,
            split=split,
            date=date_str or date.today().isoformat(),
            category_id=category_id,
            source="manual",
        )
        draft, _ = self.store.add_proposal(draft)
        return draft

    # ------------------------------------------------------------------
    # deciding
    # ------------------------------------------------------------------
    def approve(self, proposal_id: int, split: Split | None = None) -> ExpenseDraft:
        """Approve a proposal and write it to Kittysplit.

        This is the ONLY method in the codebase that creates an expense.

        The proposal is claimed atomically before the write. Without that, a
        double-clicked Approve button (or two tabs, or a browser retry)
        reads "pending" twice and writes the expense twice — which charges
        the flatmate twice.
        """
        draft = self.store.get_proposal(proposal_id)
        if draft is None:
            raise ApprovalError(f"no proposal with id {proposal_id}")
        if draft.status is not ApprovalStatus.PENDING:
            raise ApprovalError(
                f"proposal {proposal_id} is already {draft.status.value}"
            )

        final_split = split or draft.split
        final_split.validate(draft.amount)

        # Atomic claim: exactly one caller gets here.
        if not self.store.claim_proposal(proposal_id):
            raise ApprovalError(
                f"proposal {proposal_id} is being approved by someone else"
            )

        try:
            result = self.client.add_expense(
                description=draft.description,
                amount=draft.amount,
                paid_by=draft.paid_by,
                split=final_split,
                date=draft.date,
                category_id=draft.category_id,
            )
        except KittysplitError as exc:
            return self.store.decide(  # type: ignore[return-value]
                proposal_id,
                ApprovalStatus.FAILED,
                split=final_split,
                error=str(exc),
            )

        return self.store.decide(  # type: ignore[return-value]
            proposal_id,
            ApprovalStatus.APPROVED,
            split=final_split,
            kitty_entry_id=result.get("entry_id"),
            kitty_url=result.get("url"),
        )

    def approve_with_spec(self, proposal_id: int, spec: str) -> ExpenseDraft:
        """Approve a proposal using a split written as text."""
        draft = self.store.get_proposal(proposal_id)
        if draft is None:
            raise ApprovalError(f"no proposal with id {proposal_id}")
        split = parse_split(spec, self.participant_names(), total=draft.amount)
        return self.approve(proposal_id, split)

    def reject(self, proposal_id: int) -> ExpenseDraft | None:
        """Reject a proposal. Nothing is written to the kitty."""
        return self.store.decide(proposal_id, ApprovalStatus.REJECTED)

    def skip(self, proposal_id: int) -> ExpenseDraft | None:
        """Skip a proposal for now, leaving it pending."""
        return self.store.decide(proposal_id, ApprovalStatus.SKIPPED)

    def pending(self) -> list[ExpenseDraft]:
        return self.store.pending_proposals()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _order_date(order: MarketOrder) -> str:
    """The expense date for an order."""
    if order.placed_at:
        text = order.placed_at.strip()
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            if len(text) >= 10:
                return text[:10]
    return date.today().isoformat()


def _order_description(order: MarketOrder) -> str:
    """A short label for the kitty entry."""
    label = order.source.capitalize()
    if order.items:
        names = [i.name for i in order.items[:2]]
        return f"{label}: {', '.join(names)}"[:120]
    return f"{label} order"
