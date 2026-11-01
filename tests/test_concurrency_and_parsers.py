"""Tests for the concurrency, authentication and parser gaps.

Covers the gaps the dogfood pass flagged as out of scope:
  - two simultaneous approvals must not write the expense twice
  - the dashboard must not be an open door when a token is configured
  - the marketplace parsers must handle the response shapes the research
    documents, using captured fixtures
"""

from __future__ import annotations

import re
import threading
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from kittybot.approvals import ApprovalEngine
from kittybot.config import Config
from kittybot.dashboard.app import create_app
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, LineItem
from kittybot.sources.base import build_order
from kittybot.store import Store

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "no csrf token"
    return match.group(1)


@pytest.fixture(scope="module")
def live_kitty():
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        page = http.get(f"{BASE_URL}/en/new")
        resp = http.post(
            f"{BASE_URL}/en/new",
            data={
                "_csrf_token": _csrf(page.text),
                "kitty[name]": "kittybot concurrency test",
                "kitty[currency]": "INR",
                "kitty[parties][0][name]": "Ankit",
                "kitty[parties][1][name]": "Yuvraj",
            },
        )
        match = re.search(r"kittysplit\.com/(k/[^\"/?#]+)", str(resp.url))
        assert match, f"kitty creation failed: {resp.url}"
        path = match.group(1)
        yield f"{BASE_URL}/{path}"
        page = http.get(f"{BASE_URL}/{path}/delete")
        http.post(f"{BASE_URL}/{path}/delete", data={"_csrf_token": _csrf(page.text)})


@pytest.fixture
def engine(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "conc.db")
    yield ApprovalEngine(client, store)
    store.close()
    client.close()


def _proposal(engine, order_id="c-1", amount="500.00"):
    draft, _ = engine.propose_order(
        build_order(
            source="instamart",
            order_id=order_id,
            total=Decimal(amount),
            items=[LineItem(name="Milk", quantity=Decimal(1), price=Decimal(amount))],
        ),
        payer="Ankit",
    )
    return draft


