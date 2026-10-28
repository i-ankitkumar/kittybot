"""Zepto order history via Zepto's official MCP server.

Zepto runs an MCP server at ``https://mcp.zepto.co.in/mcp`` with an OAuth
server at ``https://auth.zepto.co.in``. It supports Dynamic Client
Registration and PKCE, so no app registration is needed.

Scopes are deliberately restricted to ``tools:read`` - we never request the
cart, checkout or order-management scopes, so the token physically cannot
place an order.

Docs: https://github.com/zeptonow/mcp
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..mcp import (
    McpClient,
    McpError,
    build_authorize_url,
    exchange_code,
    load_token,
    pkce_pair,
    refresh_token,
    register_client,
    run_local_callback,
    save_token,
)
from ..models import LineItem, MarketOrder
from .base import OrderSource, SourceError, build_order, normalise_price

MCP_ENDPOINT = "https://mcp.zepto.co.in/mcp"
AUTH_BASE = "https://auth.zepto.co.in"
TOKEN_FILE = Path.home() / ".config" / "kittybot" / "zepto_token.json"
SCOPE = "tools:read"

# Tool names vary slightly between Zepto MCP revisions; try each in turn.
HISTORY_TOOLS = ("list_order_history", "get_order_history", "orders_history")
DETAIL_TOOLS = ("get_order_detail", "get_order_details", "order_detail")


class ZeptoSource(OrderSource):
    """Read-only Zepto order history."""

    name = "zepto"
    label = "Zepto"
    requires_auth = True

    def __init__(self, token_file: Path | None = None) -> None:
        self.token_file = token_file or TOKEN_FILE
        self.client = McpClient(MCP_ENDPOINT)

    # ------------------------------------------------------------------
    # tokens
    # ------------------------------------------------------------------
    def _access_token(self) -> str:
        token = load_token(self.token_file)
        if not token:
            raise SourceError("Zepto is not connected. Run: kittybot connect zepto")
        expires_at = float(token.get("expires_at", 0))
        if expires_at and expires_at < time.time() - 60:
            token = self._refresh(token)
        access = token.get("access_token")
        if not access:
            raise SourceError("the Zepto token file has no access token; reconnect")
        return access

    def _refresh(self, token: dict) -> dict:
        refresh = token.get("refresh_token")
        client_id = token.get("client_id")
        if not refresh or not client_id:
            raise SourceError("the Zepto token expired and cannot be refreshed; reconnect")
        data = refresh_token(
            f"{AUTH_BASE}/token",
            client_id=client_id,
            refresh=refresh,
            scope=SCOPE,
        )
        data["client_id"] = client_id
        save_token(self.token_file, data)
        return data

    async def check_connection(self) -> bool:
        try:
            self._access_token()
        except SourceError:
            return False
        return True

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def _call_first(self, names: tuple[str, ...], arguments: dict[str, Any]) -> dict:
        access = self._access_token()
        last_error = ""
        for name in names:
            try:
                return self.client.call_tool(access, name, arguments)
            except McpError as exc:
                last_error = str(exc)
                if "not found" not in last_error.lower() and "unknown tool" not in last_error.lower():
                    raise SourceError(last_error) from exc
        raise SourceError(f"no usable Zepto tool found (tried {', '.join(names)}): {last_error}")

    async def fetch_orders(self, since: datetime | None = None) -> list[MarketOrder]:
        data = self._call_first(HISTORY_TOOLS, {})
        raw_orders = (
            data.get("orders")
            or data.get("order_history")
            or data.get("data")
            or []
        )
        if isinstance(raw_orders, dict):
            raw_orders = raw_orders.get("orders", [])

        orders: list[MarketOrder] = []
        for raw in raw_orders:
            order = _parse_order(raw)
            if order:
                orders.append(order)
        return orders

    async def fetch_order_details(self, order_id: str) -> MarketOrder | None:
        data = self._call_first(DETAIL_TOOLS, {"order_id": order_id})
        order = _parse_order(data)
        if order:
            order.order_id = order_id
        return order


def _parse_order(raw: dict) -> MarketOrder | None:
    """Normalise one Zepto order record."""
    if not isinstance(raw, dict):
        return None
    order_id = (
        raw.get("order_id")
        or raw.get("orderId")
        or raw.get("id")
        or raw.get("order_no")
        or raw.get("cart_id")
    )
    if not order_id:
        return None

    items: list[LineItem] = []
    for entry in raw.get("items") or raw.get("products") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("product_name") or entry.get("title")
        if not name:
            continue
        quantity = Decimal(str(entry.get("quantity") or entry.get("qty") or 1))
        price = normalise_price(
            entry.get("price")
            if entry.get("price") is not None
            else entry.get("selling_price") or entry.get("mrp")
        )
        items.append(LineItem(name=str(name), quantity=quantity, price=price))

    total = normalise_price(
        raw.get("total")
        if raw.get("total") is not None
        else raw.get("payable_amount") or raw.get("grand_total") or raw.get("amount")
    )
    placed = (
        raw.get("placed_at")
        or raw.get("order_date")
        or raw.get("created_at")
        or raw.get("ordered_at")
    )
    return build_order(
        source="zepto",
        order_id=str(order_id),
        total=total,
        items=items,
        placed_at=str(placed) if placed else None,
        raw=raw,
    )


def connect_interactive(token_file: Path | None = None) -> Path:
    """Run the OAuth flow and save the token."""
    token_file = token_file or TOKEN_FILE
    registration = register_client(f"{AUTH_BASE}/register", scope=SCOPE)
    client_id = registration["client_id"]
    verifier, challenge = pkce_pair()
    state = pkce_pair()[0][:24]

    url = build_authorize_url(
        f"{AUTH_BASE}/authorize",
        client_id=client_id,
        code_challenge=challenge,
        state=state,
        scope=SCOPE,
    )
    code = run_local_callback(url, expected_state=state)
    token = exchange_code(
        f"{AUTH_BASE}/token",
        client_id=client_id,
        code=code,
        code_verifier=verifier,
    )
    token["client_id"] = client_id
    save_token(token_file, token)
    return token_file
