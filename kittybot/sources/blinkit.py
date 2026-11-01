"""Blinkit order history (read-only).

Blinkit has no public API, so this talks to the same endpoints the website
itself uses. Two things make it work:

1. **TLS impersonation.** Cloudflare scores the JA3/JA4 fingerprint, so plain
   ``httpx``/``requests``/``curl`` get a 403 block page no matter which
   headers you send. ``curl_cffi`` with ``impersonate="chrome"`` passes.
2. **Session harvesting.** The login session lives in the browser's
   localStorage (``auth.accessToken``, ``authKey``, ``deviceId``, ``location``)
   plus ``gr_1_*`` cookies. ``kittybot connect blinkit`` opens a real browser
   once, the user logs in, and we save that session to disk (mode 600).

Safety: this adapter is read-only *by construction*. Every request goes
through :func:`_assert_allowed`, which allowlists URL prefixes. Blinkit's
cart and checkout endpoints are POSTs too, so blocking by HTTP method would
not be enough.

Endpoint notes: https://github.com/maheshrijal/blinkcli/blob/main/docs/endpoint-notes.md
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..models import LineItem, MarketOrder
from .base import (
    OrderSource,
    SourceError,
    build_order,
    is_cancelled_order,
    normalise_price,
    require_total,
)

BASE = "https://blinkit.com"
SESSION_FILE = Path.home() / ".config" / "kittybot" / "blinkit_session.json"

# Blinkit's fixed request key, taken from its own JS bundle (s.config.requestKey).
REQUEST_KEY = "c0e6868e-1180-400c-be51-f473479f1f0a"

# Only these URL prefixes may ever be requested. Anything that touches a cart,
# a checkout or a payment endpoint is absent on purpose.
ALLOWED_PREFIXES = (
    "/v1/layout/order_history",
    "/v1/layout/order_details",
    "/v3/history/orders",
    "/v1/order_count",
    "/v4/address",
    "/v2/accounts/auth_key/",
    "/",
)

# Never send these, whatever the URL.
FORBIDDEN_FRAGMENTS = (
    "/v5/carts",
    "/createOrder",
    "/v2/payment",
    "/v3/cart/",
    "/checkout",
    "/zomato_payment_hash",
)

APP_VERSION_V1 = "1010101010"
APP_VERSION_V2 = "52434332"
WEB_APP_VERSION = "1008010016"
RN_BUNDLE_VERSION = "1009003012"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Pack size units, used to disambiguate "2 x 75 g" from "500 g x 1".
UNIT_RE = re.compile(
    r"\b(?:kg|g|gm|ml|l|litre|liter|pcs?|pack|piece|dozen|strip|bottle|bag|box|tube|can|jar)\b",
    re.I,
)


class BlinkitSource(OrderSource):
    """Read-only Blinkit order history."""

    name = "blinkit"
    label = "Blinkit"
    requires_auth = True

    def __init__(self, session_file: Path | None = None, sleep_ms: int = 350) -> None:
        self.session_file = session_file or SESSION_FILE
        self.sleep_ms = sleep_ms
        self._session = None

    # ------------------------------------------------------------------
    # session handling
    # ------------------------------------------------------------------
    def load_session(self) -> dict:
        if not self.session_file.exists():
            raise SourceError(
                "Blinkit is not connected. Run: kittybot connect blinkit"
            )
        try:
            data = json.loads(self.session_file.read_text())
        except Exception as exc:
            raise SourceError(f"Blinkit session file is unreadable: {exc}") from exc
        if not data.get("access_token"):
            raise SourceError("Blinkit session file has no access_token; reconnect")
        return data

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
    def _client(self):
        if self._session is None:
            from curl_cffi import requests as curl_requests

            self._session = curl_requests.Session(impersonate="chrome")
        return self._session

    @staticmethod
    def _assert_allowed(path: str) -> None:
        """Refuse anything outside the read-only allowlist."""
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment.lower() in path.lower():
                raise SourceError(f"refusing non-read-only Blinkit path: {path}")
        if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            raise SourceError(f"refusing Blinkit path outside the allowlist: {path}")

    def _headers(self, session: dict, v1: bool = False) -> dict[str, str]:
        headers = {
            "app_client": "consumer_web",
            "platform": "desktop_web",
            "web_app_version": WEB_APP_VERSION,
            "rn_bundle_version": RN_BUNDLE_VERSION,
            "app_version": (
                session.get("app_version_v1")
                if v1 and session.get("app_version_v1")
                else session.get("app_version_v2")
                or (APP_VERSION_V1 if v1 else APP_VERSION_V2)
            ),
            "origin": BASE,
            "referer": f"{BASE}/account/orders",
            "device_id": session.get("device_id", ""),
            "session_uuid": session.get("session_uuid", str(uuid.uuid4())),
            "access_token": session.get("access_token", ""),
            "auth_key": session.get("auth_key", ""),
            "user-agent": session.get("user_agent", USER_AGENT),
            "accept": "application/json",
            "content-type": "application/json",
        }
        if session.get("lat"):
            headers["lat"] = str(session["lat"])
        if session.get("lon"):
            headers["lon"] = str(session["lon"])
        # Never send the literal string "null" as the access token: the web app
        # does that when logged out and the API rejects it with a 401.
        if headers.get("access_token") == "null":
            headers.pop("access_token")
        return {k: v for k, v in headers.items() if v}

    def _request(
        self,
        method: str,
        path: str,
        session: dict,
        *,
        v1: bool = False,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Make one Blinkit call and classify the outcome."""
        self._assert_allowed(path)
        client = self._client()
        url = f"{BASE}{path}"
        try:
            response = client.request(
                method,
                url,
                headers=self._headers(session, v1=v1),
                cookies=session.get("cookies") or {},
                json=json_body,
                params=params,
                timeout=30,
            )
        except Exception as exc:
            raise SourceError(f"Blinkit is unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise SourceError(
                "Blinkit rejected the session (it may have expired). "
                "Run: kittybot connect blinkit"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise SourceError(f"Blinkit is throttling us (HTTP {response.status_code}), try later")
        if response.status_code != 200:
            raise SourceError(f"Blinkit returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            # A non-JSON body means Cloudflare served a challenge page, not that
            # the session expired. Never tell a user to re-auth for this.
            raise SourceError(
                "Blinkit served a non-JSON response (likely a bot challenge). "
                "The session may still be valid; try again shortly."
            )
        try:
            return response.json()
        except Exception as exc:
            raise SourceError(f"Blinkit returned malformed JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    async def fetch_orders(self, since: datetime | None = None) -> list[MarketOrder]:
        session = self.load_session()
        refs = self._list_order_refs(session)
        orders: list[MarketOrder] = []
        for ref in refs:
            if is_cancelled_order(ref):
                continue
            detail = self._order_detail(session, ref)
            if detail is None:
                continue
            orders.append(detail)
            time.sleep(self.sleep_ms / 1000)
        return orders

    def _list_order_refs(self, session: dict) -> list[dict[str, Any]]:
        """Fetch the order list (no line items) via the v3 history endpoint."""
        refs: list[dict[str, Any]] = []
        start, next_page = 0, 10
        for _ in range(5):  # 5 pages x 10 = 50 recent orders is plenty
            payload = self._request(
                "GET",
                "/v3/history/orders",
                session,
                params={"start": start, "next": next_page, "template_version": 9},
            )
            widgets = payload.get("widgets") or []
            page_refs = _parse_order_refs(widgets)
            if not page_refs:
                break
            refs.extend(page_refs)
            if not payload.get("has_more_orders"):
                break
            start += next_page
            time.sleep(self.sleep_ms / 1000)
        if not refs:
            # Fall back to the legacy SDUI endpoint.
            payload = self._request("POST", "/v1/layout/order_history", session, v1=True)
            refs = _parse_order_refs(payload.get("response", {}).get("snippets", []))
        return refs

    def _order_detail(self, session: dict, ref: dict[str, Any]) -> MarketOrder | None:
        """Fetch one order's line items, prices and bill."""
        order_id = ref.get("order_id")
        cart_id = ref.get("cart_id")
        if not order_id:
            return None
        path = f"/v1/layout/order_details/{order_id}"
        if cart_id:
            path += f"?cart_id={cart_id}"
        payload = self._request("POST", path, session, v1=True)

        objects = (payload.get("response") or {}).get("objects") or []
        items, total, placed_at = _parse_order_detail(objects)

        if total is None:
            total = ref.get("amount")
        if placed_at is None:
            placed_at = ref.get("placed_at")

        return build_order(
            source=self.name,
            order_id=order_id,
            total=require_total(total, self.label, order_id),
            items=items,
            placed_at=placed_at,
            raw={"ref": ref, "detail": payload},
        )


# ----------------------------------------------------------------------
# parsing helpers (pure functions, easy to fixture-test)
# ----------------------------------------------------------------------
def _walk(nodes: Any):
    """Yield every dict in an arbitrarily nested SDUI tree."""
    if isinstance(nodes, dict):
        yield nodes
        for value in nodes.values():
            yield from _walk(value)
    elif isinstance(nodes, list):
        for value in nodes:
            yield from _walk(value)


def _history_text(data: dict, key: str) -> str | None:
    """Find a text field in a history widget's data.

    Blinkit's history card nests the interesting values one level deeper
    than it first appears::

        data.items[].data.left_underlined_subtitle.text   # the ₹ total
        data.items[].data.subtitle.text                   # the date

    but some generations put them directly on ``data``. Look in both.
    """
    direct = _text(data, key)
    if direct:
        return direct
    for entry in data.get("items", []) or []:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("data") or entry
        value = _text(inner, key)
        if value:
            return value
    return None


def _parse_order_refs(widgets: list) -> list[dict[str, Any]]:
    """Pull (order_id, cart_id, status, amount, date) out of the history list."""
    refs: list[dict[str, Any]] = []
    for node in _walk(widgets):
        common = (node.get("tracking") or {}).get("common_attributes") or {}
        order_id = common.get("order_id")
        if not order_id:
            continue
        cart_id = common.get("cart_id")
        if not cart_id:
            deeplink = common.get("deeplink") or ""
            match = re.search(r"cart_id=(\d+)", deeplink)
            cart_id = match.group(1) if match else None
        data = node.get("data") or {}
        amount = normalise_price(_history_text(data, "left_underlined_subtitle"))
        placed = _history_text(data, "subtitle")
        refs.append(
            {
                "order_id": str(order_id),
                "cart_id": str(cart_id) if cart_id else None,
                "status": common.get("order_status") or "UNKNOWN",
                "amount": amount,
                "placed_at": _parse_blinkit_date(placed),
            }
        )
    # A single order can appear in several widgets; keep the first sighting.
    seen: dict[str, dict[str, Any]] = {}
    for ref in refs:
        seen.setdefault(ref["order_id"], ref)
    return list(seen.values())


def _text(data: dict, key: str) -> str | None:
    node = data.get(key)
    if isinstance(node, dict):
        return node.get("text")
    if isinstance(node, str):
        return node
    return None


def _parse_order_detail(objects: list) -> tuple[list[LineItem], Decimal | None, str | None]:
    """Extract line items, the bill total and the order date from a detail tree."""
    items: list[LineItem] = []
    total: Decimal | None = None
    placed_at: str | None = None

    for node in _walk(objects):
        widget_name = ((node.get("tracking") or {}).get("widget_meta") or {}).get("widget_name")
        data = node.get("data") or {}

        # Current widget: order_items[] with product_name / final_quantity / final_price
        for raw in data.get("order_items") or []:
            name = raw.get("product_name") or raw.get("name")
            if not name:
                continue
            quantity = _to_decimal(raw.get("final_quantity")) or Decimal(1)
            price = normalise_price(raw.get("final_price"))
            items.append(LineItem(name=str(name), quantity=quantity, price=price))

        # Older widget: title / subtitle1 (qty+pack) / subtitle3 (price)
        if widget_name == "order_details_list" or "subtitle3" in data:
            title = data.get("title")
            if isinstance(title, dict):
                title = title.get("text")
            if title and not any(i.name == str(title) for i in items):
                qty_text = data.get("subtitle1")
                if isinstance(qty_text, dict):
                    qty_text = qty_text.get("text")
                price_text = data.get("subtitle3")
                if isinstance(price_text, dict):
                    price_text = price_text.get("text")
                quantity = _parse_quantity(qty_text)
                items.append(
                    LineItem(
                        name=str(title),
                        quantity=quantity,
                        price=normalise_price(price_text),
                    )
                )

        # Bill total
        for key in ("payable_amount", "total_amount", "total_text", "payable_text"):
            if data.get(key) is not None:
                value = normalise_price(data.get(key))
                if value is not None:
                    total = value
                    break

        # Order date: "placed on Sun, 13 Sep'26, 9:58 PM" / "placed today, 12:03 AM"
        checkout = data.get("checkout_time")
        if isinstance(checkout, dict):
            checkout = checkout.get("text")
        if isinstance(checkout, str) and checkout.lower().startswith("placed"):
            parsed = _parse_placed_on(checkout)
            if parsed:
                placed_at = parsed

    # De-duplicate items, keeping the richest version of each name.
    deduped: dict[str, LineItem] = {}
    for item in items:
        existing = deduped.get(item.name)
        if existing is None or (existing.price is None and item.price is not None):
            deduped[item.name] = item
    return list(deduped.values()), total, placed_at


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_quantity(text: str | None) -> Decimal:
    """Parse Blinkit's ambiguous quantity strings.

    Blinkit renders quantity in two orders:
      pack-first  "500 g x 1"  -> count 1
      count-first "2 x 75 g"   -> count 2

    The rule is that the number *adjacent to a unit* is the pack size, and the
    other number is the count. Never guess by comparing the two numbers:
    "2 x 2 kg" has both the same.
    """
    if not text:
        return Decimal(1)
    parts = [p.strip() for p in re.split(r"\s*[x×]\s*", text) if p.strip()]
    if len(parts) != 2:
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        return _to_decimal(match.group(1)) if match else Decimal(1)

    left, right = parts
    left_has_unit = bool(UNIT_RE.search(left))
    right_has_unit = bool(UNIT_RE.search(right))

    if right_has_unit and not left_has_unit:
        count_text = left
    elif left_has_unit and not right_has_unit:
        count_text = right
    else:
        # Both or neither look like units: fall back to the first number, which
        # matches Blinkit's more common pack-first rendering.
        count_text = left

    match = re.search(r"(\d+(?:\.\d+)?)", count_text)
    return _to_decimal(match.group(1)) if match else Decimal(1)


def _parse_blinkit_date(text: str | None) -> str | None:
    """Parse the history list's date strings. The year is often missing."""
    if not text:
        return None
    text = text.strip()
    now = datetime.now()
    if text.lower().startswith("today"):
        time_part = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm))", text, re.I)
        return f"{now.date().isoformat()} {time_part.group(1) if time_part else ''}".strip()
    if text.lower().startswith("yesterday"):
        time_part = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm))", text, re.I)
        yesterday = now.date().toordinal() - 1
        from datetime import date

        return f"{date.fromordinal(yesterday).isoformat()} {time_part.group(1) if time_part else ''}".strip()
    for fmt in ("%d %b, %I:%M %p", "%d %b %Y", "%d %b %I:%M %p"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _parse_placed_on(text: str) -> str | None:
    """Parse the detail page's date, which does carry the year."""
    from datetime import date

    now = datetime.now()
    cleaned = text.strip()
    if cleaned.lower().startswith("placed today"):
        time_part = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))", cleaned, re.I)
        return f"{now.date().isoformat()} {time_part.group(1) if time_part else ''}".strip()
    if cleaned.lower().startswith("placed yesterday"):
        time_part = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))", cleaned, re.I)
        yesterday = date.fromordinal(now.date().toordinal() - 1)
        return f"{yesterday.isoformat()} {time_part.group(1) if time_part else ''}".strip()

    match = re.search(r"placed on\s+(.+)", cleaned, re.I)
    if not match:
        return None
    rest = match.group(1).strip()
    # "Sun, 13 Sep'26, 9:58 PM" -> "Sun, 13 Sep 26, 9:58 PM".
    # The space matters: without it the month name and the year are glued
    # together ("Sep26") and strptime cannot parse them.
    stripped = re.sub(r"'(\d{2})\b", r" \1", rest)
    stripped = re.sub(r"\b(\d{3,4})\b", lambda m: m.group(1)[:2], stripped)
    for fmt in ("%a, %d %b %y, %I:%M %p", "%a, %d %b %y, %H:%M", "%d %b %y, %I:%M %p"):
        try:
            return datetime.strptime(stripped, fmt).isoformat()
        except ValueError:
            continue
    return None