# ----------------------------------------------------------------------
# Concurrency: one approval, one expense
# ----------------------------------------------------------------------
def test_simultaneous_approvals_write_once(engine):
    """Four threads approving the same proposal must create one expense.

    Before the atomic claim, all four read "pending", all four called
    add_expense(), and the kitty ended up with four duplicate entries —
    which charges the flatmate four times.
    """
    draft = _proposal(engine, "race-1")
    results: list[tuple] = []
    barrier = threading.Barrier(4)

    def approve() -> None:
        barrier.wait()
        try:
            result = engine.approve(draft.id)
            results.append(("ok", result.status.value))
        except Exception as exc:
            results.append(("err", type(exc).__name__))

    threads = [threading.Thread(target=approve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [r for r in results if r[0] == "ok"]
    assert len(successes) == 1, f"expected exactly one approval, got {results}"
    assert successes[0][1] == ApprovalStatus.APPROVED.value

    entries = [
        e
        for e in engine.client.list_expenses()
        if e["description"].startswith("Instamart")
    ]
    assert len(entries) == 1, f"the kitty got {len(entries)} entries, expected 1"


def test_approving_twice_in_sequence_is_rejected(engine):
    draft = _proposal(engine, "race-2")
    before = len(engine.client.list_expenses())
    assert engine.approve(draft.id).status is ApprovalStatus.APPROVED
    from kittybot.approvals import ApprovalError

    with pytest.raises(ApprovalError):
        engine.approve(draft.id)
    # A relative count: the kitty is shared across tests in this module.
    assert len(engine.client.list_expenses()) == before + 1


def test_a_stale_claim_is_released(engine):
    """A proposal stuck in 'approving' (process died mid-write) recovers."""
    draft = _proposal(engine, "race-3")
    engine.store.claim_proposal(draft.id)
    assert engine.store.get_proposal(draft.id).status is ApprovalStatus.APPROVING

    # Nothing to release yet: the claim is fresh.
    assert engine.store.release_stale_claims(max_age_seconds=300) == 0

    # Backdate the claim and it is released.
    engine.store._conn.execute(
        "UPDATE proposals SET decided_at = '2000-01-01T00:00:00' WHERE id = ?",
        (draft.id,),
    )
    engine.store._conn.commit()
    assert engine.store.release_stale_claims(max_age_seconds=300) == 1
    assert engine.store.get_proposal(draft.id).status is ApprovalStatus.PENDING


def test_the_engine_releases_stale_claims_on_startup(engine):
    """A proposal stranded by a crashed process is recovered, not lost."""
    draft = _proposal(engine, "stale-1")
    engine.store.claim_proposal(draft.id)
    assert engine.store.get_proposal(draft.id).status is ApprovalStatus.APPROVING

    # Backdate the claim, then build a new engine the way a restart would.
    engine.store._conn.execute(
        "UPDATE proposals SET decided_at = '2000-01-01T00:00:00' WHERE id = ?",
        (draft.id,),
    )
    engine.store._conn.commit()

    restarted = ApprovalEngine(engine.client, engine.store)
    recovered = restarted.store.get_proposal(draft.id)
    assert recovered is not None
    assert recovered.status is ApprovalStatus.PENDING


def test_store_survives_concurrent_reads_and_writes(engine):
    """The store must not raise InterfaceError under concurrent access."""
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            for _ in range(10):
                engine.store.list_proposals()
                draft = engine.propose_manual(
                    Decimal("10.00"),
                    f"Concurrent {index}",
                    paid_by="Ankit",
                    split=engine.client.solo_split("Ankit"),
                )
                engine.store.get_proposal(draft.id)
        except Exception as exc:  # pragma: no cover - the failure mode
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent store access raised: {errors[:3]}"


# ----------------------------------------------------------------------
# Dashboard authentication
# ----------------------------------------------------------------------
def _app_and_client(config: Config, engine, store) -> TestClient:
    return TestClient(create_app(config, engine, store), follow_redirects=True)


def test_dashboard_is_open_without_a_token(engine):
    config = Config()
    config.dashboard.token = ""
    store = engine.store
    with _app_and_client(config, engine, store) as client:
        assert client.get("/").status_code == 200


def test_dashboard_requires_auth_when_a_token_is_set(engine):
    config = Config()
    config.dashboard.token = "s3cret-token"
    store = engine.store
    with _app_and_client(config, engine, store) as client:
        response = client.get("/")
        assert response.status_code == 401
        assert response.headers.get("www-authenticate", "").lower().startswith("basic")


def test_dashboard_rejects_a_wrong_token(engine):
    config = Config()
    config.dashboard.token = "s3cret-token"
    store = engine.store
    with _app_and_client(config, engine, store) as client:
        response = client.get("/", auth=("ankit", "wrong"))
        assert response.status_code == 401


def test_dashboard_accepts_the_right_token(engine):
    config = Config()
    config.dashboard.token = "s3cret-token"
    store = engine.store
    with _app_and_client(config, engine, store) as client:
        response = client.get("/", auth=("ankit", "s3cret-token"))
        assert response.status_code == 200
        assert "kittybot" in response.text


def test_auth_covers_the_write_routes_too(engine):
    """The approve endpoint must not be reachable without the token."""
    config = Config()
    config.dashboard.token = "s3cret-token"
    draft = _proposal(engine, "auth-1")
    before = len(engine.client.list_expenses())
    with _app_and_client(config, engine, engine.store) as client:
        response = client.post(f"/proposal/{draft.id}/approve", data={"split": "50/50"})
        assert response.status_code == 401
    # And nothing was written.
    assert len(engine.client.list_expenses()) == before


# ----------------------------------------------------------------------
# Marketplace parsers against captured fixtures
# ----------------------------------------------------------------------
def test_blinkit_parses_the_captured_order_history_shape():
    """The legacy SDUI history shape from Blinkit's own bundle."""
    from kittybot.sources.blinkit import _parse_order_refs

    payload = {
        "snippets": [
            {
                "widget_type": "order_history_container_vr",
                "tracking": {
                    "common_attributes": {
                        "order_id": "3866907189",
                        "order_status": "DELIVERED",
                        "deeplink": "grofers://widgetized/order_details_v2?order_id=3866907189&cart_id=2638799842",
                    }
                },
                "data": {
                    "items": [
                        {
                            "widget_type": "image_text_vr_type_header",
                            "data": {
                                "title": {"text": "Arrived in 12 minutes"},
                                "left_underlined_subtitle": {"text": "₹1,234"},
                                "subtitle": {"text": "09 Oct, 9:32 am"},
                            },
                        }
                    ]
                },
            }
        ]
    }
    refs = _parse_order_refs(payload["snippets"])
    assert len(refs) == 1
    ref = refs[0]
    assert ref["order_id"] == "3866907189"
    assert ref["cart_id"] == "2638799842"
    assert ref["status"] == "DELIVERED"
    assert ref["amount"] == Decimal("1234")
    assert ref["placed_at"]


def test_blinkit_parses_the_current_order_items_widget():
    """widgetType 90: data.order_items[] with product_name/final_price."""
    from kittybot.sources.blinkit import _parse_order_detail

    objects = [
        {
            "widget_type": 90,
            "data": {
                "order_items": [
                    {
                        "product_name": "Amul Taaza Toned Milk",
                        "unit": "500 ml",
                        "final_quantity": 2,
                        "final_price": 54,
                    },
                    {
                        "product_name": "Tata Salt",
                        "unit": "1 kg",
                        "final_quantity": 1,
                        "final_price": 28,
                    },
                ],
                "heading": "2 items in this order",
            },
        }
    ]
    items, total, placed = _parse_order_detail(objects)
    assert [i.name for i in items] == ["Amul Taaza Toned Milk", "Tata Salt"]
    assert items[0].quantity == Decimal(2)
    assert items[0].price == Decimal("54")


def test_blinkit_parses_the_older_order_details_widget():
    """widget_name == order_details_list: title / subtitle1 / subtitle3."""
    from kittybot.sources.blinkit import _parse_order_detail

    objects = [
        {
            "widget_type": "z_v3_image_text_snippet_type_30",
            "tracking": {"widget_meta": {"widget_name": "order_details_list"}},
            "data": {
                "title": "Amul Taaza Toned Milk",
                "subtitle1": "500 ml x 1",
                "subtitle3": "₹27",
            },
        }
    ]
    items, _total, _placed = _parse_order_detail(objects)
    assert len(items) == 1
    assert items[0].name == "Amul Taaza Toned Milk"
    assert items[0].quantity == Decimal(1)
    assert items[0].price == Decimal("27")


def test_blinkit_parses_the_bill_widget():
    """widgetType 104: total_amount / payable_amount."""
    from kittybot.sources.blinkit import _parse_order_detail

    objects = [
        {
            "widget_type": 104,
            "data": {
                "total_amount": 1234,
                "payable_amount": 1250,
                "bill_details": [{"type": "delivery", "value": 16}],
            },
        }
    ]
    _items, total, _placed = _parse_order_detail(objects)
    assert total == Decimal("1250")


def test_blinkit_parses_the_checkout_time_widget():
    """widgetType 88: checkout_time carries the year the list omits."""
    from kittybot.sources.blinkit import _parse_order_detail

    objects = [
        {
            "widget_type": 88,
            "data": {"checkout_time": "placed on Sun, 13 Sep'26, 9:58 PM"},
        }
    ]
    _items, _total, placed = _parse_order_detail(objects)
    assert placed is not None
    assert placed.startswith("2026-09-13")


def test_the_blinkit_connect_diagnostic_names_missing_fields(capsys):
    """A broken session import must say what is missing, not just "Connected"."""
    from kittybot.cli import _report_blinkit_session

    _report_blinkit_session(
        {
            "access_token": "CREDENTIALVALUEAAA",
            "auth_key": "",
            "device_id": "dev",
            "session_uuid": "sess",
            "user_agent": "Mozilla/5.0",
            "app_version_v1": "1010101010",
            "lat": 18.52,
            "lon": 73.85,
            "cookies": {},
        }
    )
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "auth key" in out
    assert "gr_1 cookies" in out
    assert "WARNING" in out
    # A credential must never reach the terminal, not even truncated.
    assert "CREDENTIALVALUEAAA" not in out
    assert "(not shown)" in out


def test_the_blinkit_connect_diagnostic_passes_a_good_session(capsys):
    from kittybot.cli import _report_blinkit_session

    _report_blinkit_session(
        {
            "access_token": "CREDENTIALVALUEAAA",
            "auth_key": "CREDENTIALVALUEBBB",
            "device_id": "dev",
            "session_uuid": "sess",
            "user_agent": "Mozilla/5.0",
            "app_version_v1": "1010101010",
            "lat": 18.52,
            "lon": 73.85,
            "cookies": {"gr_1_a": "1"},
        }
    )
    out = capsys.readouterr().out
    assert "MISSING" not in out
    assert "WARNING" not in out
    assert "Everything Blinkit needs is present" in out
    # Even a good session must not print its credentials.
    assert "CREDENTIALVALUEAAA" not in out
    assert "CREDENTIALVALUEBBB" not in out


def test_the_bigbasket_connect_diagnostic_names_missing_fields(capsys):
    from kittybot.cli import _report_bigbasket_session

    _report_bigbasket_session(
        {
            "member_id": "MEMBERVALUE123",
            "csrf_token": "",
            "entry_context_id": "",
            "user_agent": "Mozilla/5.0",
            "cookies": {"_bb_mid": "MEMBERVALUE123"},
        }
    )
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "csrf token" in out
    assert "WARNING" in out


def test_cancelled_orders_are_recognised():
    """A cancelled or refunded order is not spend, in any adapter's shape."""
    from kittybot.sources.base import is_cancelled_order

    assert is_cancelled_order({"status": "CANCELLED"})
    assert is_cancelled_order({"status": "cancelled"})
    assert is_cancelled_order({"order_status": "CANCELLED"})
    assert is_cancelled_order({"orderStatus": "CANCELLED_BY_USER"})
    assert is_cancelled_order({"state": "REFUNDED"})
    assert is_cancelled_order({"status": "RETURNED"})
    assert is_cancelled_order({"status": "DELIVERY_FAILED"})
    assert is_cancelled_order({"refund_details": {"total_amount_refunded": 250.0}})
    assert is_cancelled_order({"refund": 100})
    assert is_cancelled_order({"refund": 0}) is False

    # A delivered order is spend.
    assert not is_cancelled_order({"status": "DELIVERED"})
    assert not is_cancelled_order({"status": "COMPLETED"})
    assert not is_cancelled_order({})
    assert not is_cancelled_order(None)


def test_instamart_drops_cancelled_orders():
    """The Instamart adapter must not import a cancelled order."""
    from kittybot.sources.instamart import InstamartSource

    source = InstamartSource.__new__(InstamartSource)
    source._call = lambda tool, arguments: {
        "orders": [
            {
                "orderId": "keep-1",
                "status": "DELIVERED",
                "totalAmount": 500,
                "items": [{"name": "Milk", "quantity": 1, "price": 500}],
            },
            {
                "orderId": "drop-1",
                "status": "CANCELLED",
                "totalAmount": 300,
                "items": [{"name": "Bread", "quantity": 1, "price": 300}],
            },
        ]
    }

    import asyncio

    orders = asyncio.run(source.fetch_orders())
    assert [o.order_id for o in orders] == ["keep-1"]


def test_zepto_drops_cancelled_orders():
    """The Zepto adapter must not import a cancelled order."""
    from kittybot.sources.zepto import _parse_order

    assert _parse_order({"order_id": "z-1", "status": "CANCELLED", "total": 100}) is None
    assert _parse_order({"order_id": "z-2", "status": "DELIVERED", "total": 100}) is not None


def test_bigbasket_parses_the_captured_detail_shape():
    """The order detail shape from BigBasket's live Next.js bundle."""
    from kittybot.sources.bigbasket import _parse_bb_date, _reconciles

    order = {
        "order_id": "1000101534",
        "items": [
            {
                "line_items": [
                    {
                        "desc": "Gold Glucose Biscuits",
                        "brand": "Parle-G",
                        "weight": "1 kg",
                        "quantity": 2,
                        "actual_quantity": 2,
                        "sp": 30.0,
                        "total": 60.0,
                    }
                ]
            }
        ],
        "charges": [{"description": {"display_name": "Delivery Charge"}, "charge": 29.0}],
        "sub_total": 60.0,
        "delivery_charge": 29.0,
        "total_with_charges": 89.0,
        "total_payable_with_charges": 89.0,
        "wallet_used": 0.0,
        "neucoins_used": 0.0,
        "created_on": "2020-04-01 10:22:31",
    }
    items = [
        LineItem(name="Gold Glucose Biscuits", quantity=Decimal(2), price=Decimal("30"))
    ]
    assert _reconciles(order, items)
    assert _parse_bb_date("2020-04-01 10:22:31").startswith("2020-04-01")


def test_bigbasket_reconciliation_rejects_a_waived_charge_mismatch():
    """A bill that does not add up must refuse the order, not fudge it."""
    from kittybot.sources.bigbasket import _reconciles

    order = {
        "items": [
            {"line_items": [{"desc": "X", "sp": 10.0, "total": 10.0, "quantity": 1}]}
        ],
        "sub_total": 10.0,
        "charges": [{"charge": 29.0}],
        # The total says 25 but subtotal + charges is 39.
        "total_with_charges": 25.0,
        "total_payable_with_charges": 25.0,
        "wallet_used": 0.0,
        "neucoins_used": 0.0,
    }
    items = [LineItem(name="X", quantity=Decimal(1), price=Decimal("10"))]
    assert _reconciles(order, items) is False


def test_zepto_parses_a_captured_order_shape():
    """Zepto's order list shape, with paise prices."""
    from kittybot.sources.zepto import _parse_order

    raw = {
        "order_id": "Z123",
        "total": 124800,
        "placed_at": "2026-10-28T18:30:00",
        "items": [
            {"name": "Amul Butter", "quantity": 2, "selling_price": 28500},
            {"name": "Potatoes", "quantity": 1, "selling_price": 12000},
        ],
    }
    order = _parse_order(raw)
    assert order is not None
    assert order.order_id == "Z123"
    assert [i.name for i in order.items] == ["Amul Butter", "Potatoes"]
    assert order.items[0].quantity == Decimal(2)


def test_price_normalisation_handles_every_shape():
    from kittybot.sources.base import normalise_price

    assert normalise_price("₹1,234.50") == Decimal("1234.50")
    assert normalise_price("Rs. 120") == Decimal("120")
    assert normalise_price(285) == Decimal("285.00")
    assert normalise_price("") is None
    assert normalise_price(None) is None
    assert normalise_price("no digits") is None
    assert normalise_price(".50") == Decimal("0.50")
    assert normalise_price("₹ 1,234.50") == Decimal("1234.50")
    assert normalise_price("INR 99") == Decimal("99.00")
