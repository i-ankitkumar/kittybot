"""Regression tests for the dashboard bugs found in the dogfood QA pass.

Each test names the issue it guards against. The redirect tests follow
redirects the way a browser does, because the original bug lived in a
redirect the old suite never followed.
"""

from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from kittybot.approvals import ApprovalEngine
from kittybot.config import Config
from kittybot.dashboard.app import create_app, scrub, source_label
from kittybot.kittysplit import BASE_URL, USER_AGENT, KittysplitClient
from kittybot.models import ApprovalStatus, LineItem, MarketOrder
from kittybot.sources.base import build_order
from kittybot.store import Store


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
                "kitty[name]": "kittybot dashboard test",
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
def stack(live_kitty, tmp_path):
    client = KittysplitClient(live_kitty)
    client.ensure_identity("Ankit")
    store = Store(tmp_path / "dash.db")
    engine = ApprovalEngine(client, store)

    config = Config()
    config.kitty.kitty_url = live_kitty
    config.kitty.my_name = "Ankit"

    app = create_app(config, engine, store)
    # TestClient follows redirects the way a browser does by default, which is
    # exactly what the old suite did NOT do and what hid the 307 bug.
    with TestClient(app, follow_redirects=True) as http:
        yield {"http": http, "engine": engine, "store": store, "client": client, "config": config}
    store.close()
    client.close()


def _order(order_id="d-1", total="1000.00", items=None) -> MarketOrder:
    return build_order(
        source="instamart",
        order_id=order_id,
        total=Decimal(total),
        items=items
        or [LineItem(name="Amul Butter 500g", quantity=Decimal(2), price=Decimal("285"))],
        placed_at="2026-10-28T18:30:00",
    )


# ----------------------------------------------------------------------
# Issue #1 — the redirect must not preserve the POST method
# ----------------------------------------------------------------------
def test_approve_redirects_with_get_not_post(stack):
    """A browser following the redirect must land on the pending page.

    The bug: RedirectResponse defaults to 307, which preserves the POST
    method, so the browser re-POSTed to a GET-only route and rendered a raw
    JSON 405 page instead of the app.
    """
    draft, _ = stack["engine"].propose_order(_order("r-1"), payer="Ankit")
    response = stack["http"].post(f"/proposal/{draft.id}/approve", data={"split": "50/50"})

    assert response.status_code == 200
    assert "Method Not Allowed" not in response.text
    # The dashboard, not a JSON error page.
    assert "kittybot" in response.text
    assert "pending" in response.text.lower()


def test_reject_redirects_with_get_not_post(stack):
    draft, _ = stack["engine"].propose_order(_order("r-2"), payer="Ankit")
    response = stack["http"].post(f"/proposal/{draft.id}/reject")

    assert response.status_code == 200
    assert "Method Not Allowed" not in response.text
    assert "kittybot" in response.text


def test_redirect_uses_see_other(stack):
    """The raw redirect must be a 303, not a method-preserving 307."""
    draft, _ = stack["engine"].propose_order(_order("r-3"), payer="Ankit")
    with TestClient(
        create_app(stack["config"], stack["engine"], stack["store"]), follow_redirects=False
    ) as http:
        response = http.post(f"/proposal/{draft.id}/approve", data={"split": "50/50"})
    assert response.status_code == 303
    assert response.headers["location"] == "/pending"


# ----------------------------------------------------------------------
# Issue #3 and #4 — failures must be visible
# ----------------------------------------------------------------------
def test_invalid_split_shows_an_error_message(stack):
    """An unreadable split must not fail silently."""
    draft, _ = stack["engine"].propose_order(_order("e-1"), payer="Ankit")
    response = stack["http"].post(f"/proposal/{draft.id}/approve", data={"split": "banana"})

    assert response.status_code == 200
    assert "banana" in response.text or "split" in response.text.lower()
    # An error is rendered, not a silent reload.
    assert "did not work" in response.text.lower()
    # The proposal is still pending, and the reason is recorded.
    stored = stack["store"].get_proposal(draft.id)
    assert stored.error, "the failure reason must be recorded on the proposal"


def test_success_shows_a_confirmation(stack):
    draft, _ = stack["engine"].propose_order(_order("e-2"), payer="Ankit")
    response = stack["http"].post(f"/proposal/{draft.id}/approve", data={"split": "50/50"})

    assert response.status_code == 200
    assert "done" in response.text.lower()
    stored = stack["store"].get_proposal(draft.id)
    assert stored.status is ApprovalStatus.APPROVED, stored.error


def test_failed_proposal_shows_its_error_on_the_overview(stack):
    """A failed proposal must display why it failed."""
    draft = stack["engine"].propose_manual(
        Decimal("77.00"),
        "Broken proposal",
        paid_by="Nobody",
        split=stack["client"].equal_split(["Ankit"]),
    )
    stack["store"].decide(
        draft.id, ApprovalStatus.FAILED, error="unknown payer 'Nobody'"
    )
    response = stack["http"].get("/")
    assert "unknown payer" in response.text


