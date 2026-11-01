"""The kittybot command line.

    kittybot setup            interactive setup
    kittybot run              start the bot and the dashboard
    kittybot connect <app>    connect a grocery app
    kittybot status           show what is configured
    kittybot pending          list proposals
    kittybot approve <id>     approve a proposal
    kittybot balance          who owes whom
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal, InvalidOperation

from .approvals import ApprovalEngine
from .config import CONFIG_DIR, CONFIG_FILE, Config, load_config
from .kittysplit import KittysplitClient, KittysplitError
from .splits import SplitParseError, parse_split
from .store import Store

log = logging.getLogger(__name__)


def _prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{text}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def _ask_yes_no(text: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    answer = _prompt(f"{text}{suffix}", "y" if default else "n").lower()
    return answer.startswith("y")


# ----------------------------------------------------------------------
# setup
# ----------------------------------------------------------------------
def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive setup."""
    config = load_config()
    print("kittybot setup\n==============\n")

    print("1. Telegram bot token")
    print("   Create a bot with @BotFather and copy the token.")
    token = _prompt("   Bot token", config.telegram.bot_token)
    if token:
        config.telegram.bot_token = token

    print("\n2. Your Kittysplit kitty")
    print("   Open your kitty in a browser and copy the address bar URL.")
    print("   It looks like https://www.kittysplit.com/k/AbCdEf-12")
    kitty_url = _prompt("   Kitty URL", config.kitty.kitty_url)
    if kitty_url:
        config.kitty.kitty_url = kitty_url

    if config.kitty.kitty_url:
        try:
            with KittysplitClient(config.kitty.kitty_url) as client:
                names = [p.name for p in client.participants()]
        except KittysplitError as exc:
            print(f"\n   Could not read that kitty: {exc}")
            names = []

        if names:
            print(f"\n   People in the kitty: {', '.join(names)}")
            my_name = _prompt("   Which one are you?", config.kitty.my_name or names[0])
            config.kitty.my_name = my_name
            config.telegram.default_payer = _prompt(
                "   Who usually pays for the grocery orders?",
                config.telegram.default_payer or my_name,
            )
            config.kitty.currency = _prompt("   Currency", config.kitty.currency)

    print("\n3. Grocery apps (optional, you can do this later)")
    for app in ("instamart", "zepto", "blinkit", "bigbasket"):
        if _ask_yes_no(f"   Connect {app} now?", default=False):
            _connect(app)

    path = config.save()
    print(f"\nSaved your settings to {path}")
    print("\nNext: kittybot run")
    return 0


# ----------------------------------------------------------------------
# connect
# ----------------------------------------------------------------------
def _connect(app: str) -> int:
    """Connect one marketplace."""
    if app in ("instamart", "swiggy", "swiggy-instamart"):
        from .sources.instamart import connect_interactive

        print("\nConnecting Swiggy Instamart (official MCP server)…")
        try:
            path = connect_interactive()
        except Exception as exc:
            print(f"   Failed: {exc}")
            return 1
        print(f"   Connected. Token saved to {path}")
        return 0

    if app == "zepto":
        from .sources.zepto import connect_interactive

        print("\nConnecting Zepto (official MCP server)…")
        try:
            path = connect_interactive()
        except Exception as exc:
            print(f"   Failed: {exc}")
            return 1
        print(f"   Connected. Token saved to {path}")
        return 0

    if app == "blinkit":
        return _connect_blinkit()

    if app == "bigbasket":
        return _connect_bigbasket()

    print(f"Unknown app: {app}")
    return 1


