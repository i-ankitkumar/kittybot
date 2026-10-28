"""Import-and-wiring checks: every module must import and the registry must be sane."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "kittybot.models",
        "kittybot.splits",
        "kittybot.kittysplit",
        "kittybot.mcp",
        "kittybot.store",
        "kittybot.approvals",
        "kittybot.config",
        "kittybot.cli",
        "kittybot.bot.telegram",
        "kittybot.dashboard.app",
        "kittybot.sources",
        "kittybot.sources.base",
        "kittybot.sources.instamart",
        "kittybot.sources.zepto",
        "kittybot.sources.blinkit",
        "kittybot.sources.bigbasket",
    ],
)
def test_module_imports(module):
    importlib.import_module(module)


def test_all_four_sources_are_registered():
    from kittybot.sources import ALL_SOURCES

    assert set(ALL_SOURCES) == {"instamart", "zepto", "blinkit", "bigbasket"}


def test_sources_can_be_built():
    from kittybot.sources import build_sources

    sources = build_sources()
    assert len(sources) == 4
    for source in sources:
        assert source.name
        assert source.label


def test_enabled_sources_filter_works():
    from kittybot.config import Config
    from kittybot.sources import build_sources

    config = Config()
    config.enabled_sources = ["zepto"]
    sources = build_sources(config)
    assert [s.name for s in sources] == ["zepto"]


def test_blinkit_refuses_non_readonly_paths():
    from kittybot.sources.blinkit import BlinkitSource

    for path in ("/v5/carts", "/createOrder/123", "/v2/payment/x", "/v3/cart/1/actions"):
        with pytest.raises(Exception):
            BlinkitSource._assert_allowed(path)


def test_blinkit_allows_readonly_paths():
    from kittybot.sources.blinkit import BlinkitSource

    for path in (
        "/v1/layout/order_history",
        "/v1/layout/order_details/123",
        "/v3/history/orders",
        "/v1/order_count",
    ):
        BlinkitSource._assert_allowed(path)


def test_bigbasket_refuses_non_readonly_paths():
    from kittybot.sources.bigbasket import BigBasketSource

    for path in (
        "/order/v1/order/123/cancel",
        "/payment_svc/v1/payment/get-sdk-params/",
        "/potentialorder/",
        "/order/v2/checkout",
    ):
        with pytest.raises(Exception):
            BigBasketSource._assert_allowed(path)


def test_bigbasket_allows_readonly_paths():
    from kittybot.sources.bigbasket import BigBasketSource

    for path in (
        "/self-service-svc/v2/order-listing/",
        "/order/v2/order/123/detail",
        "/order/v1/order/tabs",
    ):
        BigBasketSource._assert_allowed(path)


def test_blinkit_quantity_parser():
    """The number next to a unit is the pack size; the other is the count."""
    from decimal import Decimal

    from kittybot.sources.blinkit import _parse_quantity

    assert _parse_quantity("500 g x 1") == Decimal(1)   # pack-first
    assert _parse_quantity("2 x 75 g") == Decimal(2)     # count-first
    assert _parse_quantity("2 x 2 kg") == Decimal(2)     # both look like units
    assert _parse_quantity(None) == Decimal(1)


def test_bigbasket_reconciliation_rejects_a_bad_bill():
    from kittybot.sources.bigbasket import _reconciles

    order = {
        "items": [
            {
                "line_items": [
                    {"desc": "Milk", "sp": 30.0, "total": 60.0, "quantity": 2},
                ]
            }
        ],
        "sub_total": 210.0,
        "charges": [{"charge": 29.0}],
        "total_with_charges": 239.0,
        "total_payable_with_charges": 239.0,
        "wallet_used": 0.0,
        "neucoins_used": 0.0,
    }
    # subtotal 210 does not match the line total of 60
    assert _reconciles(order, []) is False


def test_bigbasket_reconciliation_accepts_a_good_bill():
    from kittybot.sources.bigbasket import _reconciles

    order = {
        "items": [
            {
                "line_items": [
                    {"desc": "Milk", "sp": 30.0, "total": 60.0, "quantity": 2},
                ]
            }
        ],
        "sub_total": 60.0,
        "charges": [{"charge": 29.0}],
        "total_with_charges": 89.0,
        "total_payable_with_charges": 89.0,
        "wallet_used": 0.0,
        "neucoins_used": 0.0,
    }
    from kittybot.models import LineItem

    items = [LineItem(name="Milk", quantity=Decimal(2), price=Decimal(30))]
    assert _reconciles(order, items) is True
