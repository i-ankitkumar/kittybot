"""BigBasket order history (read-only).

BigBasket has no public consumer API. The member endpoints the website
itself uses are:

* ``POST /self-service-svc/v2/order-listing/`` - past orders (a pure read
  that happens to be a POST)
* ``GET  /order/v2/order/{order_id}/detail``     - line items, prices, bill

Two things are required:

1. **A session.** BigBasket is OTP-only (passwords were removed in 2021)
   and the login page is behind reCAPTCHA Enterprise, so a scripted login is
   not possible. ``kittybot connect bigbasket`` opens a real browser once;
   you log in with your OTP; the cookies are saved to disk (mode 600) and
   replayed afterwards.
2. **Chrome TLS.** BigBasket sits behind Akamai bot protection, so a plain
   ``httpx`` request is rejected. ``curl_cffi`` with ``impersonate="chrome"``
   passes.

Bills are reconciled before an order is accepted: if the components do not
add up, the order is rejected rather than silently recorded wrong.

Research: https://github.com/Harsh-Gopal/CartRadar
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..models import LineItem, MarketOrder
from .base import (
    OrderSource,
    SourceError,
    build_order,
    is_cancelled_order,
    normalise_price,
)

BASE = "https://www.bigbasket.com"
SESSION_FILE = Path.home() / ".config" / "kittybot" / "bigbasket_session.json"

# The business lines to query: BigBasket B2C, BB Now express, Tata Neu.
EC_IDS = ["1", "100", "10"]
ENTRY_CONTEXT = "bb-b2c"

# Only these paths may be requested. Everything that could change an order
# or touch a payment is absent on purpose.
ALLOWED_PATHS = (
    "/self-service-svc/v2/order-listing/",
    "/order/v2/order/",
    "/order/v1/order/",
    "/order/v1/order/tabs",
)

# Never touch these, whatever the path.
FORBIDDEN_FRAGMENTS = (
    "/cancel",
    "/paynow",
    "/potentialorder",
    "/checkout",
    "/payment_svc/",
    "/initiate-cee-call",
    "/basketService/update-qty",
    "/voucher/submit",
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

TOLERANCE = Decimal("0.01")


class BigBasketSource(OrderSource):
    """Read-only BigBasket order history."""

    name = "bigbasket"
    label = "BigBasket"
    requires_auth = True

    def __init__(self, session_file: Path | None = None, sleep_ms: int = 400) -> None:
        self.session_file = session_file or SESSION_FILE
        self.sleep_ms = sleep_ms
        self._client = None
        self._session: dict | None = None

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def load_session(self) -> dict:
        if self.session_file.exists():
            try:
                data = json.loads(self.session_file.read_text())
                if data.get("cookies"):
                    return data
            except Exception:
                pass
        raise SourceError(
            "BigBasket is not connected. Run: kittybot connect bigbasket"
        )

    def save_session(self, data: dict) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps(data, indent=2))
        self.session_file.chmod(0o600)

    async def check_connection(self) -> bool:
        try:
            self.load_session()
        except SourceError:
            return False
        return True

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            from curl_cffi import requests as curl_requests

            self._client = curl_requests.Session(impersonate="chrome")
        return self._client

    def _headers(self, session: dict) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": BASE,
            "referer": f"{BASE}/member/active-orders",
            "user-agent": session.get("user_agent") or USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
            "x-sourceslug": "bigbasket",
            "x-sourceid": "1",
            "x-timestamp": str(int(time.time())),
            "x-channel": "web",
            "x-entry-context": ENTRY_CONTEXT,
        }
        if session.get("member_id"):
            headers["memberid"] = str(session["member_id"])
        if session.get("csrf_token"):
            headers["x-csurftoken"] = str(session["csrf_token"])
        if session.get("entry_context_id"):
            headers["x-entry-context-id"] = str(session["entry_context_id"])
        return headers

    @staticmethod
    def _assert_allowed(path: str) -> None:
        lowered = path.lower()
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                raise SourceError(f"refusing non-read-only BigBasket path: {path}")
        if not any(lowered.startswith(prefix) for prefix in ALLOWED_PATHS):
            raise SourceError(f"refusing BigBasket path outside the allowlist: {path}")

    def _request(
        self, method: str, path: str, session: dict, body: dict | None = None
    ) -> dict:
        self._assert_allowed(path)
        client = self._get_client()
        cookies = session.get("cookies") or {}
        try:
            response = client.request(
                method,
                f"{BASE}{path}",
                headers=self._headers(session),
                cookies=cookies,
                json=body,
                timeout=30,
            )
        except Exception as exc:
            raise SourceError(f"BigBasket is unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise SourceError(
                "BigBasket rejected the session (it may have expired). "
                "Run: kittybot connect bigbasket"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise SourceError(f"BigBasket is throttling us (HTTP {response.status_code})")
        if response.status_code != 200:
            raise SourceError(f"BigBasket returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise SourceError(
                "BigBasket served a non-JSON response (likely a bot challenge). "
                "Try again shortly."
            )
        try:
            return response.json()
        except Exception as exc:
            raise SourceError(f"BigBasket returned malformed JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    async def fetch_orders(self, since: datetime | None = None) -> list[MarketOrder]:
        session = self.load_session()
        listing = self._request(
            "POST",
            "/self-service-svc/v2/order-listing/",
            session,
            body={
                "context": "all",
                "group_by": "po",
                "old_system_orders": True,
                "request_type": "my_orders",
                "sort_by": "creation_date",
                "modifications": True,
                "exchange_order_supported": True,
                "additional_query_params": None,
                "ec_ids": EC_IDS,
                "page": 1,
                "identifier": ENTRY_CONTEXT,
            },
        )

        orders: list[MarketOrder] = []
        for group in listing.get("orders", []) or []:
            for stub in group.get("orders", []) or []:
                order = self._build_order(session, stub)
                if order:
                    orders.append(order)
                time.sleep(self.sleep_ms / 1000)
        return orders

    def _build_order(self, session: dict, stub: dict) -> MarketOrder | None:
        order_id = stub.get("order_id")
        if not order_id:
            return None
        if is_cancelled_order(stub):
            return None

        detail = self._request("GET", f"/order/v2/order/{order_id}/detail", session)
        order = (detail or {}).get("order") or {}
        if not order:
            return None
        if is_cancelled_order(order):
            return None

        items: list[LineItem] = []
        for group in order.get("items", []) or []:
            for line in group.get("line_items", []) or []:
                name = line.get("desc")
                if not name:
                    continue
                quantity = Decimal(str(line.get("actual_quantity") or line.get("quantity") or 1))
                # sp is the unit selling price; the line total is "total".
                unit_price = normalise_price(line.get("sp"))
                line_total = normalise_price(line.get("total"))
                if unit_price is None and line_total is not None:
                    unit_price = line_total / quantity if quantity else line_total
                items.append(
                    LineItem(
                        name=f"{name} ({line.get('brand')})" if line.get("brand") else str(name),
                        quantity=quantity,
                        price=unit_price,
                    )
                )

        total = normalise_price(
            order.get("total_payable_with_charges") or order.get("total_with_charges")
        )
        if total is None:
            total = Decimal(0)

        refund = normalise_price(
            ((detail or {}).get("refund_details") or {}).get("total_amount_refunded")
        )
        if refund:
            # A refunded order is not spend.
            return None

        if not _reconciles(order, items):
            raise SourceError(
                f"BigBasket order {order_id} does not reconcile; refusing to record it "
                "rather than writing a wrong amount."
            )

        return build_order(
            source=self.name,
            order_id=str(order_id),
            total=total,
            items=items,
            placed_at=_parse_bb_date(order.get("created_on")),
            raw={"stub": stub, "detail": detail},
        )


# ----------------------------------------------------------------------
# parsing / reconciliation
# ----------------------------------------------------------------------
def _parse_bb_date(value: object) -> str | None:
    """BigBasket uses "YYYY-MM-DD HH:MM:SS" for past rows and ISO for recent ones."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _reconciles(order: dict, items: list[LineItem]) -> bool:
    """Check that the bill adds up.

    Rules (from the reference implementations):
      1. unit_price x quantity == line_total, per line
      2. sum of line totals == the item subtotal
      3. subtotal + fees == the order total
      4. total - (wallet + neucoins) == amount payable

    We fail closed: an order that does not reconcile is rejected.
    """
    if not items:
        return False

    # 1. per-line check
    for group in order.get("items", []) or []:
        for line in group.get("line_items", []) or []:
            unit = normalise_price(line.get("sp"))
            line_total = normalise_price(line.get("total"))
            quantity = Decimal(str(line.get("actual_quantity") or line.get("quantity") or 1))
            if unit is None or line_total is None:
                continue
            if abs(unit * quantity - line_total) > TOLERANCE:
                return False

    # 2. line totals vs subtotal
    subtotal = normalise_price(order.get("sub_total"))
    if subtotal is not None:
        computed = sum(
            (normalise_price(line.get("total")) or Decimal(0))
            for group in order.get("items", []) or []
            for line in group.get("line_items", []) or []
        )
        if abs(computed - subtotal) > TOLERANCE:
            return False

    # 3. charges vs total
    total_with_charges = normalise_price(order.get("total_with_charges"))
    if total_with_charges is not None:
        charges = sum(
            (normalise_price(charge.get("charge")) or Decimal(0))
            for charge in order.get("charges", []) or []
        )
        if subtotal is not None and abs(subtotal + charges - total_with_charges) > TOLERANCE:
            return False

    # 4. wallet and neu coins
    payable = normalise_price(order.get("total_payable_with_charges"))
    if payable is not None and total_with_charges is not None:
        wallet = normalise_price(order.get("wallet_used")) or Decimal(0)
        neu = normalise_price(order.get("neucoins_used")) or Decimal(0)
        if abs(total_with_charges - wallet - neu - payable) > TOLERANCE:
            return False

    return True


def decode_bb_cookie(value: str) -> str:
    """Decode BigBasket's base64 location cookies for display."""
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except Exception:
        return value
