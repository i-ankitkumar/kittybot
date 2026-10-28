# kittybot

Split household expenses from Telegram. Pull grocery orders from Swiggy Instamart, Zepto, Blinkit and BigBasket, review every split before it lands, and write it to [Kittysplit](https://kittysplit.com).

No more opening five tabs to tell your flatmate what you paid for.

<!-- Add a hero GIF here once the bot is running. -->

## What it does

- **Add an expense in Telegram.** `/add 450 zepto groceries 60/40` lands in your kitty in seconds.
- **Import orders from grocery apps.** `/sync` pulls recent orders from Instamart, Zepto, Blinkit and BigBasket in read-only mode.
- **Every split goes through you.** An imported order becomes a proposal. You approve it, adjust it, or reject it. Nothing is written to the kitty without a human yes.
- **Any split you like.** Equal, weighted, percentage, fixed amounts, or entirely personal. The bot never guesses which one you meant.
- **A dashboard.** Balances, settlement plan, spend by category, and every proposal waiting on you.

## Why an approval step

This is the whole point of the tool. A ₹1,200 Instamart order might be entirely yours, entirely your flatmate's, or split 70/30. No rule can tell those apart, so the bot asks.

```
🛒 Instamart · ₹1,248.00 · 28 Oct
   Amul Butter 500g ×2 · ₹285
   Potatoes 2kg ×1 · · ₹120
   Onions 3kg ×1 · · · ₹74
   ...
[Split equally] [All mine] [Split 70/30] [Reject]
```

Tap a button, or send a custom split like `/split 800 400` or `/split 60/30`.

## Install

```bash
git clone https://github.com/iankitkumar/kittybot
cd kittybot
uv venv && uv pip install -e ".[dev]"
```

or with plain pip:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Setup

```bash
kittybot setup
```

The wizard asks for:

| Setting | What it is |
|---|---|
| **Telegram bot token** | From [@BotFather](https://t.me/BotFather). |
| **KittySplit URL** | The secret link from your kitty, e.g. `https://www.kittysplit.com/k/AbCdEf-12`. This is the only credential Kittysplit needs. |
| **Your name in the kitty** | So the bot can tell which participant you are. |
| **Marketplace logins** | Optional. See below. |

Then:

```bash
kittybot run
```

The bot talks to Telegram over long polling, so no public URL or webhook tunnel is needed. A dashboard is served at `http://localhost:8080`.

## Connecting the grocery apps

All four are **read-only**. The tool never places, cancels or modifies an order.

| App | How it connects |
|---|---|
| Swiggy Instamart | Official Swiggy MCP server, OAuth. `kittybot connect instamart` |
| Zepto | Official Zepto MCP server, OAuth. `kittybot connect zepto` |
| Blinkit | Session import from your browser. `kittybot connect blinkit` |
| BigBasket | Session import from your browser. `kittybot connect bigbasket` |

Session imports use a local browser window: you log in yourself, the bot saves the session cookie to `~/.config/kittybot/credentials.json` (mode 600) and reuses it. Your password never touches the bot.

## Commands

| Command | Does |
|---|---|
| `/add <amount> <description> [split]` | Record an expense. Split can be `50/50`, `70/30`, `all`, `personal`, or omitted (you get asked). |
| `/sync [app]` | Pull recent orders and propose them. |
| `/pending` | List proposals waiting on you. |
| `/approve <id> [split]` | Approve a proposal, optionally with a custom split. |
| `/reject <id>` | Reject a proposal. |
| `/balance` | Who owes whom. |
| `/export` | The kitty as a spreadsheet. |

## How the split maths works

Kittysplit supports four split modes and kittybot maps straight onto them:

| Mode | Example | Notes |
|---|---|---|
| `equal` | `50/50` | Everyone involved splits evenly. |
| `weight` | `70/30` | Proportional shares. Weights are normalised. |
| `percent` | `60/40` | Must total 100. |
| `amount` | `800 400` | Fixed rupee amounts. Must total the expense. |

A personal expense (`/add 300 headphones personal`) records one participant as owing 100%. This is a first-class case, not an edge case: half your grocery orders are yours alone.

## Architecture

```
kittybot/
├── kittysplit.py     Kittysplit client: read, write, split modes, XLSX export
├── models.py         Split, Expense, ExpenseDraft, MarketOrder, approvals
├── sources/          Read-only marketplace adapters
│   ├── base.py       OrderSource protocol + normalisation
│   ├── instamart.py  Swiggy Instamart (official MCP, OAuth)
│   ├── zepto.py      Zepto (official MCP, OAuth)
│   ├── blinkit.py    Blinkit (session import)
│   └── bigbasket.py  BigBasket (session import)
├── approvals.py      Proposal lifecycle, dedupe, approval gate
├── bot/              Telegram bot: commands, inline keyboards, approval flow
├── dashboard/        FastAPI dashboard
└── store.py          SQLite persistence
```

Kittysplit has no public REST API, so `kittysplit.py` speaks its actual protocol: form posts against the Phoenix endpoints, with the `kittysplit_viewing_parties` identity cookie. Every endpoint and all four split modes were verified against a live kitty.

## Privacy

- Credentials live in `~/.config/kittybot/credentials.json`, mode 600, never in the repo.
- Marketplace access is read-only.
- Nothing is written to your kitty without an explicit approval from you.

## License

MIT
