"""The Telegram bot.

Commands:

    /add <amount> <description> [split]   record an expense
    /sync [app]                           pull recent orders and propose them
    /pending                              list proposals waiting on you
    /approve <id> [split]                 approve a proposal
    /reject <id>                          reject a proposal
    /balance                              who owes whom
    /export                               the kitty as a spreadsheet
    /help                                 this list

The approval flow is inline: every proposal arrives with buttons for the
common splits plus "Reject", and a custom split can be sent as text.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.types import CallbackQuery

from ..approvals import ApprovalEngine, describe_order
from ..config import Config
from ..kittysplit import CATEGORY_FOOD, KittysplitClient
from ..models import ApprovalStatus, ExpenseDraft, Split
from ..sources import build_sources
from ..sources.base import SourceError
from ..splits import SplitParseError, parse_split
from ..store import Store

log = logging.getLogger(__name__)

HELP_TEXT = """\
<b>kittybot</b> - split expenses without opening the app.

/add <amount> <description> [split]
  Record an expense. Split examples: <code>50/50</code>, <code>70/30</code>,
  <code>60% 40%</code>, <code>800 400</code>, <code>me</code>, <code>personal</code>.
  Leave the split off and you will be asked.

/sync [app]
  Pull recent orders from your grocery apps and propose them.
  Apps: instamart, zepto, blinkit, bigbasket (or none for all).

/pending
  Proposals waiting for your decision.

/approve &lt;id&gt; [split]
  Approve a proposal, optionally with a different split.

/reject &lt;id&gt;
  Reject a proposal. Nothing is written to the kitty.

/balance
  Who owes whom, and how to settle up.

/export
  The kitty as a spreadsheet.

