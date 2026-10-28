"""End-to-end test of the approval flow against a live kitty.

This is the test that matters: it proves that a market order becomes a
proposal, that a proposal does NOT touch the kitty until it is approved, and
that approving writes the right split.

    KITTYBOT_LIVE=1 pytest tests/test_live_flow.py -v
"""

from __future__ import annotations

import os
import re
from decimal import Decimal

import httpx
import pytest

from kittybot.approvals import ApprovalEngine, suggest_split
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, LineItem, MarketOrder
from kittybot.sources.base import build_order
from kittybot.store import Store


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "no csrf token"
    return match.group(1)


@pytest.fixture(scope="module")
def live_kitty():
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot flow test",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": "Ankit",
                "kitty[parties][1][name]": "Yuvraj",
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match, f"kitty creation failed: {resp.url}"
        path = match.group(1)
        yield f"{BASE_URL}/{path}"
        page = http.get(f"{BASE_URL}/{path}/delete")
        http.post(f"{BASE_URL}/{path}/delete", data={"_csrf_token": _csrf(page.text)})


@pytest.fixture
def engine(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "test.db")
    yield ApprovalEngine(client, store)
    store.close()
    client.close()


def _order(**kwargs) -> MarketOrder:
    default = {
        "source": "instamart",
        "order_id": "order-123",
        "total": Decimal("1248.00"),
        "items": [
            LineItem(name="Amul Butter 500g", quantity=Decimal(2), price=Decimal("285")),
            LineItem(name="Potatoes 2kg", quantity=Decimal(1), price=Decimal("120")),
        ],
        "placed_at": "2026-10-28T18:30:00",
    }
    default.update(kwargs)
    return build_order(**default)


def test_order_becomes_a_proposal_and_does_not_touch_the_kitty(engine):
    before = len(engine.client.list_expenses())
    draft, created = engine.propose_order(_order(), payer="Ankit")
    assert created is True
    assert draft.status is ApprovalStatus.PENDING
    assert len(engine.client.list_expenses()) == before


def test_approving_writes_the_split(engine):
    draft, _ = engine.propose_order(_order(), payer="Ankit")
    result = engine.approve(draft.id)

    assert result.status is ApprovalStatus.APPROVED, result.error
    assert result.kitty_entry_id is not None

    rows = {r["description"]: r for r in engine.client.list_expenses()}
    row = next(iter(rows.values()))
    assert row["amount"] == Decimal("1248.00")
    # Equal split between two people.
    assert row["your_share"] == Decimal("624.00")
    assert len(row["share_party_ids"]) == 2


def test_approving_with_a_custom_split(engine):
    draft, _ = engine.propose_order(_order(), payer="Ankit")
    result = engine.approve_with_spec(draft.id, "70/30")

    assert result.status is ApprovalStatus.APPROVED, result.error
    rows = engine.client.list_expenses()
    assert rows[0]["your_share"] == Decimal("873.60")  # 70% of 1248


def test_approving_a_personal_expense(engine):
    draft, _ = engine.propose_order(
        _order(order_id="order-personal", total=Decimal("450.00")),
        payer="Ankit",
    )
    result = engine.approve_with_spec(draft.id, "me")

    assert result.status is ApprovalStatus.APPROVED, result.error
    rows = engine.client.list_expenses()
    assert rows[0]["your_share"] == Decimal("450.00")
    assert len(rows[0]["share_party_ids"]) == 1


def test_rejecting_writes_nothing(engine):
    before = len(engine.client.list_expenses())
    draft, _ = engine.propose_order(_order(), payer="Ankit")
    engine.reject(draft.id)
    assert len(engine.client.list_expenses()) == before
    assert engine.store.get_proposal(draft.id).status is ApprovalStatus.REJECTED


def test_the_same_order_is_not_proposed_twice(engine):
    first, created_first = engine.propose_order(_order(), payer="Ankit")
    second, created_second = engine.propose_order(_order(), payer="Ankit")
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_a_pending_proposal_survives_a_restart(engine, tmp_path):
    draft, _ = engine.propose_order(_order(), payer="Ankit")
    store = Store(tmp_path / "test.db")
    reopened = store.get_proposal(draft.id)
    assert reopened is not None
    assert reopened.status is ApprovalStatus.PENDING
    store.close()


def test_suggested_split_defaults_to_equal(engine):
    order = _order()
    split = suggest_split(order, ["Ankit", "Yuvraj"], payer="Ankit")
    assert split.mode == "equal"
    assert split.participant_names() == ["Ankit", "Yuvraj"]


def test_suggested_split_is_personal_for_a_single_person_kitty():
    split = suggest_split(_order(), ["Ankit"], payer="Ankit")
    assert split.participant_names() == ["Ankit"]


def test_manual_expense_flow(engine):
    draft = engine.propose_manual(
        Decimal("600"), "Electricity bill", paid_by="Ankit",
        split=engine.client.equal_split(["Ankit", "Yuvraj"]),
    )
    result = engine.approve(draft.id)
    assert result.status is ApprovalStatus.APPROVED, result.error
    rows = engine.client.list_expenses()
    assert rows[0]["description"] == "Electricity bill"
    assert rows[0]["your_share"] == Decimal("300.00")
