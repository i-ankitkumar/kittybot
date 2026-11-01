"""Order sources: read-only adapters for grocery marketplaces.

Every adapter implements :class:`OrderSource` and returns normalised
:class:`~kittybot.models.MarketOrder` objects. Adapters NEVER place, modify
or cancel an order; they only read history.

Adding a new marketplace means writing one class and registering it.
"""

from __future__ import annotations

import abc
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..models import LineItem, MarketOrder

CREDENTIALS_DIR = Path.home() / ".config" / "kittybot"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"


class SourceError(RuntimeError):
    """Raised when a marketplace cannot be read."""


class OrderSource(abc.ABC):
    """Read-only access to one marketplace's order history."""

    #: short identifier, e.g. "instamart"
    name: str = "source"
    #: human label for menus
    label: str = "Source"
    #: whether this source needs credentials before it can be used
    requires_auth: bool = True

    @abc.abstractmethod
    async def fetch_orders(self, since: datetime | None = None) -> list[MarketOrder]:
        """Return recent orders, newest first."""

    async def check_connection(self) -> bool:
        """Whether credentials are present and probably still valid."""
        return False

    # -- credential helpers -------------------------------------------------
    @staticmethod
    def load_credentials() -> dict:
        if not CREDENTIALS_FILE.exists():
            return {}
        try:
            return json.loads(CREDENTIALS_FILE.read_text())
        except Exception:
            return {}

    @staticmethod
    def save_credentials(data: dict) -> None:
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_FILE.write_text(json.dumps(data, indent=2))
        CREDENTIALS_FILE.chmod(0o600)

    def credential(self, key: str) -> str | None:
        return self.load_credentials().get(self.name, {}).get(key)

    def store_credential(self, key: str, value: str) -> None:
        data = self.load_credentials()
        data.setdefault(self.name, {})[key] = value
        self.save_credentials(data)


#: Currency words and symbols that may prefix or suffix an amount.
_CURRENCY_RE = re.compile(
    r"(?i)\b(?:rs|inr|usd|eur|gbp|jpy|aud|cad)\b\.?|₹|\$|€|£|¥|₹"
)
_AMOUNT_RE = re.compile(r"-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)")


#: Words that mean "this order never became spend".
_CANCEL_WORDS = ("CANCEL", "REFUND", "RETURNED", "FAILED", "DECLINED")


def is_cancelled_order(raw: object) -> bool:
    """True when a marketplace marks the order as cancelled or refunded.

    A cancelled order is not spend. Every adapter checks this, because
    importing one would add an expense that never happened.
    """
    if not isinstance(raw, dict):
        return False
    for key in ("status", "order_status", "orderStatus", "state", "order_state"):
        value = raw.get(key)
        if isinstance(value, str) and any(word in value.upper() for word in _CANCEL_WORDS):
            return True
    for key in ("refund_details", "refundDetails", "refund"):
        value = raw.get(key)
        if not value:
            continue
        if isinstance(value, dict):
            amount = normalise_price(
                value.get("total_amount_refunded")
                or value.get("refunded_amount")
                or value.get("amount")
            )
            if amount and amount > 0:
                return True
        elif isinstance(value, (int, float, Decimal)) and Decimal(str(value)) > 0:
            return True
    return False


def normalise_price(value: object) -> Decimal | None:
    """Coerce whatever a marketplace returns into a Decimal rupees amount.

    Handles "₹1,234.50", "Rs. 120", "Rs 120", "INR 99" and plain numbers.
    The currency prefix has to be stripped BEFORE the number is parsed,
    otherwise the dot in "Rs." becomes a decimal point and 120 reads as 0.12.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value).strip()
    if not text:
        return None
    cleaned = _CURRENCY_RE.sub(" ", text)
    match = _AMOUNT_RE.search(cleaned)
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", "")).quantize(Decimal("0.01"))
    except Exception:
        return None


def require_total(value: Decimal | None, source: str, order_id: str = "") -> Decimal:
    """Fail loudly when an order total cannot be read.

    A renamed field in a marketplace response must not turn every order
    into a silently skipped zero. The old behaviour was to coerce a missing
    total to 0 and drop the order, so a sync reported "0 new proposals"
    with no reason and no clue that anything was wrong.

    BigBasket already refused a bill that did not reconcile. This is the
    same rule for the other three: if the amount cannot be read, say so.
    """
    if value is None:
        raise SourceError(
            f"{source} returned an order{f' {order_id}' if order_id else ''} "
            "with no readable total. The response shape may have changed."
        )
    return value


def parse_timestamp(value: object) -> str | None:
    """Best-effort conversion of a marketplace timestamp to ISO-8601."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def build_order(
    source: str,
    order_id: str,
    total: Decimal | None,
    items: list[LineItem] | None = None,
    placed_at: str | None = None,
    currency: str = "INR",
    raw: dict | None = None,
) -> MarketOrder:
    """Assemble a normalised order."""
    return MarketOrder(
        source=source,
        order_id=str(order_id),
        total=total if total is not None else Decimal(0),
        placed_at=placed_at,
        items=items or [],
        currency=currency,
        raw=raw or {},
    )


def default_since(days: int = 15) -> datetime:
    """How far back to look by default.

    Swiggy's official MCP returns the last 15 days of orders, so that is the
    default window for every source to stay consistent.
    """
    return datetime.now() - timedelta(days=days)
