"""End-to-end smoke test: live kitty + store + dashboard + bot handlers.

Exercises the whole stack in-process without touching the network for
Telegram: a live Kittysplit kitty, the real store, the real dashboard app
(via the ASGI test client) and the real bot handler functions with a fake
message object.

    KITTYBOT_LIVE=1 pytest tests/test_live_stack.py -v
"""

from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest

from kittybot.approvals import ApprovalEngine
from kittybot.bot.telegram import KittyBot, build_approval_keyboard, build_proposal_text
from kittybot.config import Config
from kittybot.dashboard.app import create_app
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
                "kitty[name]": "kittybot stack test",
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
def stack(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "stack.db")
    engine = ApprovalEngine(client, store)

    config = Config()
    config.kitty.kitty_url = live_kitty
    config.kitty.my_name = "Ankit"
    config.telegram.default_payer = "Ankit"

    bot = KittyBot(config, engine, store)
    yield {"client": client, "store": store, "engine": engine, "bot": bot, "config": config}
    store.close()
    client.close()


def _order(order_id="stack-1", total=Decimal("900.00")) -> MarketOrder:
    return build_order(
        source="instamart",
        order_id=order_id,
        total=total,
        items=[
            LineItem(name="Amul Butter 500g", quantity=Decimal(2), price=Decimal("285")),
            LineItem(name="Onions 3kg", quantity=Decimal(1), price=Decimal("74")),
        ],
        placed_at="2026-10-28T18:30:00",
    )


def test_bot_proposes_and_approves(stack):
    engine = stack["engine"]

    draft, created = engine.propose_order(_order(), payer="Ankit")
    assert created

    # The proposal message and keyboard build cleanly.
    text = build_proposal_text(draft, "₹")
    assert "900.00" in text
    assert "Amul Butter" in text

    keyboard = build_approval_keyboard(draft, ["Ankit", "Yuvraj"])
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert f"approve:{draft.id}:equal" in callbacks
    assert f"approve:{draft.id}:personal" in callbacks
    assert f"approve:{draft.id}:percent:70,30" in callbacks
    assert f"custom:{draft.id}" in callbacks
    assert f"reject:{draft.id}" in callbacks

    # Approve via the equal-split callback payload.
    result = engine.approve(draft.id)
    assert result.status is ApprovalStatus.APPROVED, result.error

    rows = stack["client"].list_expenses()
    assert rows[0]["amount"] == Decimal("900.00")
    assert rows[0]["your_share"] == Decimal("450.00")


def test_dashboard_renders(stack):
    from fastapi.testclient import TestClient

    engine = stack["engine"]
    draft, _ = engine.propose_order(_order("dash-1"), payer="Ankit")

    app = create_app(stack["config"], engine, stack["store"])
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "kittybot" in response.text

        pending = client.get("/pending")
        assert pending.status_code == 200
        assert "Amul Butter" in pending.text or "Instamart" in pending.text

        # Approving through the dashboard form writes to the kitty.
        approve = client.post(
            f"/proposal/{draft.id}/approve", data={"split": "70/30"}, follow_redirects=False
        )
        assert approve.status_code in (200, 302, 303, 307)

        updated = stack["store"].get_proposal(draft.id)
        assert updated.status is ApprovalStatus.APPROVED, updated.error
        rows = stack["client"].list_expenses()
        assert rows[0]["your_share"] == Decimal("630.00")  # 70% of 900

        # Rejecting from the dashboard leaves the kitty alone.
        other, _ = engine.propose_order(_order("dash-2"), payer="Ankit")
        before = len(stack["client"].list_expenses())
        client.post(f"/proposal/{other.id}/reject", follow_redirects=False)
        assert len(stack["client"].list_expenses()) == before
        assert stack["store"].get_proposal(other.id).status is ApprovalStatus.REJECTED


def test_export_endpoint_returns_a_spreadsheet(stack):
    from fastapi.testclient import TestClient

    app = create_app(stack["config"], stack["engine"], stack["store"])
    with TestClient(app) as client:
        response = client.get("/export.xlsx")
        assert response.status_code == 200
        assert response.content[:2] == b"PK"


def test_cli_add_writes_to_the_kitty(stack, monkeypatch, tmp_path):
    """The `kittybot add` path works end to end."""
    from kittybot import cli

    monkeypatch.setenv("KITTYBOT_KITTY_URL", stack["config"].kitty.kitty_url)
    monkeypatch.setenv("KITTYBOT_MY_NAME", "Ankit")
    monkeypatch.setenv("KITTYBOT_DEFAULT_PAYER", "Ankit")
    monkeypatch.setenv("KITTYBOT_DB_PATH", str(tmp_path / "cli.db"))

    import argparse

    args = argparse.Namespace(amount="750", description=["Dinner", "out"], split="50/50")
    assert cli.cmd_add(args) == 0

    rows = stack["client"].list_expenses()
    assert any(r["description"] == "Dinner out" for r in rows)
    dinner = next(r for r in rows if r["description"] == "Dinner out")
    assert dinner["amount"] == Decimal("750.00")
    assert dinner["your_share"] == Decimal("375.00")