def test_flash_message_does_not_leak_the_kitty_url(stack):
    """The kitty link is a credential; it must not appear in a message."""
    secret = "https://www.kittysplit.com/k/SUPERSECRETKEY-123"
    assert scrub(f"failed reading {secret}") == "failed reading [kitty link]"
    assert "SUPERSECRETKEY" not in scrub(f"failed reading {secret}")


def test_flash_is_not_in_the_url(stack):
    """Flash messages travel in a cookie, not the query string."""
    draft, _ = stack["engine"].propose_order(_order("e-3"), payer="Ankit")
    with TestClient(
        create_app(stack["config"], stack["engine"], stack["store"]), follow_redirects=False
    ) as http:
        response = http.post(f"/proposal/{draft.id}/approve", data={"split": "banana"})
    assert "banana" not in response.headers.get("location", "")


# ----------------------------------------------------------------------
# Issue #2 — the payer name must show
# ----------------------------------------------------------------------
def test_expense_rows_show_the_payer_name(stack):
    """Every expense row must name who paid."""
    draft = stack["engine"].propose_manual(
        Decimal("100.00"),
        "Payer check",
        paid_by="Ankit",
        split=stack["client"].equal_split(["Ankit", "Yuvraj"]),
    )
    stack["engine"].approve(draft.id)

    response = stack["http"].get("/")
    assert "Ankit paid" in response.text
    # The old bug rendered a bare " paid" with no subject.
    assert re.search(r"[^A-Za-z] paid &middot; your share", response.text) is None


# ----------------------------------------------------------------------
# Issue #8 — item lists must not silently truncate
# ----------------------------------------------------------------------
def test_pending_list_says_how_many_items_are_hidden(stack):
    items = [
        LineItem(name=f"Item number {i}", quantity=Decimal(1), price=Decimal(10))
        for i in range(1, 16)
    ]
    stack["engine"].propose_order(_order("t-1", "150.00", items), payer="Ankit")
    response = stack["http"].get("/pending")
    assert "more item" in response.text.lower()


# ----------------------------------------------------------------------
# Issues #5, #6, #7, #11, #13 — accessibility
# ----------------------------------------------------------------------
def test_pages_have_landmarks(stack):
    for path in ("/", "/pending"):
        body = stack["http"].get(path).text
        assert "<main>" in body, f"{path} has no main landmark"
        assert "<header" in body, f"{path} has no header landmark"
        assert "<footer" in body, f"{path} has no footer landmark"
        assert "<nav" in body, f"{path} has no nav landmark"


def test_split_input_has_a_label(stack):
    stack["engine"].propose_order(_order("a-1"), payer="Ankit")
    body = stack["http"].get("/pending").text
    assert '<label class="visually-hidden"' in body
    assert re.search(r'<label[^>]*for="split-\d+"', body)


def test_flash_region_is_announced(stack):
    draft, _ = stack["engine"].propose_order(_order("a-2"), payer="Ankit")
    body = stack["http"].post(f"/proposal/{draft.id}/approve", data={"split": "50/50"}).text
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body


def test_stylesheet_defines_a_visible_focus_ring(stack):
    css = stack["http"].get("/static/dashboard.css").text
    assert ":focus-visible" in css
    assert "outline: 2px solid" in css


def test_primary_button_colour_meets_wcag_aa(stack):
    """The Approve button must reach 4.5:1 against its own text colour."""
    css = stack["http"].get("/static/dashboard.css").text
    match = re.search(r"--accent-solid:\s*(#[0-9a-fA-F]{6})", css)
    assert match, "no --accent-solid token"
    accent = match.group(1)

    def luminance(hex_colour: str) -> float:
        h = hex_colour.lstrip("#")
        channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        channels = [
            c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
        ]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    l1 = luminance(accent)
    l2 = luminance("#ffffff")
    ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
    assert ratio >= 4.5, f"{accent} on white is {ratio:.2f}:1, needs 4.5:1"


# ----------------------------------------------------------------------
# Issue #12 — consistent source labels
# ----------------------------------------------------------------------
def test_source_labels_are_consistent():
    assert source_label("instamart") == "Instamart"
    assert source_label("bigbasket") == "BigBasket"
    assert source_label("manual") == "Manual"
    assert source_label("something-else") == "Something-Else"


def test_overview_uses_the_same_source_label_in_both_sections(stack):
    stack["engine"].propose_order(_order("s-1"), payer="Ankit")
    body = stack["http"].get("/").text
    assert "Instamart" in body
    assert "instamart" not in body


# ----------------------------------------------------------------------
# Issue #10 — long descriptions must not bury the actions
# ----------------------------------------------------------------------
def test_long_descriptions_are_clamped(stack):
    long_description = "Instamart: " + "a very long description " * 12
    stack["engine"].propose_order(
        _order("l-1"), payer="Ankit", description=long_description
    )
    body = stack["http"].get("/pending").text
    assert "clamp-3" in body
    assert f'title="{long_description}"' in body
