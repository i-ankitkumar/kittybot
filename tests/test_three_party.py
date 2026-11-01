"""Tests for the multi-party and robustness gaps found in the work list.

Everything so far ran against a two-party kitty where "me" is one of two
people. These run against a THREE-party kitty with a third person who is
neither of us, a payer who is not the viewer, and a personal expense
belonging to someone else.
"""

from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from kittybot.approvals import ApprovalEngine
from kittybot.config import Config
from kittybot.dashboard.app import create_app
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, LineItem, Share, Split
from kittybot.sources.base import build_order
from kittybot.store import Store


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "no csrf token"
    return match.group(1)


@pytest.fixture(scope="module")
def three_party_kitty():
    """A kitty with Ankit, Yuvraj and Rohan."""
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot three party",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": "Ankit",
                "kitty[parties][1][name]": "Yuvraj",
                "kitty[parties][2][name]": "Rohan",
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match, f"kitty creation failed: {resp.url}"
        path = match.group(1)
        yield f"{BASE_URL}/{path}"
        page = http.get(f"{BASE_URL}/{path}/delete")
        http.post(f"{BASE_URL}/{path}/delete", data={"_csrf_token": _csrf(page.text)})


@pytest.fixture
def three_parts(three_party_kitty, tmp_path):
    client = KittysplitClient(three_party_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "three.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = three_party_kitty
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)
    yield app, engine, store, client
    store.close()
    client.close()


# ----------------------------------------------------------------------
# The split maths has to hold for three people
# ----------------------------------------------------------------------
def test_a_three_way_equal_split_divides_by_three(three_parts):
    _app, engine, store, client = three_parts
    before = len(client.list_expenses())
    result = client.add_expense(
        description="Three way equal",
        amount=Decimal("300.00"),
        paid_by="Ankit",
        split=Split(
            mode="equal",
            shares=[Share(participant=n) for n in ("Ankit", "Yuvraj", "Rohan")],
        ),
    )
    assert result.get("entry_id")
    rows = client.list_expenses()
    assert len(rows) == before + 1
    new = [r for r in rows if r["description"] == "Three way equal"]
    assert new, "the expense was not written"
    assert new[0]["your_share"] == Decimal("100.00")
    assert len(new[0]["share_party_ids"]) == 3


def test_a_three_way_percent_split_is_accurate(three_parts):
    _app, engine, store, client = three_parts
    client.add_expense(
        description="Three way percent",
        amount=Decimal("1000.00"),
        paid_by="Yuvraj",
        split=Split(
            mode="percent",
            shares=[
                Share(participant="Ankit", percent=Decimal(50)),
                Share(participant="Yuvraj", percent=Decimal(30)),
                Share(participant="Rohan", percent=Decimal(20)),
            ],
        ),
    )
    rows = client.list_expenses()
    new = [r for r in rows if r["description"] == "Three way percent"][0]
    # Ankit's 50% of 1000.
    assert new["your_share"] == Decimal("500.00")


def test_a_personal_expense_for_someone_else_leaves_you_out(three_parts):
    """Ankit is not involved in Rohan's headphones, so he owes nothing."""
    _app, engine, store, client = three_parts
    client.add_expense(
        description="Rohan personal headphones",
        amount=Decimal("1499.00"),
        paid_by="Rohan",
        split=Split(mode="equal", shares=[Share(participant="Rohan")]),
    )
    rows = client.list_expenses()
    new = [r for r in rows if r["description"] == "Rohan personal headphones"][0]
    assert new["your_share"] is None
    assert len(new["share_party_ids"]) == 1


def test_the_dashboard_survives_an_expense_you_are_not_involved_in(three_parts):
    """This one crashed the whole overview page with a TypeError."""
    app, engine, store, client = three_parts
    client.add_expense(
        description="Rohan personal headphones",
        amount=Decimal("1499.00"),
        paid_by="Rohan",
        split=Split(mode="equal", shares=[Share(participant="Rohan")]),
    )
    client.add_expense(
        description="Shared groceries",
        amount=Decimal("300.00"),
        paid_by="Yuvraj",
        split=Split(mode="equal", shares=[Share(participant=n) for n in ("Ankit", "Yuvraj")]),
    )
    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Rohan personal headphones" in body
    assert "not your expense" in body
    # And the expense you ARE part of still shows its share.
    assert "your share" in body


def test_the_dashboard_survives_someone_else_paying(three_parts):
    """The viewer is not the payer."""
    app, engine, store, client = three_parts
    client.add_expense(
        description="Yuvraj paid for this",
        amount=Decimal("300.00"),
        paid_by="Yuvraj",
        split=Split(mode="equal", shares=[Share(participant=n) for n in ("Ankit", "Yuvraj")]),
    )
    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")
    assert response.status_code == 200
    assert "Yuvraj paid" in response.text