def _connect_blinkit() -> int:
    """Import a Blinkit session from a real browser login."""
    print(
        "\nConnecting Blinkit\n"
        "-------------------\n"
        "Blinkit has no public API, so kittybot reuses your browser session.\n"
        "This installs Playwright and opens a browser window.\n"
    )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run:\n")
        print("    pip install playwright && playwright install chromium\n")
        return 1

    from .sources.blinkit import BlinkitSource

    source = BlinkitSource()
    url = "https://blinkit.com/"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        print("A browser window is opening. Log in to Blinkit there, then come back.")
        page.goto(url, wait_until="domcontentloaded")
        input("\nPress Enter once you are logged in…")

        try:
            data = page.evaluate(
                """() => ({
                    auth: localStorage.getItem('auth'),
                    authKey: localStorage.getItem('authKey'),
                    deviceId: localStorage.getItem('deviceId'),
                    location: localStorage.getItem('location'),
                    user: localStorage.getItem('user'),
                    sessionId: sessionStorage.getItem('sessionId'),
                    useragent: localStorage.getItem('useragent'),
                })"""
            )
            cookie_list = context.cookies()
        finally:
            browser.close()

    if not data or not data.get("auth"):
        print("   Could not read the Blinkit session. Is the browser logged in?")
        return 1

    import json

    auth = json.loads(data["auth"])
    location = json.loads(data["location"]) if data.get("location") else {}
    coords = (location or {}).get("coords") or {}
    useragent = json.loads(data["useragent"]) if data.get("useragent") else {}

    session = {
        "access_token": auth.get("accessToken"),
        "auth_key": data.get("authKey") or "",
        "device_id": data.get("deviceId") or "",
        "session_uuid": data.get("sessionId") or "",
        "user_id": (json.loads(data["user"]) if data.get("user") else {}).get("id"),
        "lat": coords.get("lat"),
        "lon": coords.get("lon"),
        "locality": coords.get("locality"),
        # The gr_1_* cookies are sent alongside the access_token header:
        # some Blinkit endpoints validate the cookie, not just the header.
        "cookies": {
            c["name"]: c["value"]
            for c in cookie_list
            if c["name"].startswith("gr_1_") or c["name"].startswith("__cf")
        },
        "user_agent": useragent.get("uaString") or useragent.get("userAgent") or "",
        "app_version_v1": str(useragent.get("appVersion") or "") or None,
        "captured_at": __import__("datetime").datetime.now().isoformat(),
    }
    if not session["access_token"]:
        print("   No access token found. Was the browser logged in?")
        return 1

    source.save_session(session)
    print(f"   Connected. Session saved to {source.session_file} (mode 600)")
    print("   Note: logging out of Blinkit revokes this session.")
    _report_blinkit_session(session)
    return 0


def _connect_bigbasket() -> int:
    """Import a BigBasket session from a real browser login."""
    print(
        "\nConnecting BigBasket\n"
        "-------------------\n"
        "BigBasket has no public API, so kittybot reuses your browser session.\n"
        "BigBasket is OTP-only and its login page is behind a captcha, so\n"
        "you have to log in yourself. A browser window will open.\n"
    )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run:\n")
        print("    pip install playwright && playwright install chromium\n")
        return 1

    from .sources.bigbasket import BigBasketSource

    source = BigBasketSource()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        print("A browser window is opening.")
        page.goto("https://www.bigbasket.com/", wait_until="domcontentloaded")
        input(
            "Log in to BigBasket in that window (mobile or email + OTP), "
            "then press Enter here…"
        )
        try:
            cookies = context.cookies()
            member_id = None
            for cookie in cookies:
                if cookie["name"] == "_bb_mid":
                    member_id = cookie["value"]
            csrf = None
            for cookie in cookies:
                if cookie["name"] == "csurftoken":
                    csrf = cookie["value"]
            entry_context_id = None
            for cookie in cookies:
                if cookie["name"] == "xentrycontextid":
                    entry_context_id = cookie["value"]
            user_agent = page.evaluate("() => navigator.userAgent")
        finally:
            browser.close()

    if not member_id:
        print("   Could not find a BigBasket session. Were you logged in?")
        return 1

    session = {
        "cookies": {cookie["name"]: cookie["value"] for cookie in cookies},
        "member_id": member_id,
        "csrf_token": csrf,
        "entry_context_id": entry_context_id,
        "user_agent": user_agent,
        "captured_at": __import__("datetime").datetime.now().isoformat(),
    }
    source.save_session(session)
    print(f"   Connected. Session saved to {source.session_file} (mode 600)")
    _report_bigbasket_session(session)
    return 0


