"""Swiggy Instamart order history via Swiggy's official MCP server.

Swiggy publishes a first-party MCP server with read-only Instamart tools:

* ``get_orders``        - the last 15 days of orders with items and totals
* ``get_order_details`` - one order's line items with prices and the bill

Authentication is OAuth 2.1 with PKCE and Dynamic Client Registration, so
there is nothing to register by hand: the client registers itself, you
approve in a browser, and we get a bearer token.

Docs: https://mcp.swiggy.com/builders/docs/reference/instamart/get_orders
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

MCP_ENDPOINT = "https://mcp.swiggy.com/im"
AUTH_BASE = "https://mcp.swiggy.com/auth"
TOKEN_FILE = Path.home() / ".config" / "kittybot" / "swiggy_token.json"
SCOPE = "mcp:tools"


class InstamartSource(OrderSource):
    """Read-only Instamart order history."""

    name = "instamart"
    label = "Swiggy Instamart"
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
            raise SourceError("Swiggy is not connected. Run: kittybot connect instamart")
        expires_at = float(token.get("expires_at", 0))
        if expires_at and expires_at < time.time() - 60:
            token = self._refresh(token)
        access = token.get("access_token")
        if not access:
            raise SourceError("the Swiggy token file has no access token; reconnect")
        return access

    def _refresh(self, token: dict) -> dict:
        refresh = token.get("refresh_token")
        client_id = token.get("client_id")
        if not refresh or not client_id:
            raise SourceError("the Swiggy token expired and cannot be refreshed; reconnect")
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
    def _call(self, tool: str, arguments: dict[str, Any]) -> dict:
        try:
            return self.client.call_tool(self._access_token(), tool, arguments)
        except McpError as exc:
            raise SourceError(str(exc)) from exc

    async def fetch_orders(self, since: datetime | None = None) -> list[MarketOrder]:
        data = self._call("get_orders", {"count": 20, "orderType": "INSTAMART"})
        orders: list[MarketOrder] = []
        for raw in data.get("orders", []) or []:
            items = [
                LineItem(
                    name=str(item.get("name", "item")),
                    quantity=Decimal(str(item.get("quantity", 1) or 1)),
                    price=normalise_price(item.get("price")),
                )
                for item in raw.get("items", []) or []
            ]
            total = normalise_price(raw.get("totalAmount"))
            if total is None:
                bill = raw.get("billDetails") or {}
                total = normalise_price(bill.get("grandTotal"))
            orders.append(
                build_order(
                    source=self.name,
                    order_id=raw.get("orderId", ""),
                    total=total,
                    items=items,
                    placed_at=raw.get("createdAt"),
                    raw=raw,
                )
            )
        return [order for order in orders if order.order_id]

    async def fetch_order_details(self, order_id: str) -> MarketOrder | None:
        data = self._call("get_order_details", {"orderId": order_id})
        items = [
            LineItem(
                name=str(item.get("name", "item")),
                quantity=Decimal(str(item.get("quantity", 1) or 1)),
                price=normalise_price(item.get("finalPrice")),
            )
            for item in data.get("items", []) or []
            if not item.get("removed")
        ]
        bill = data.get("bill") or {}
        total = normalise_price(bill.get("grandTotal")) or normalise_price(data.get("totalBill"))
        return build_order(
            source=self.name,
            order_id=order_id,
            total=total,
            items=items,
            placed_at=data.get("createdAt"),
            raw=data,
        )


def connect_interactive(token_file: Path | None = None) -> Path:
    """Run the OAuth flow and save the token. Returns the token file path."""
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