# ----------------------------------------------------------------------
# The settlement plan
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def settlement_kitty():
    """Its own kitty with one expense, so the plan is deterministic.

    The shared three-party kitty accumulates expenses from the other tests,
    so the settlement plan in it changes as the module runs. The expense is
    added once here, by the fixture, because two tests reading the same
    kitty must not both write to it.
    """
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot settlement",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": "Ankit",
                "kitty[parties][1][name]": "Yuvraj",
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match
        path = match.group(1)
        url = f"{BASE_URL}/{path}"

        client = KittysplitClient(url)
        client.ensure_identity("Ankit")
        client.add_expense(
            description="Shared groceries",
            amount=Decimal("300.00"),
            paid_by="Yuvraj",
            split=Split(
                mode="equal", shares=[Share(participant=n) for n in ("Ankit", "Yuvraj")]
            ),
        )
        yield url

        page = http.get(f"{url}/delete")
        http.post(f"{url}/delete", data={"_csrf_token": _csrf(page.text)})


def test_the_settlement_plan_is_parsed(settlement_kitty):
    """It was always empty. The amount is written "₹ 150.00", with a space."""
    client = KittysplitClient(settlement_kitty)
    client.ensure_identity("Ankit")
    summary = client.summary()
    assert summary.settlements, "no settlement was parsed from the kitty page"
    payer, recipient, amount = summary.settlements[0]
    assert payer == "Ankit"
    assert recipient == "Yuvraj"
    assert amount == Decimal("150.00")


def test_the_dashboard_shows_the_settlement_plan(settlement_kitty, tmp_path):
    client = KittysplitClient(settlement_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "settle.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = settlement_kitty
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)
    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")
    assert response.status_code == 200
    assert "How to settle up" in response.text
    # The dashboard shows the settlement as "payer -> recipient".
    assert "Ankit" in response.text
    assert "Yuvraj" in response.text
    assert "&rarr;" in response.text or "→" in response.text
    store.close()


def test_the_settlement_parser_handles_the_real_markup():
    """A unit test against the exact markup Kittysplit renders."""
    from bs4 import BeautifulSoup

    from kittybot.kittysplit import _settlement_texts

    html = """
    <li class="transaction ks-data-row">
      <div class="transaction-text">Ankit gives ₹ 150.00 to Yuvraj</div>
      <button>Mark as settled</button>
    </li>
    <li class="transaction ks-data-row">
      <div class="transaction-text">Rohan gives ₹ 1,250.50 to Yuvraj</div>
    </li>
    """
    soup = BeautifulSoup(html, "html.parser")
    found = _settlement_texts(soup)
    assert len(found) == 2
    assert found[0] == ("Ankit", "₹ 150.00", "Yuvraj")
    assert found[1] == ("Rohan", "₹ 1,250.50", "Yuvraj")


def test_the_settlement_parser_handles_names_with_spaces():
    from bs4 import BeautifulSoup

    from kittybot.kittysplit import _settlement_texts

    html = """
    <div class="transaction-text">Ankit Kumar gives ₹ 150.00 to Yuvraj Sharma</div>
    """
    soup = BeautifulSoup(html, "html.parser")
    found = _settlement_texts(soup)
    assert found == [("Ankit Kumar", "₹ 150.00", "Yuvraj Sharma")]


# ----------------------------------------------------------------------
# An unreachable kitty must not look like an empty one
# ----------------------------------------------------------------------
def test_an_unreachable_kitty_says_so(tmp_path):
    """A blanket except turned "cannot reach Kittysplit" into "No expenses
    found", which on a money dashboard reads like "you are square"."""
    broken_url = "https://www.kittysplit.com/k/this-key-does-not-exist-999"
    client = KittysplitClient(broken_url)
    store = Store(tmp_path / "broken.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = broken_url
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)

    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Cannot reach Kittysplit" in body
    # It must NOT claim there are no expenses, and must NOT show a zero
    # group total, because both read as "you are square".
    assert "No expenses found." not in body
    assert "Group total" not in body
    store.close()


def test_a_healthy_kitty_shows_no_error_banner(three_parts):
    app, engine, store, client = three_parts
    client.add_expense(
        description="Shared groceries",
        amount=Decimal("300.00"),
        paid_by="Yuvraj",
        split=Split(mode="equal", shares=[Share(participant=n) for n in ("Ankit", "Yuvraj")]),
    )
    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")
    assert "Cannot reach Kittysplit" not in response.text
    assert "Group total" in response.text


# ----------------------------------------------------------------------
# The bot with a third party
# ----------------------------------------------------------------------
def test_the_bot_proposes_a_three_way_split(three_parts):
    _app, engine, store, client = three_parts
    draft, _ = engine.propose_order(
        build_order(
            source="instamart",
            order_id="three-1",
            total=Decimal("900.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("900"))],
        ),
        payer="Ankit",
    )
    assert sorted(draft.split.participant_names()) == ["Ankit", "Rohan", "Yuvraj"]
    assert draft.split.mode == "equal"


def test_approving_a_three_way_split_writes_three_shares(three_parts):
    _app, engine, store, client = three_parts
    before = len(client.list_expenses())
    draft, _ = engine.propose_order(
        build_order(
            source="instamart",
            order_id="three-2",
            total=Decimal("900.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("900"))],
        ),
        payer="Ankit",
    )
    engine.approve(draft.id)
    rows = client.list_expenses()
    assert len(rows) == before + 1
    new = rows[0]
    assert len(new["share_party_ids"]) == 3
    assert new["your_share"] == Decimal("300.00")
    assert store.get_proposal(draft.id).status is ApprovalStatus.APPROVED