def _report_session_fields(
    fields: list[tuple[str, object, bool]],
    extra: tuple[str, object, bool],
    missing_hint: str,
    success_hint: str,
) -> None:
    """Print a capture report. ``sensitive`` fields are never printed.

    A credential must not end up in scrollback or a terminal recording, so
    for those only the ok/MISSING verdict is shown. The non-sensitive ones
    are truncated, because a mismatch is only debuggable if you can see the
    value that was captured.
    """
    print("   captured:")
    missing: list[str] = []
    for label, value, sensitive in fields:
        mark = "ok " if value else "MISSING"
        if sensitive:
            shown = "(not shown)"
        else:
            shown = (str(value)[:28] + "...") if value else "-"
        print(f"     [{mark:>7}] {label:<14} {shown}")
        if not value:
            missing.append(label)

    label, value, sensitive = extra
    mark = "ok " if value else "MISSING"
    shown = "(not shown)" if sensitive else str(value)
    print(f"     [{mark:>7}] {label:<14} {shown}")
    if not value:
        missing.append(label)

    if missing:
        print(
            f"   WARNING: missing {', '.join(missing)}.\n"
            f"   {missing_hint}"
        )
    else:
        print(f"   {success_hint}")


def _report_blinkit_session(session: dict) -> None:
    """Say what was captured and what is missing.

    Without this the import always says "Connected", and a missing field
    only shows up much later as a 401 with no clue which one it was.
    """
    cookies = session.get("cookies") or {}
    _report_session_fields(
        fields=[
            ("access token", session.get("access_token"), True),
            ("auth key", session.get("auth_key"), True),
            ("device id", session.get("device_id"), False),
            ("session uuid", session.get("session_uuid"), False),
            ("user agent", session.get("user_agent"), False),
            ("app version", session.get("app_version_v1"), False),
            ("latitude", session.get("lat"), False),
            ("longitude", session.get("lon"), False),
        ],
        extra=("gr_1 cookies", len(cookies), False),
        missing_hint=(
            "Blinkit will probably return 401. Log out of Blinkit in that "
            "browser window, log in again, and retry."
        ),
        success_hint="Everything Blinkit needs is present.",
    )


def _report_bigbasket_session(session: dict) -> None:
    """Same idea for BigBasket."""
    cookies = session.get("cookies") or {}
    _report_session_fields(
        fields=[
            ("member id", session.get("member_id"), False),
            ("csrf token", session.get("csrf_token"), True),
            ("entry context", session.get("entry_context_id"), False),
            ("user agent", session.get("user_agent"), False),
        ],
        extra=("cookies", len(cookies), False),
        missing_hint=(
            "BigBasket will probably return 401. Log in again in that "
            "browser window and retry."
        ),
        success_hint="Everything BigBasket needs is present.",
    )


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    """Start the bot (and the dashboard)."""
    config = load_config()
    if not config.kitty.kitty_url:
        print("No kitty configured. Run: kittybot setup", file=sys.stderr)
        return 1
    if not config.telegram.bot_token:
        print("No Telegram bot token. Run: kittybot setup", file=sys.stderr)
        return 1

    from .bot.telegram import run_bot

    client = KittysplitClient(config.kitty.kitty_url)
    client.ensure_identity(config.kitty.my_name or None)
    store = Store(config.db_path or None)
    engine = ApprovalEngine(client, store)

    async def _run() -> None:
        tasks = []
        if not args.no_dashboard:
            tasks.append(asyncio.create_task(_serve_dashboard(config, engine, store)))
        tasks.append(asyncio.create_task(run_bot(config, engine, store)))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        store.close()
        client.close()
    return 0


async def _serve_dashboard(config: Config, engine: ApprovalEngine, store: Store) -> None:
    """Serve the dashboard in the background."""
    import uvicorn

    from .dashboard.app import create_app

    app = create_app(config, engine, store)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.dashboard.host,
            port=config.dashboard.port,
            log_level="warning",
        )
    )
    await server.serve()


