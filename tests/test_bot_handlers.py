"""Tests for the Telegram bot handlers.

The bot is the primary interface, and it was the one part of the project
that had no command-level tests. These drive the real handler functions with
fake Message and CallbackQuery objects, against a live kitty, so the whole
path is exercised: command -> proposal -> approval -> Kittysplit entry.
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from kittybot.approvals import ApprovalEngine
from kittybot.bot.telegram import KittyBot, build_approval_keyboard, build_proposal_text
from kittybot.config import Config
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, ExpenseDraft, LineItem, Share, Split
from kittybot.sources.base import build_order
from kittybot.store import Store


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "no csrf token"
    return match.group(1)


# ----------------------------------------------------------------------
# fake Telegram objects
# ----------------------------------------------------------------------
class FakeMessage:
    """Enough of a Telegram Message for the handlers."""

    def __init__(self, text: str = "", chat_id: int = 42):
        self.text = text
        self.chat_id = chat_id
        self.from_user = SimpleNamespace(id=chat_id)
        self.answers: list[str] = []
        self.documents: list[object] = []
        self.reply_markups: list[object] = []
        self.state = "start"

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        if "reply_markup" in kwargs:
            self.reply_markups.append(kwargs["reply_markup"])
        return FakeMessage(text, self.chat_id)

    async def answer_document(self, document=None, **kwargs):
        self.documents.append(document)
        return FakeMessage(kwargs.get("caption", ""), self.chat_id)

    async def edit_text(self, text, **kwargs):
        self.answers.append(text)
        return self

    @property
    def last(self) -> str:
        return self.answers[-1] if self.answers else ""


class FakeCallback:
    """Enough of a Telegram CallbackQuery."""

    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)
        if show_alert:
            self.alerts.append(text)

    async def edit_text(self, text, **kwargs):
        self.message.answers.append(text)
        self.edits.append(text)
        return self.message


class FakeState:
    def __init__(self):
        self.data: dict = {}
        self.cleared = False

    async def get_data(self):
        return self.data

    async def set_state(self, _):
        return None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.data = {}
        self.cleared = True


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
                "kitty[name]": "kittybot bot test",
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
def bot(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "bot.db")
    engine = ApprovalEngine(client, store)

    config = Config()
    config.kitty.kitty_url = live_kitty
    config.kitty.my_name = "Ankit"
    config.telegram.default_payer = "Ankit"

    yield KittyBot(config, engine, store)
    store.close()
    client.close()


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
async def test_add_command_creates_a_proposal(bot):
    message = FakeMessage("/add 450 groceries 50/50")
    await bot.cmd_add(message)

    assert "Proposed split" in message.last
    assert "₹450.00" in message.last
    pending = bot.engine.pending()
    assert len(pending) == 1
    assert pending[0].amount == Decimal("450")
    assert pending[0].description == "groceries"
    # "50/50" maps to Kittysplit's native equal split, not a percent split.
    assert pending[0].split.mode == "equal"
    assert pending[0].split.participant_names() == ["Ankit", "Yuvraj"]


async def test_add_command_without_a_split_asks_for_one(bot):
    message = FakeMessage("/add 450 groceries")
    await bot.cmd_add(message)
    pending = bot.engine.pending()
    assert len(pending) == 1
    # The default proposal is an equal split, which the user confirms.
    assert pending[0].split.mode == "equal"
    assert pending[0].split.participant_names() == ["Ankit", "Yuvraj"]


async def test_add_command_rejects_a_nonsense_amount(bot):
    message = FakeMessage("/add abc groceries")
    await bot.cmd_add(message)
    assert "amount" in message.last.lower()
    assert bot.engine.pending() == []


async def test_add_command_reports_an_unreadable_split(bot):
    message = FakeMessage("/add 450 groceries banana")
    await bot.cmd_add(message)
    assert "could not" in message.last.lower() or "understand" in message.last.lower()


async def test_approve_command_writes_to_the_kitty(bot):
    message = FakeMessage("/add 450 groceries 50/50")
    await bot.cmd_add(message)
    draft = bot.engine.pending()[0]

    before = len(bot.engine.client.list_expenses())
    approve = FakeMessage(f"/approve {draft.id}")
    await bot.cmd_approve(approve)

    assert "Added" in approve.last
    assert len(bot.engine.client.list_expenses()) == before + 1
    assert bot.store.get_proposal(draft.id).status is ApprovalStatus.APPROVED


async def test_approve_command_with_a_custom_split(bot):
    message = FakeMessage("/add 900 dinner")
    await bot.cmd_add(message)
    draft = bot.engine.pending()[0]

    before = len(bot.engine.client.list_expenses())
    approve = FakeMessage(f"/approve {draft.id} 70/30")
    await bot.cmd_approve(approve)

    assert "Added" in approve.last
    assert len(bot.engine.client.list_expenses()) == before + 1
    stored = bot.store.get_proposal(draft.id)
    assert stored.split.mode == "percent"
    assert stored.split.involved[0].percent == Decimal(70)


async def test_approve_command_reports_an_unknown_id(bot):
    approve = FakeMessage("/approve 9999")
    await bot.cmd_approve(approve)
    assert "9999" in approve.last


async def test_reject_command(bot):
    message = FakeMessage("/add 450 groceries")
    await bot.cmd_add(message)
    draft = bot.engine.pending()[0]

    before = len(bot.engine.client.list_expenses())
    reject = FakeMessage(f"/reject {draft.id}")
    await bot.cmd_reject(reject)

    assert "Rejected" in reject.last
    assert len(bot.engine.client.list_expenses()) == before
    assert bot.store.get_proposal(draft.id).status is ApprovalStatus.REJECTED


async def test_pending_command_lists_proposals(bot):
    message = FakeMessage("/add 450 groceries")
    await bot.cmd_add(message)
    pending = FakeMessage("/pending")
    await bot.cmd_pending(pending)
    assert "waiting" in pending.last.lower()
    assert "groceries" in pending.last


async def test_pending_command_when_nothing_waits(bot):
    pending = FakeMessage("/pending")
    await bot.cmd_pending(pending)
    assert "nothing waiting" in pending.last.lower()


async def test_balance_command(bot):
    message = FakeMessage("/add 450 groceries 50/50")
    await bot.cmd_add(message)
    draft = bot.engine.pending()[0]
    await bot.cmd_approve(FakeMessage(f"/approve {draft.id}"))

    balance = FakeMessage("/balance")
    await bot.cmd_balance(balance)
    assert "Total for the group" in balance.last
    assert "Cost to you" in balance.last


async def test_export_command_sends_a_spreadsheet(bot):
    export = FakeMessage("/export")
    await bot.cmd_export(export)
    assert export.documents
    name, data, media_type = export.documents[0]
    assert name == "kitty.xlsx"
    assert data[:2] == b"PK"


async def test_help_command_lists_the_commands(bot):
    help_message = FakeMessage("/help")
    await bot.cmd_help(help_message)
    for command in ("/add", "/sync", "/pending", "/approve", "/reject", "/balance"):
        assert command in help_message.last


async def test_unknown_command_says_so(bot):
    message = FakeMessage("hello there")
    await bot.on_unknown(message)
    assert "/help" in message.last


# ----------------------------------------------------------------------
# inline approval flow
# ----------------------------------------------------------------------
async def test_inline_equal_split_approval(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-1",
            total=Decimal("1000.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("1000"))],
        ),
        payer="Ankit",
    )
    before = len(bot.engine.client.list_expenses())

    callback = FakeCallback(f"approve:{draft.id}:equal", FakeMessage())
    await bot.on_callback(callback, FakeState())

    assert len(bot.engine.client.list_expenses()) == before + 1
    assert bot.store.get_proposal(draft.id).status is ApprovalStatus.APPROVED


async def test_inline_percent_split_approval(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-2",
            total=Decimal("1000.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("1000"))],
        ),
        payer="Ankit",
    )
    before = len(bot.engine.client.list_expenses())

    callback = FakeCallback(f"approve:{draft.id}:percent:70,30", FakeMessage())
    await bot.on_callback(callback, FakeState())

    assert len(bot.engine.client.list_expenses()) == before + 1
    stored = bot.store.get_proposal(draft.id)
    assert stored.split.mode == "percent"
    assert stored.split.involved[0].percent == Decimal(70)


async def test_inline_personal_approval(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-3",
            total=Decimal("500.00"),
            items=[LineItem(name="Headphones", quantity=Decimal(1), price=Decimal("500"))],
        ),
        payer="Ankit",
    )
    before = len(bot.engine.client.list_expenses())

    callback = FakeCallback(f"approve:{draft.id}:personal", FakeMessage())
    await bot.on_callback(callback, FakeState())

    assert len(bot.engine.client.list_expenses()) == before + 1
    rows = bot.engine.client.list_expenses()
    assert len(rows[0]["share_party_ids"]) == 1


async def test_inline_reject(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-4",
            total=Decimal("500.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("500"))],
        ),
        payer="Ankit",
    )
    before = len(bot.engine.client.list_expenses())

    callback = FakeCallback(f"reject:{draft.id}", FakeMessage())
    await bot.on_callback(callback, FakeState())

    assert len(bot.engine.client.list_expenses()) == before
    assert bot.store.get_proposal(draft.id).status is ApprovalStatus.REJECTED


async def test_custom_split_button_then_typed_split(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-5",
            total=Decimal("1000.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("1000"))],
        ),
        payer="Ankit",
    )
    state = FakeState()
    before = len(bot.engine.client.list_expenses())

    callback = FakeCallback(f"custom:{draft.id}", FakeMessage())
    await bot.on_callback(callback, state)
    assert state.data.get("proposal_id") == draft.id

    # The user then types the split.
    typed = FakeMessage("60/40")
    await bot.on_text(typed, state)

    assert len(bot.engine.client.list_expenses()) == before + 1
    stored = bot.store.get_proposal(draft.id)
    assert stored.split.mode == "percent"
    assert stored.split.involved[0].percent == Decimal(60)


async def test_custom_split_reports_an_unreadable_split(bot):
    draft, _ = bot.engine.propose_order(
        build_order(
            source="instamart",
            order_id="inline-6",
            total=Decimal("1000.00"),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("1000"))],
        ),
        payer="Ankit",
    )
    state = FakeState()
    await bot.on_callback(FakeCallback(f"custom:{draft.id}", FakeMessage()), state)

    before = len(bot.engine.client.list_expenses())
    typed = FakeMessage("banana")
    await bot.on_text(typed, state)

    assert len(bot.engine.client.list_expenses()) == before
    assert "understand" in typed.last.lower() or "could not" in typed.last.lower()
    assert bot.store.get_proposal(draft.id).status is ApprovalStatus.PENDING


async def test_callback_for_a_missing_proposal_is_handled(bot):
    callback = FakeCallback("approve:9999:equal", FakeMessage())
    await bot.on_callback(callback, FakeState())
    assert callback.alerts


# ----------------------------------------------------------------------
# message rendering
# ----------------------------------------------------------------------
def test_proposal_message_contains_the_details():
    draft = ExpenseDraft(
        description="Instamart: Milk",
        amount=Decimal("1000.00"),
        paid_by="Ankit",
        split=Split(mode="equal", shares=[Share(participant="Ankit"), Share(participant="Yuvraj")]),
        date="2026-10-28",
        items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal("1000"))],
    )
    text = build_proposal_text(draft, "₹")
    assert "Instamart: Milk" in text
    assert "₹1,000.00" in text
    assert "Milk" in text
    assert "equal between Ankit, Yuvraj" in text


def test_approval_keyboard_offers_the_common_splits():
    draft = ExpenseDraft(
        description="Test",
        amount=Decimal("100"),
        paid_by="Ankit",
        split=Split(mode="equal", shares=[Share(participant="Ankit"), Share(participant="Yuvraj")]),
        date="2026-10-28",
    )
    draft.id = 7
    keyboard = build_approval_keyboard(draft, ["Ankit", "Yuvraj"])
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "approve:7:equal" in callbacks
    assert "approve:7:percent:70,30" in callbacks
    assert "approve:7:personal" in callbacks
    assert "custom:7" in callbacks
    assert "reject:7" in callbacks


def test_approval_keyboard_for_a_single_person_kitty():
    draft = ExpenseDraft(
        description="Test",
        amount=Decimal("100"),
        paid_by="Ankit",
        split=Split(mode="equal", shares=[Share(participant="Ankit")]),
        date="2026-10-28",
    )
    draft.id = 3
    keyboard = build_approval_keyboard(draft, ["Ankit"])
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "approve:3:equal" not in callbacks
    assert "approve:3:personal" in callbacks
    assert "reject:3" in callbacks
