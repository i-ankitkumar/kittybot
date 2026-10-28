"""Order sources: read-only adapters for grocery marketplaces.

Every adapter implements :class:`OrderSource` and returns normalised
:class:`~kittybot.models.MarketOrder` objects. Adapters NEVER place, modify
or cancel an order; they only read history.

Adding a new marketplace means writing one class and registering it.
"""

from __future__ import annotations

import abc
import json
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

    def __init__(self, **_: object) -> None:
        pass

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


def normalise_price(value: object) -> Decimal | None:
    """Coerce whatever a marketplace returns into a Decimal rupees amount."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value).strip()
    if not text:
        return None
    # "₹1,234.50", "Rs. 120", "1234.5"
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except Exception:
        return None


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
