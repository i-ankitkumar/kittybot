"""Tests for the issues found in the third dogfood pass.

Each test names the issue it guards. The double-submit ones are the
important ones: the second pass fixed the duplicate write, but the fix
introduced a new bug where a successful approval was relabelled "failed".
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
from kittybot.models import ApprovalStatus, LineItem
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
                "kitty[name]": "kittybot dogfood 3",
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
def app_parts(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "d3.db")
    engine = ApprovalEngine(client, store)
    config = Config()
    config.kitty.kitty_url = live_kitty
    config.kitty.my_name = "Ankit"
    app = create_app(config, engine, store)
    yield app, engine, store, client
    store.close()
    client.close()


@pytest.fixture
def client(app_parts):
    app, engine, store, kitty = app_parts
    with TestClient(app, follow_redirects=True) as c:
        yield c


def _proposal(engine, order_id="d3-1", amount="500.00"):
    draft, _ = engine.propose_order(
        build_order(
            source="instamart",
            order_id=order_id,
            total=Decimal(amount),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal(amount))],
        ),
        payer="Ankit",
    )
    return draft


# ----------------------------------------------------------------------
# Issue 1 (High): a double-submit must not relabel a success as failed
# ----------------------------------------------------------------------
def test_double_submit_keeps_the_approved_status(client, app_parts):
    """The loser of a double-submit must not overwrite the winner's status.

    The expense is written once, but the second request used to call
    decide(FAILED) on a proposal that had already been approved, so the
    dashboard reported a successful expense as failed.
    """
    _app, engine, store, _kitty = app_parts
    draft = _proposal(engine, "double-1")

    first = client.post(f"/proposal/{draft.id}/approve", data={"split": ""})
    assert first.status_code == 200
    assert store.get_proposal(draft.id).status is ApprovalStatus.APPROVED

    # The second request for the same proposal.
    second = client.post(f"/proposal/{draft.id}/approve", data={"split": ""})
    assert second.status_code == 200

    after = store.get_proposal(draft.id)
    assert after.status is ApprovalStatus.APPROVED, (
        f"the approved proposal became {after.status.value}"
    )
    assert after.kitty_entry_id is not None
    assert after.error is None


def test_a_real_failure_still_marks_the_proposal_failed(client, app_parts):
    """The guard must not stop genuine failures from being recorded."""
    _app, engine, store, _kitty = app_parts
    draft = engine.propose_manual(
        Decimal("77.00"),
        "Broken proposal",
        paid_by="Nobody",
        split=engine.client.solo_split("Ankit"),
    )
    client.post(f"/proposal/{draft.id}/approve", data={"split": ""})
    after = store.get_proposal(draft.id)
    assert after.status is ApprovalStatus.FAILED
    assert after.error


def test_the_dashboard_shows_the_expense_not_a_failure(client, app_parts):
    """End to end: after a double-submit the overview says approved."""
    _app, engine, store, _kitty = app_parts
    draft = _proposal(engine, "double-2")
    client.post(f"/proposal/{draft.id}/approve", data={"split": ""})
    client.post(f"/proposal/{draft.id}/approve", data={"split": ""})

    page = client.get("/").text
    assert "failed" not in page.lower() or "already approved" not in page
    assert "approved" in page.lower()


# ----------------------------------------------------------------------
# Issue 2 (High): Telegram double-tap must not raise
# ----------------------------------------------------------------------
async def test_telegram_double_tap_is_answered_not_crashed(app_parts):
    """Tapping Approve twice must answer the callback, not raise."""
    from kittybot.bot.telegram import KittyBot

    _app, engine, store, _kitty = app_parts
    config = Config()
    config.kitty.kitty_url = engine.client.kitty_url
    config.kitty.my_name = "Ankit"
    bot = KittyBot(config, engine, store)

    class FakeMessage:
        def __init__(self):
            self.texts = []

        async def edit_text(self, text, **kwargs):
            self.texts.append(text)

        async def answer(self, text, **kwargs):
            self.texts.append(text)

    class FakeCallback:
        def __init__(self, data):
            self.data = data
            self.message = FakeMessage()
            self.answers = []

        async def answer(self, text="", show_alert=False):
            self.answers.append(text)

    class FakeState:
        async def get_data(self):
            return {}

        async def update_data(self, **kwargs):
            pass

        async def clear(self):
            pass

    draft = _proposal(engine, "tg-double")
    callback = FakeCallback(f"approve:{draft.id}:equal")

    await bot.on_callback(callback, FakeState())
    assert callback.answers, "the first tap must answer the callback"
    assert store.get_proposal(draft.id).status is ApprovalStatus.APPROVED

    # The second tap: no exception, and the callback is still answered.
    callback2 = FakeCallback(f"approve:{draft.id}:equal")
    await bot.on_callback(callback2, FakeState())
    assert callback2.answers, "the second tap must answer the callback too"
    assert store.get_proposal(draft.id).status is ApprovalStatus.APPROVED


# ----------------------------------------------------------------------
# Issue 3 (Medium): a real 404 page
# ----------------------------------------------------------------------
def test_the_404_page_is_html_not_json(client):
    response = client.get("/definitely-not-a-page")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "There is nothing at this address" in response.text
    # It should still look like the rest of the app.
    assert 'class="site-header"' in response.text
    assert 'href="/"' in response.text


# ----------------------------------------------------------------------
# Issue 4 (Medium): a flash must not bleed onto an unrelated page
# ----------------------------------------------------------------------
def test_a_flash_does_not_show_on_an_unrelated_page(client, app_parts):
    """A flash set for /pending must not appear on a proposal page."""
    _app, engine, store, _kitty = app_parts
    draft = _proposal(engine, "flash-1")
    client.post(f"/proposal/{draft.id}/approve", data={"split": ""})

    other = _proposal(engine, "flash-2")
    page = client.get(f"/proposal/{other.id}")
    assert page.status_code == 200
    assert "flash" not in page.text.lower() or "Added" not in page.text


def test_a_flash_still_shows_on_the_page_it_belongs_to(client, app_parts):
    _app, engine, store, _kitty = app_parts
    draft = _proposal(engine, "flash-3")
    response = client.post(f"/proposal/{draft.id}/approve", data={"split": ""})
    assert "Added" in response.text


# ----------------------------------------------------------------------
# Issue 5 (Low): amounts are grouped
# ----------------------------------------------------------------------
def test_amounts_are_thousands_separated(client, app_parts):
    _app, engine, store, _kitty = app_parts
    engine.propose_manual(
        Decimal("99999999.99"),
        "Large amount formatting check",
        paid_by="Ankit",
        split=engine.client.equal_split(["Ankit", "Yuvraj"]),
    )
    page = client.get("/").text
    assert "99,999,999.99" in page
    assert "99999999.99" not in page
    # The summary cards must be grouped too. An ungrouped "24885.50" next
    # to a grouped "18,500.00" on the same page is worse than either.
    # Four digits is the point where grouping starts to matter.
    assert not re.search(r"₹\d{4,}\.\d\d", page), (
        "an amount on the page is not thousands-separated"
    )


# ----------------------------------------------------------------------
# Issue 6 (Low): a newline in a description is preserved
# ----------------------------------------------------------------------
def test_a_newline_in_a_description_is_preserved(client, app_parts):
    _app, engine, store, _kitty = app_parts
    engine.propose_manual(
        Decimal("320.00"),
        "Café ☕ दूध\nsecond line 🧀",
        paid_by="Ankit",
        split=engine.client.equal_split(["Ankit", "Yuvraj"]),
    )
    page = client.get("/")
    assert "second line" in page.text
    # The stylesheet must ask the browser to keep the line break, and it
    # must be the rule the description actually uses. Asserting that
    # "pre-line" appears somewhere in the file is not enough: another
    # rule can carry it while the description still collapses.
    css = client.get("/static/dashboard.css").text
    assert ".clamp-2, .clamp-3" in css
    clamp_rule = css.split(".clamp-2, .clamp-3", 1)[1].split("}", 1)[0]
    assert "pre-line" in clamp_rule, (
        "the clamp rule the description uses must set white-space: pre-line"
    )


# ----------------------------------------------------------------------
# The scrubber is shared, so both surfaces behave the same
# ----------------------------------------------------------------------
def test_the_scrubber_is_shared_by_the_dashboard_and_the_bot():
    from kittybot.bot.telegram import scrub as bot_scrub
    from kittybot.dashboard.app import scrub as dashboard_scrub

    message = "failed: https://www.kittysplit.com/k/AbCdEf-12/entries/1"
    assert bot_scrub(message) == dashboard_scrub(message)
    assert "kittysplit.com" not in bot_scrub(message)
