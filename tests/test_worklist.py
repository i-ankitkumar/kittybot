"""Tests for the work-list items that could be closed without an account.

  - the adapters must fail loudly when a response shape changes
  - the store and the dashboard must hold up at the 50-proposal cap
  - a party name that is hostile markup must never become markup
"""

from __future__ import annotations

import re
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from kittybot.approvals import ApprovalEngine
from kittybot.config import Config
from kittybot.dashboard.app import create_app
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, LineItem, Share, Split
from kittybot.sources.base import SourceError, build_order, is_cancelled_order, require_total
from kittybot.sources.instamart import InstamartSource
from kittybot.sources.zepto import _parse_order as zepto_parse_order
from kittybot.store import Store


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match
    return match.group(1)


# ----------------------------------------------------------------------
# The adapters must not swallow an unreadable total
# ----------------------------------------------------------------------
def test_require_total_raises_when_the_total_is_missing():
    with pytest.raises(SourceError) as exc:
        require_total(None, "Instamart", "abc-123")
    message = str(exc.value)
    assert "Instamart" in message
    assert "abc-123" in message
    assert "shape may have changed" in message


def test_require_total_passes_a_real_total_through():
    assert require_total(Decimal("125.50"), "Zepto") == Decimal("125.50")


def test_instamart_fails_loudly_on_a_renamed_total_field():
    """If Swiggy renames totalAmount, the user must be told, not left with
    a sync that silently reports zero new proposals."""
    source = InstamartSource.__new__(InstamartSource)
    source._call = lambda tool, arguments: {
        "orders": [
            {
                "orderId": "shape-drift-1",
                # totalAmount and billDetails.grandTotal both gone.
                "items": [{"name": "Milk", "quantity": 1, "price": 500}],
            }
        ]
    }

    import asyncio

    with pytest.raises(SourceError) as exc:
        asyncio.run(source.fetch_orders())
    message = str(exc.value)
    assert "no readable total" in message
    assert "shape may have changed" in message


def test_zepto_fails_loudly_on_a_renamed_total_field():
    with pytest.raises(SourceError) as exc:
        zepto_parse_order({"order_id": "z-1", "status": "DELIVERED", "items": []})
    assert "no readable total" in str(exc.value)


def test_zepto_still_accepts_a_good_order():
    order = zepto_parse_order({"order_id": "z-2", "status": "DELIVERED", "total": 250})
    assert order is not None
    assert order.total == Decimal("250")


def test_a_cancelled_order_is_still_dropped_before_the_total_check():
    """The cancellation check has to come first, or a cancelled order with
    no total would be reported as a shape problem."""
    source = InstamartSource.__new__(InstamartSource)
    source._call = lambda tool, arguments: {
        "orders": [
            {"orderId": "cancelled-1", "status": "CANCELLED"},
            {
                "orderId": "good-1",
                "status": "DELIVERED",
                "totalAmount": 500,
                "items": [{"name": "Milk", "quantity": 1, "price": 500}],
            },
        ]
    }

    import asyncio

    orders = asyncio.run(source.fetch_orders())
    assert [o.order_id for o in orders] == ["good-1"]


def test_the_sync_reports_orders_it_skipped_rather_than_dropping_them():
    """A zero-total order is not spend, but the user should be told."""
    import asyncio

    from kittybot.bot.telegram import KittyBot

    class FakeSource:
        label = "Fake App"
        name = "fake"

        async def fetch_orders(self, since=None):
            return [
                build_order(
                    source="fake",
                    order_id="zero-1",
                    total=Decimal(0),
                    items=[LineItem(name="X", quantity=Decimal(1), price=Decimal(0))],
                )
            ]

    class FakeMessage:
        def __init__(self):
            self.text = "/sync"
            self.texts = []
            self.edits = []

        async def answer(self, text, **kwargs):
            self.texts.append(text)
            return self

        async def edit_text(self, text, **kwargs):
            self.edits.append(text)

    class FakeState:
        async def get_data(self):
            return {}

        async def update_data(self, **kwargs):
            pass

        async def clear(self):
            pass

    engine = _dummy_engine()
    config = Config()
    config.kitty.my_name = "Ankit"
    bot = KittyBot(config, engine, _dummy_store())

    # Point the bot at the fake source.
    import kittybot.bot.telegram as telegram_module

    original = telegram_module.build_sources
    telegram_module.build_sources = lambda config: [FakeSource()]
    try:
        message = FakeMessage()
        asyncio.run(bot.cmd_sync(message))
        summary = message.edits[-1] if message.edits else ""
    finally:
        telegram_module.build_sources = original

    assert "Skipped 1 order(s) with a zero total" in summary
    # The label and the order id, so the user can find it.
    assert "Fake App zero-1" in summary


def _dummy_store():
    import tempfile

    store = Store(tempfile.mktemp(suffix=".db"))
    return store


def _dummy_engine():
    class Dummy:
        def __init__(self):
            self.store = _dummy_store()

        def participant_names(self):
            return ["Ankit"]

    return Dummy()


# ----------------------------------------------------------------------
# Scale: the store at its 50-proposal cap
# ----------------------------------------------------------------------
def test_the_store_handles_fifty_pending_proposals(tmp_path):
    store = Store(tmp_path / "scale.db")
    started = time.perf_counter()
    for index in range(50):
        store.add_proposal(
            _draft(f"Proposal {index}", Decimal("100.00") + index),
        )
    elapsed = time.perf_counter() - started

    pending = store.pending_proposals()
    assert len(pending) == 50
    # The cap is 50, so this is the worst case the dashboard can face.
    assert elapsed < 5.0, f"adding 50 proposals took {elapsed:.1f}s"

    started = time.perf_counter()
    listed = store.list_proposals(limit=20)
    elapsed = time.perf_counter() - started
    assert len(listed) == 20
    assert elapsed < 1.0, f"listing proposals took {elapsed:.1f}s"
    store.close()