Nothing reaches your kitty without your approval.
"""


class WaitingForSplit(StatesGroup):
    """Waiting for the user to type a split."""

    proposal_id: int


def _money(value: Decimal, symbol: str = "₹") -> str:
    return f"{symbol}{value:,.2f}"


def build_approval_keyboard(
    draft: ExpenseDraft, participants: list[str]
) -> InlineKeyboardMarkup:
    """Buttons for a proposal: common splits, custom, reject."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    if len(participants) > 1:
        row.append(
            InlineKeyboardButton(
                text=f"Split equally ({len(participants)})",
                callback_data=f"approve:{draft.id}:equal",
            )
        )
        row.append(
            InlineKeyboardButton(
                text="70 / 30",
                callback_data=f"approve:{draft.id}:percent:70,30",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []

    row.append(
        InlineKeyboardButton(
            text="All mine",
            callback_data=f"approve:{draft.id}:personal",
        )
    )
    row.append(
        InlineKeyboardButton(
            text="Custom…",
            callback_data=f"custom:{draft.id}",
        )
    )
    if row:
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Reject",
                callback_data=f"reject:{draft.id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_proposal_text(draft: ExpenseDraft, symbol: str = "₹") -> str:
    """The message body for a proposal."""
    lines = [
        f"<b>{_escape(draft.description)}</b>",
        f"Amount: <b>{_money(draft.amount, symbol)}</b>",
    ]
    if draft.items:
        lines.append("")
        for item in draft.items[:8]:
            price = f"{symbol}{item.price:.2f}" if item.price is not None else "?"
            lines.append(f"· {item.name} ×{item.quantity:g} — {price}")
        if len(draft.items) > 8:
            lines.append(f"· … and {len(draft.items) - 8} more")
    lines.append("")
    lines.append(f"Proposed split: <i>{_escape(draft.split_summary())}</i>")
    lines.append("Tap a button, or send a custom split like <code>60/40</code>.")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class KittyBot:
    """Wires the Telegram bot to the approval engine."""

    def __init__(self, config: Config, engine: ApprovalEngine, store: Store) -> None:
        self.config = config
        self.engine = engine
        self.store = store
        self.router = Router()
        self._register()

    # ------------------------------------------------------------------
    def _register(self) -> None:
        router = self.router
        router.message.register(self.cmd_start, CommandStart())
        router.message.register(self.cmd_help, Command("help"))
        router.message.register(self.cmd_add, Command("add"))
        router.message.register(self.cmd_sync, Command("sync"))
        router.message.register(self.cmd_pending, Command("pending"))
        router.message.register(self.cmd_approve, Command("approve"))
        router.message.register(self.cmd_reject, Command("reject"))
        router.message.register(self.cmd_balance, Command("balance"))
        router.message.register(self.cmd_export, Command("export"))
        router.message.register(self.on_text, State(WaitingForSplit))
        router.callback_query.register(self.on_callback)
        router.message.register(self.on_unknown)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _symbol(self) -> str:
        return "₹" if self.config.kitty.currency.upper() == "INR" else ""

    def _my_name(self) -> str:
        if self.config.telegram.default_payer:
            return self.config.telegram.default_payer
        if self.config.kitty.my_name:
            return self.config.kitty.my_name
        names = self.engine.participant_names()
        return names[0] if names else ""

    def _participants(self) -> list[str]:
        try:
            return self.engine.participant_names()
        except Exception:
            return []

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def cmd_start(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Hi. I keep your shared expenses tidy.\n\n"
            "Try <code>/add 450 zepto groceries 50/50</code>\n"
            "or <code>/sync</code> to pull in your grocery orders.\n\n"
            "Use /help for everything.",
        )

    async def cmd_help(self, message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="HTML")

    async def on_unknown(self, message: Message) -> None:
        await message.answer(
            "I did not catch that. Send /help to see what I understand."
        )

    async def cmd_add(self, message: Message) -> None:
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer(
                "Usage: <code>/add &lt;amount&gt; &lt;description&gt; [split]</code>\n"
                "Example: <code>/add 450 groceries 50/50</code>"
            )
            return

        raw_amount = args[1].replace("₹", "").replace(",", "").strip()
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            await message.answer(f"Could not read <code>{args[1]}</code> as an amount.")
            return

        remainder = args[2] if len(args) > 2 else ""
        description, split_spec = _split_description(remainder)
        participants = self._participants()
        payer = self._my_name()

        if not participants:
            await message.answer(
                "I cannot reach your kitty right now. Check the kitty link in the config."
            )
            return

        try:
            split = parse_split(split_spec, participants, total=amount)
        except SplitParseError as exc:
            await message.answer(f"{exc}")
            return

        draft = self.engine.propose_manual(
            amount,
            description,
            paid_by=payer,
            split=split,
            category_id=CATEGORY_FOOD,
        )
        await self._send_proposal(message, draft)

    async def cmd_sync(self, message: Message) -> None:
        wanted = None
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            wanted = parts[1].strip().lower()

        sources = build_sources(self.config)
        if wanted:
            sources = [s for s in sources if s.name == wanted]
            if not sources:
                await message.answer(
                    f"I do not know the app <code>{wanted}</code>. "
                    f"Try: {', '.join(s.name for s in build_sources(self.config))}"
                )
                return

        if not sources:
            await message.answer(
                "No grocery apps are connected yet.\n"
                "Run <code>kittybot connect instamart</code> (or zepto, blinkit, bigbasket)."
            )
            return

        status = await message.answer(f"Checking {len(sources)} app(s)…")
        payer = self._my_name()
        created = 0
        failures: list[str] = []

        for source in sources:
            try:
                orders = await source.fetch_orders()
            except SourceError as exc:
                failures.append(f"{source.label}: {exc}")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                failures.append(f"{source.label}: {exc}")
                continue

            for order in orders:
                if order.total <= 0:
                    continue
                try:
                    _, is_new = self.engine.propose_order(order, payer=payer)
                except Exception as exc:
                    failures.append(f"{source.label}: {exc}")
                    continue
                if is_new:
                    created += 1
                    draft = self.store.get_by_dedupe_key(order.dedupe_key)
                    if draft:
                        await self._send_proposal(message, draft)

        summary = f"Pulled orders from {len(sources) - len(failures)} app(s). {created} new proposal(s)."
        if failures:
            summary += "\n\n" + "\n".join(f"⚠️ {f}" for f in failures)
        await status.edit_text(summary)

    async def cmd_pending(self, message: Message) -> None:
        pending = self.engine.pending()
        if not pending:
            await message.answer("Nothing waiting. All clear.")
            return
        lines = [f"<b>{len(pending)} proposal(s) waiting</b>", ""]
        for draft in pending:
            lines.append(
                f"<code>#{draft.id}</code> {_escape(draft.description)} — "
                f"{_money(draft.amount, self._symbol())} ({_escape(draft.split_summary())})"
            )
        lines.append("")
        lines.append("Use <code>/approve &lt;id&gt;</code> or <code>/reject &lt;id&gt;</code>.")
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_approve(self, message: Message) -> None:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2:
            await message.answer("Usage: <code>/approve &lt;id&gt; [split]</code>")
            return
        try:
            proposal_id = int(parts[1])
        except ValueError:
            await message.answer("That does not look like a proposal id.")
            return

        draft = self.store.get_proposal(proposal_id)
        if draft is None:
            await message.answer(f"No proposal #{proposal_id}.")
            return

        if len(parts) > 2 and parts[2].strip():
            try:
                self.engine.approve_with_spec(proposal_id, parts[2].strip())
            except SplitParseError as exc:
                await message.answer(f"{exc}")
                return
            except Exception as exc:
                await message.answer(f"Could not approve it: {exc}")
                return
        else:
            self.engine.approve(proposal_id)

        updated = self.store.get_proposal(proposal_id)
        await message.answer(_approval_result(updated))

    async def cmd_reject(self, message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: <code>/reject &lt;id&gt;</code>")
            return
        try:
            proposal_id = int(parts[1])
        except ValueError:
            await message.answer("That does not look like a proposal id.")
            return
        self.engine.reject(proposal_id)
        await message.answer(f"Rejected #{proposal_id}. Nothing was added to the kitty.")

    async def cmd_balance(self, message: Message) -> None:
        try:
            summary = self.engine.client.summary()
        except Exception as exc:
            await message.answer(f"Could not read the kitty: {exc}")
            return

        symbol = summary.currency_symbol or self._symbol()
        lines = [
            f"<b>{_escape(summary.kitty_name)}</b>",
            "",
            f"Total for the group: {_money(summary.total_cost, symbol)}",
            f"Cost to you: {_money(summary.your_cost, symbol)}",
            f"You paid: {_money(summary.you_paid, symbol)}",
            f"You are owed: {_money(summary.you_are_owed, symbol)}",
        ]
        if summary.settlements:
            lines.append("")
            lines.append("<b>How to settle up</b>")
            for payer, recipient, amount in summary.settlements:
                lines.append(f"{_escape(payer)} → {_escape(recipient)}: {_money(amount, symbol)}")
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_export(self, message: Message) -> None:
        try:
            data = self.engine.client.export_xlsx()
        except Exception as exc:
            await message.answer(f"Could not export the kitty: {exc}")
            return
        await message.answer_document(
            document=("kitty.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            caption="Your kitty as a spreadsheet.",
        )

    # ------------------------------------------------------------------
    # inline flow
    # ------------------------------------------------------------------
    async def _send_proposal(self, message: Message, draft: ExpenseDraft) -> None:
        keyboard = build_approval_keyboard(draft, self._participants())
        await message.answer(
            build_proposal_text(draft, self._symbol()),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def on_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        data = callback.data or ""
        parts = data.split(":")
        if not parts:
            await callback.answer()
            return

        action = parts[0]
        try:
            proposal_id = int(parts[1])
        except (IndexError, ValueError):
            await callback.answer()
            return

        draft = self.store.get_proposal(proposal_id)
        if draft is None:
            await callback.answer("That proposal is gone.", show_alert=True)
            return

        if action == "reject":
            self.engine.reject(proposal_id)
            await callback.message.edit_text(
                f"Rejected #{proposal_id}. Nothing was added to the kitty."
            )
            await callback.answer()
            return

        if action == "custom":
            await state.set_state(WaitingForSplit)
            await state.update_data(proposal_id=proposal_id)
            await callback.message.answer(
                "Send the split for this expense.\n"
                "Examples: <code>50/50</code>, <code>70/30</code>, <code>60% 40%</code>, "
                "<code>800 400</code>, <code>me</code>."
            )
            await callback.answer()
            return

        if action == "approve":
            split = await self._split_for_callback(parts, draft)
            if split is None:
                await callback.answer("Could not work out that split.", show_alert=True)
                return
            self.engine.approve(proposal_id, split)
            updated = self.store.get_proposal(proposal_id)
            await callback.message.edit_text(_approval_result(updated))
            await callback.answer()

    async def _split_for_callback(
        self, parts: list[str], draft: ExpenseDraft
    ) -> Split | None:
        participants = self._participants()
        if len(parts) < 3:
            return draft.split
        mode = parts[2]
        if mode == "equal":
            return Split(mode="equal", shares=[_share(p) for p in participants])
        if mode == "personal":
            return Split(mode="equal", shares=[_share(self._my_name())])
        if mode == "percent" and len(parts) > 3:
            try:
                values = [Decimal(v) for v in parts[3].split(",")]
            except InvalidOperation:
                return None
            names = participants[: len(values)]
            if len(names) < len(values):
                return None
            from decimal import Decimal as D

            return Split(
                mode="percent",
                shares=[
                    _share(name, percent=value) for name, value in zip(names, values, strict=False)
                ],
            )
        return None

    async def on_text(self, message: Message, state: FSMContext) -> None:
        """Handle a custom split typed after tapping "Custom…"."""
        data = await state.get_data()
        proposal_id = int(data.get("proposal_id", 0))
        draft = self.store.get_proposal(proposal_id)
        if draft is None:
            await state.clear()
            await message.answer("That proposal is gone.")
            return

        try:
            split = parse_split(message.text, self._participants(), total=draft.amount)
        except SplitParseError as exc:
            await message.answer(f"{exc}")
            return

        self.engine.approve(proposal_id, split)
        await state.clear()
        updated = self.store.get_proposal(proposal_id)
        await message.answer(_approval_result(updated))


def _share(participant: str, percent: Decimal | None = None):
    from ..models import Share

    return Share(participant=participant, percent=percent)


def _split_description(remainder: str) -> tuple[str, str]:
    """Pull a trailing split spec out of a description.

    ``/add 450 groceries 50/50`` -> ("groceries", "50/50")
    ``/add 450 groceries``       -> ("groceries", "")
    """
    remainder = (remainder or "").strip()
    if not remainder:
        return "", ""

    tokens = remainder.split()
    # Try progressively longer trailing slices as a split spec.
    for take in (3, 2, 1):
        if len(tokens) < take + 1:
            continue
        candidate = " ".join(tokens[-take:])
        try:
            parse_split(candidate, ["a", "b"], total=Decimal(100))
        except SplitParseError:
            continue
        except Exception:
            continue
        description = " ".join(tokens[:-take]).strip()
        if description:
            return description, candidate
    return remainder, ""


def _approval_result(draft: ExpenseDraft | None) -> str:
    if draft is None:
        return "Something went wrong."
    if draft.status is ApprovalStatus.FAILED:
        return f"Could not write it to the kitty: {draft.error}"
    if draft.status is ApprovalStatus.APPROVED:
        text = (
            f"Added to the kitty: <b>{_escape(draft.description)}</b> — "
            f"{_money(draft.amount)} ({_escape(draft.split_summary())})"
        )
        if draft.kitty_url:
            text += f"\n\n{_escape(draft.kitty_url)}"
        return text
    return f"Proposal #{draft.id} is {draft.status.value}."


async def run_bot(config: Config, engine: ApprovalEngine, store: Store) -> None:
    """Start the bot with long polling (no public URL needed)."""
    bot = Bot(token=config.telegram.bot_token)
    dispatcher = Dispatcher()
    kitty_bot = KittyBot(config, engine, store)
    dispatcher.include_router(kitty_bot.router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("kittybot is up, polling for updates")
    await dispatcher.start_polling(bot)


def main() -> None:  # pragma: no cover - entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from ..cli import run

    asyncio.run(run())