# ----------------------------------------------------------------------
# status / one-shots
# ----------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    """Show what is configured and what is connected."""
    config = load_config()
    print("kittybot status\n===============\n")
    print(f"config file : {CONFIG_FILE}")
    print(f"kitty url   : {config.kitty.kitty_url or '(not set)'}")
    print(f"my name     : {config.kitty.my_name or '(not set)'}")
    print(f"telegram    : {'configured' if config.telegram.bot_token else '(not set)'}")
    print(f"database    : {config.db_path or '(default)'}")
    print(f"sources     : {', '.join(config.enabled_sources) or '(all available)'}")

    if config.kitty.kitty_url:
        try:
            with KittysplitClient(config.kitty.kitty_url) as client:
                names = [p.name for p in client.participants()]
            print(f"participants : {', '.join(names)}")
        except KittysplitError as exc:
            print(f"participants : could not read the kitty ({exc})")

    print("\nconnected apps:")
    from .sources import build_sources

    for source in build_sources(config):
        connected = asyncio.run(source.check_connection())
        print(f"  {source.label:20s} {'yes' if connected else 'no'}")

    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config.db_path or None)
    pending = store.pending_proposals()
    if not pending:
        print("Nothing pending.")
        return 0
    for draft in pending:
        print(f"#{draft.id} {draft.description} — Rs{draft.amount} ({draft.split_summary()})")
    store.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.kitty.kitty_url:
        print("No kitty configured.", file=sys.stderr)
        return 1

    client = KittysplitClient(config.kitty.kitty_url)
    client.ensure_identity(config.kitty.my_name or None)
    store = Store(config.db_path or None)
    engine = ApprovalEngine(client, store)

    try:
        proposal_id = int(args.id)
    except ValueError:
        print("The id must be a number.", file=sys.stderr)
        return 1

    try:
        if args.split:
            draft = engine.approve_with_spec(proposal_id, args.split)
        else:
            draft = engine.approve(proposal_id)
    except SplitParseError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Could not approve: {exc}", file=sys.stderr)
        return 1

    print(f"Proposal #{proposal_id}: {draft.status.value}")
    if draft.error:
        print(f"  error: {draft.error}")
    if draft.kitty_url:
        print(f"  {draft.kitty_url}")
    store.close()
    client.close()
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.kitty.kitty_url:
        print("No kitty configured.", file=sys.stderr)
        return 1
    with KittysplitClient(config.kitty.kitty_url) as client:
        client.ensure_identity(config.kitty.my_name or None)
        summary = client.summary()
    symbol = summary.currency_symbol or ""
    print(f"{summary.kitty_name}")
    print(f"  group total  : {symbol}{summary.total_cost}")
    print(f"  cost to you  : {symbol}{summary.your_cost}")
    print(f"  you paid     : {symbol}{summary.you_paid}")
    print(f"  you are owed : {symbol}{summary.you_are_owed}")
    if summary.settlements:
        print("\nsettle up:")
        for payer, recipient, amount in summary.settlements:
            print(f"  {payer} -> {recipient}: {symbol}{amount}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Add an expense from the command line."""
    config = load_config()
    if not config.kitty.kitty_url:
        print("No kitty configured.", file=sys.stderr)
        return 1

    try:
        amount = Decimal(args.amount.replace(",", "").replace("₹", ""))
    except InvalidOperation:
        print("Could not read the amount.", file=sys.stderr)
        return 1

    client = KittysplitClient(config.kitty.kitty_url)
    client.ensure_identity(config.kitty.my_name or None)
    store = Store(config.db_path or None)
    engine = ApprovalEngine(client, store)

    participants = engine.participant_names()
    payer = config.telegram.default_payer or config.kitty.my_name or participants[0]
    description = " ".join(args.description)
    try:
        split = parse_split(args.split or "", participants, total=amount)
    except SplitParseError as exc:
        print(exc, file=sys.stderr)
        return 1

    draft = engine.propose_manual(
        amount, description, paid_by=payer, split=split
    )
    approved = engine.approve(draft.id)
    print(f"{approved.status.value}: {description} — Rs{amount} ({approved.split_summary()})")
    if approved.kitty_url:
        print(approved.kitty_url)
    store.close()
    client.close()
    return 0


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kittybot", description="Split expenses from Telegram.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="interactive setup").set_defaults(func=cmd_setup)

    run_parser = sub.add_parser("run", help="start the bot and the dashboard")
    run_parser.add_argument("--no-dashboard", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    connect = sub.add_parser("connect", help="connect a grocery app")
    connect.add_argument("app", choices=["instamart", "swiggy", "zepto", "blinkit", "bigbasket"])
    connect.set_defaults(func=lambda a: _connect(a.app))

    sub.add_parser("status", help="show what is configured").set_defaults(func=cmd_status)
    sub.add_parser("pending", help="list proposals").set_defaults(func=cmd_pending)
    sub.add_parser("balance", help="who owes whom").set_defaults(func=cmd_balance)

    approve = sub.add_parser("approve", help="approve a proposal")
    approve.add_argument("id")
    approve.add_argument("split", nargs="?", default="")
    approve.set_defaults(func=cmd_approve)

    add = sub.add_parser("add", help="add an expense")
    add.add_argument("amount")
    add.add_argument("description", nargs="+")
    add.add_argument("split", nargs="?", default="")
    add.set_defaults(func=cmd_add)

    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