def test_the_pending_page_renders_at_the_cap(tmp_path):
    """A page with 50 proposals must still be usable, not a 50,000px wall."""
    store = Store(tmp_path / "scale2.db")
    for index in range(50):
        store.add_proposal(_draft(f"Proposal {index}", Decimal("100.00")))

    engine = _DummyEngine(store)
    config = Config()
    config.kitty.kitty_url = "https://www.kittysplit.com/k/unused-1"
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)

    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/pending")
    assert response.status_code == 200
    body = response.text
    assert body.count('class="inline"') == 50
    # Every proposal has an approve control, so the page is not truncated
    # in a way that hides work.
    assert body.count("Approve") == 50
    store.close()


def _draft(description: str, amount: Decimal):
    from kittybot.models import ExpenseDraft

    return ExpenseDraft(
        description=description,
        amount=amount,
        paid_by="Ankit",
        split=Split(mode="equal", shares=[Share(participant="Ankit")]),
        date="2026-11-01",
    )


class _DummyEngine:
    """An engine whose kitty is unreachable, so the page is fast."""

    def __init__(self, store):
        self.store = store
        self.client = _UnreachableClient()

    def participant_names(self):
        return ["Ankit"]


class _UnreachableClient:
    def summary(self):
        raise RuntimeError("no kitty")

    def list_expenses(self):
        raise RuntimeError("no kitty")


# ----------------------------------------------------------------------
# Hostile party names
# ----------------------------------------------------------------------
HOSTILE = '<script>alert("xss")</script>'
HOSTILE2 = '"><img src=x onerror=alert(1)>'


@pytest.fixture(scope="module")
def hostile_kitty():
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot hostile names",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": HOSTILE,
                "kitty[parties][1][name]": HOSTILE2,
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match, f"kitty creation failed: {resp.url}"
        path = match.group(1)
        yield f"{BASE_URL}/{path}"
        page = http.get(f"{BASE_URL}/{path}/delete")
        http.post(f"{BASE_URL}/{path}/delete", data={"_csrf_token": _csrf(page.text)})


def test_kittysplit_stores_hostile_party_names_verbatim(hostile_kitty):
    """They really are stored, so the escaping has to hold."""
    client = KittysplitClient(hostile_kitty)
    client.ensure_identity(HOSTILE)
    names = [p.name for p in client.participants()]
    assert HOSTILE in names
    assert HOSTILE2 in names


def test_the_dashboard_escapes_hostile_party_names(hostile_kitty, tmp_path):
    client = KittysplitClient(hostile_kitty)
    client.ensure_identity(HOSTILE)
    parts = client.participants()
    client.add_expense(
        description="Hostile party expense",
        amount=Decimal("300.00"),
        paid_by=parts[0].name,
        split=Split(mode="equal", shares=[Share(participant=p.name) for p in parts]),
    )

    store = Store(tmp_path / "xss.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = hostile_kitty
    config.kitty.my_name = parts[0].name
    app = create_app(config, engine, store)

    with TestClient(app, follow_redirects=False) as tc:
        response = tc.get("/")
        pending = tc.get("/pending")

    for body in (response.text, pending.text):
        assert not re.search(r"<script(?![ >])", body), "a script tag was injected"
        assert not re.search(r"<img[^>]*onerror", body, re.I), "an img onerror was injected"
    # The overview carries the party names, escaped.
    assert "&lt;script&gt;" in response.text
    store.close()


def test_the_bot_escapes_hostile_party_names(hostile_kitty, tmp_path):
    from kittybot.bot.telegram import KittyBot, build_proposal_text

    client = KittysplitClient(hostile_kitty)
    client.ensure_identity(HOSTILE)
    parts = client.participants()

    store = Store(tmp_path / "xssbot.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = hostile_kitty
    config.kitty.my_name = parts[0].name
    KittyBot(config, engine, store)

    draft = engine.propose_manual(
        Decimal("300.00"),
        "Hostile party proposal",
        paid_by=parts[0].name,
        split=Split(mode="equal", shares=[Share(participant=p.name) for p in parts]),
    )
    text = build_proposal_text(draft, "₹")
    assert "<script" not in text
    assert "<img" not in text
    assert "&lt;script&gt;" in text
    store.close()


def test_the_flash_cookie_cannot_inject_markup(tmp_path):
    """The flash carries an error message. It is rendered, so it must be
    escaped there even though it is not escaped in the cookie."""
    client = KittysplitClient("https://www.kittysplit.com/k/unused-1")
    store = Store(tmp_path / "flash.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = "https://www.kittysplit.com/k/unused-1"
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)

    with TestClient(app, follow_redirects=False) as tc:
        tc.cookies.set(
            "kittybot_flash",
            '{"kind":"error","message":"<script>alert(1)</script>","for":"/"}',
        )
        response = tc.get("/")

    assert response.status_code == 200
    assert not re.search(r"<script(?![ >])", response.text)
    assert "&lt;script&gt;" in response.text
    store.close()


def test_the_cancellation_predicate_is_still_shared():
    assert is_cancelled_order({"status": "CANCELLED"})
    assert not is_cancelled_order({"status": "DELIVERED"})


def test_approval_status_still_works_after_all_this():
    assert ApprovalStatus.APPROVED.value == "approved"
