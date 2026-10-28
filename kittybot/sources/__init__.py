"""Read-only marketplace adapters."""

from __future__ import annotations

from ..config import Config
from .base import OrderSource, SourceError, build_order, default_since, normalise_price
from .bigbasket import BigBasketSource
from .blinkit import BlinkitSource
from .instamart import InstamartSource
from .zepto import ZeptoSource

#: Every adapter kittybot knows about.
ALL_SOURCES: dict[str, type[OrderSource]] = {
    "instamart": InstamartSource,
    "zepto": ZeptoSource,
    "blinkit": BlinkitSource,
    "bigbasket": BigBasketSource,
}

__all__ = [
    "ALL_SOURCES",
    "BigBasketSource",
    "BlinkitSource",
    "InstamartSource",
    "OrderSource",
    "SourceError",
    "ZeptoSource",
    "build_order",
    "build_sources",
    "default_since",
    "normalise_price",
]


def build_sources(config: Config | None = None) -> list[OrderSource]:
    """Instantiate the enabled adapters.

    A source that is not in ``config.enabled_sources`` is skipped, unless the
    config lists none at all, in which case every known adapter is offered
    (each one reports "not connected" until it is).
    """
    enabled = list(config.enabled_sources) if config and config.enabled_sources else []
    names = enabled or list(ALL_SOURCES)
    sources: list[OrderSource] = []
    for name in names:
        source_cls = ALL_SOURCES.get(name)
        if source_cls is None:
            continue
        sources.append(source_cls())
    return sources
