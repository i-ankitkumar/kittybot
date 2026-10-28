"""Integration test against a live Kittysplit kitty.

Creates a throwaway kitty, exercises every split mode and the approval-shaped
write path, then deletes the kitty. Skipped unless KITTYBOT_LIVE=1.

    KITTYBOT_LIVE=1 pytest tests/test_live_kittysplit.py -v
"""

from __future__ import annotations

import os
import re
from decimal import Decimal

import httpx
import pytest

from kittybot.kittysplit import (
    BASE_URL,
    CATEGORY_FOOD,
    USER_AGENT,
    KittysplitClient,
)
from kittybot.models import Share, Split

pytestmark = pytest.mark.skipif(
    os.environ.get("KITTYBOT_LIVE") != "1",
    reason="set KITTYBOT_LIVE=1 to run against a live Kittysplit kitty",
)


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "no csrf token"
    return match.group(1)


@pytest.fixture(scope="module")
def live_kitty():
    """Create a throwaway kitty and delete it afterwards."""
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot live test",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": "Alpha",
                "kitty[parties][1][name]": "Beta",
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match, f"kitty creation failed, landed on {resp.url}"
        kitty_path = match.group(1)
        yield f"{BASE_URL}/{kitty_path}"

        page = http.get(f"{BASE_URL}/{kitty_path}/delete")
        http.post(
            f"{BASE_URL}/{kitty_path}/delete",
            data={"_csrf_token": _csrf(page.text)},
        )


@pytest.fixture
def client(live_kitty):
    c = KittysplitClient(live_kitty)
    c.ensure_identity("Alpha")
    yield c
    c.close()


def test_participants_are_discovered(client):
    names = [p.name for p in client.participants()]
    assert set(names) == {"Alpha", "Beta"}


def test_resolve_party_id(client):
    assert client.resolve_party_id("Alpha") != client.resolve_party_id("Beta")


def test_equal_split(client):
    result = client.add_expense(
        "live equal",
        "100.00",
        paid_by="Alpha",
        split=KittysplitClient.equal_split(["Alpha", "Beta"]),
        category_id=CATEGORY_FOOD,
    )
    assert result["entry_id"] is not None
    rows = {r["description"]: r for r in client.list_expenses()}
    row = rows["live equal"]
    assert row["amount"] == Decimal("100.00")
    assert row["your_share"] == Decimal("50.00")


def test_weight_split(client):
    client.add_expense(
        "live weight",
        "200.00",
        paid_by="Alpha",
        split=KittysplitClient.weighted_split({"Alpha": "0.7", "Beta": "0.3"}),
    )
    rows = {r["description"]: r for r in client.list_expenses()}
    assert rows["live weight"]["your_share"] == Decimal("140.00")


def test_percent_split(client):
    client.add_expense(
        "live percent",
        "300.00",
        paid_by="Alpha",
        split=KittysplitClient.percent_split({"Alpha": "60", "Beta": "40"}),
    )
    rows = {r["description"]: r for r in client.list_expenses()}
    assert rows["live percent"]["your_share"] == Decimal("180.00")


def test_amount_split(client):
    client.add_expense(
        "live amount",
        "200.00",
        paid_by="Alpha",
        split=KittysplitClient.amount_split({"Alpha": "120", "Beta": "80"}),
    )
    rows = {r["description"]: r for r in client.list_expenses()}
    assert rows["live amount"]["your_share"] == Decimal("120.00")


def test_personal_expense_involves_only_one_person(client):
    client.add_expense(
        "live personal",
        "75.00",
        paid_by="Alpha",
        split=KittysplitClient.solo_split("Alpha"),
    )
    rows = {r["description"]: r for r in client.list_expenses()}
    row = rows["live personal"]
    assert row["your_share"] == Decimal("75.00")
    assert len(row["share_party_ids"]) == 1


def test_summary_reports_totals(client):
    summary = client.summary()
    assert summary.currency_symbol == "₹"
    assert summary.total_cost > 0
    assert summary.you_paid > 0


def test_export_xlsx_is_a_zip(client):
    data = client.export_xlsx()
    assert data[:2] == b"PK"


def test_delete_expense_removes_it(client):
    result = client.add_expense(
        "live delete me",
        "10.00",
        paid_by="Alpha",
        split=KittysplitClient.solo_split("Alpha"),
    )
    assert client.delete_expense(result["entry_id"]) is True
    descriptions = {r["description"] for r in client.list_expenses()}
    assert "live delete me" not in descriptions


def test_unknown_participant_is_rejected(client):
    from kittybot.kittysplit import KittysplitError

    with pytest.raises(KittysplitError):
        client.add_expense(
            "live bad",
            "10.00",
            paid_by="Nobody",
            split=KittysplitClient.solo_split("Alpha"),
        )


def test_invalid_percent_split_is_rejected(client):
    split = Split(
        mode="percent",
        shares=[
            Share(participant="Alpha", percent=Decimal(60)),
            Share(participant="Beta", percent=Decimal(30)),
        ],
    )
    with pytest.raises(ValueError, match="total 100"):
        split.validate(Decimal("100"))
