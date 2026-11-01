"""Long-running behaviour: the database grows, and OAuth tokens expire.

Nothing in the project had been observed past the first hour. These are
the two things that actually change over a week: the proposal store grows,
and the marketplace OAuth tokens stop being valid.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from kittybot.mcp import load_token, save_token
from kittybot.models import ApprovalStatus, Share, Split
from kittybot.sources.instamart import InstamartSource
from kittybot.sources.zepto import ZeptoSource
from kittybot.store import Store


def _draft(index: int):
    from kittybot.models import ExpenseDraft

    return ExpenseDraft(
        description=f"Order {index}",
        amount=Decimal("100.00") + (index % 500),
        paid_by="Ankit",
        split=Split(mode="equal", shares=[Share(participant="Ankit")]),
        date="2026-11-01",
    )


# ----------------------------------------------------------------------
# The store over a week of use
# ----------------------------------------------------------------------
def test_the_store_survives_ten_thousand_proposals(tmp_path):
    """A week of syncing is a few hundred proposals, but nothing grows
    without bound. Measure the growth and the query time at the end."""
    db = tmp_path / "grow.db"
    store = Store(db)

    started = time.perf_counter()
    for index in range(10_000):
        store.add_proposal(_draft(index))
    write_time = time.perf_counter() - started

    size_kb = db.stat().st_size / 1024
    assert size_kb < 40_000, f"the database grew to {size_kb / 1024:.1f} MB"

    started = time.perf_counter()
    pending = store.pending_proposals()
    pending_time = time.perf_counter() - started

    started = time.perf_counter()
    recent = store.list_proposals(limit=20)
    recent_time = time.perf_counter() - started

    assert len(recent) == 20
    assert pending_time < 1.0, f"pending_proposals took {pending_time:.2f}s"
    assert recent_time < 0.5, f"list_proposals took {recent_time:.2f}s"
    # A write is one fsync, so it costs a few milliseconds. What matters is
    # the cost of a realistic sync: one store's worth of orders.
    per_write_ms = write_time * 1000 / 10_000
    assert per_write_ms < 25.0, f"a write took {per_write_ms:.1f}ms"
    assert write_time < 250.0, f"10,000 writes took {write_time:.1f}s"

    print(
        f"\n  10,000 proposals: db {size_kb / 1024:.1f} MB, "
        f"writes {write_time:.1f}s, pending query {pending_time * 1000:.0f}ms, "
        f"recent query {recent_time * 1000:.0f}ms, {len(pending)} pending"
    )
    store.close()


def test_the_store_drops_old_decided_proposals(tmp_path):
    """A month of proposals is noise. There has to be a way to clear it."""
    store = Store(tmp_path / "prune.db")
    for index in range(10):
        store.add_proposal(_draft(index))
    # The first five are decided, the last five are still waiting.
    for index in range(1, 6):
        store.decide(index, ApprovalStatus.APPROVED)
    assert len(store.list_proposals(limit=50)) == 10

    removed = store.prune_proposals(keep=2)
    assert removed == 3, "the three oldest decided proposals should go"
    remaining = store.list_proposals(limit=50)
    assert len(remaining) == 7
    # The pending ones are work waiting on a human. They stay.
    pending = store.pending_proposals()
    assert len(pending) == 5
    store.close()


def test_the_store_can_drop_stale_pending_proposals(tmp_path):
    """Nobody ever acted on them. After a month they are not going to."""
    store = Store(tmp_path / "prune2.db")
    for index in range(5):
        store.add_proposal(_draft(index))
    # Backdate them by 40 days.
    store._conn.execute(
        "UPDATE proposals SET decided_at = '2026-09-22T00:00:00' WHERE id > 0"
    )
    store._conn.commit()

    # The count alone does not touch pending proposals.
    assert store.prune_proposals(keep=100) == 0
    assert len(store.pending_proposals()) == 5

    # An age limit does.
    removed = store.prune_proposals(keep=100, max_pending_age_days=30)
    assert removed == 5
    assert store.pending_proposals() == []
    store.close()


# ----------------------------------------------------------------------
# OAuth tokens expire
# ----------------------------------------------------------------------
def test_the_token_file_is_not_world_readable(tmp_path):
    path = tmp_path / "token.json"
    save_token(path, {"access_token": "secret-value"})
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"the token file is mode {oct(mode)}"
    # And it must not be readable by anyone else.
    assert not (mode & 0o077)


def test_an_expired_token_without_a_refresh_token_says_so(tmp_path):
    """The error has to tell the user to reconnect, not to retry."""
    token_file = tmp_path / "token.json"
    save_token(
        token_file,
        {
            "access_token": "old",
            "expires_at": time.time() - 3600,
            # no refresh_token
        },
    )
    source = InstamartSource(token_file=token_file)
    with pytest.raises(Exception) as exc:
        source._access_token()
    message = str(exc.value)
    assert "reconnect" in message.lower()
    assert "refresh" in message.lower()


def test_an_expired_token_with_a_refresh_token_is_refreshed(tmp_path, monkeypatch):
    """The happy path: an expired token is renewed without the user
    doing anything."""
    import kittybot.sources.instamart as instamart_module

    token_file = tmp_path / "token.json"
    save_token(
        token_file,
        {
            "access_token": "old",
            "refresh_token": "refresh-me",
            "client_id": "client-abc",
            "expires_at": time.time() - 3600,
        },
    )

    calls = {}

    def fake_refresh(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return {
            "access_token": "fresh",
            "refresh_token": "refresh-me-2",
            "expires_in": 3600,
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(instamart_module, "refresh_token", fake_refresh)

    source = InstamartSource(token_file=token_file)
    assert source._access_token() == "fresh"
    assert calls["url"].endswith("/token")
    assert calls["refresh"] == "refresh-me"

    # The new token is persisted, still mode 600.
    saved = load_token(token_file)
    assert saved["access_token"] == "fresh"
    assert saved["refresh_token"] == "refresh-me-2"
    assert (token_file.stat().st_mode & 0o777) == 0o600


def test_a_valid_token_is_not_refreshed(tmp_path, monkeypatch):
    import kittybot.sources.instamart as instamart_module

    token_file = tmp_path / "token.json"
    save_token(
        token_file,
        {
            "access_token": "still-good",
            "refresh_token": "refresh-me",
            "client_id": "client-abc",
            "expires_at": time.time() + 3600,
        },
    )

    def explode(*args, **kwargs):
        raise AssertionError("a valid token must not be refreshed")

    monkeypatch.setattr(instamart_module, "refresh_token", explode)
    source = InstamartSource(token_file=token_file)
    assert source._access_token() == "still-good"


def test_zepto_expired_token_also_says_reconnect(tmp_path):
    token_file = tmp_path / "token.json"
    save_token(
        token_file,
        {"access_token": "old", "expires_at": time.time() - 3600},
    )
    source = ZeptoSource(token_file=token_file)
    with pytest.raises(Exception) as exc:
        source._access_token()
    assert "reconnect" in str(exc.value).lower()


def test_a_token_with_no_access_token_says_so(tmp_path):
    token_file = tmp_path / "token.json"
    save_token(token_file, {"expires_at": time.time() + 3600})
    source = InstamartSource(token_file=token_file)
    with pytest.raises(Exception) as exc:
        source._access_token()
    assert "reconnect" in str(exc.value).lower()


def test_a_missing_token_file_says_so(tmp_path):
    source = InstamartSource(token_file=tmp_path / "nothing.json")
    with pytest.raises(Exception) as exc:
        source._access_token()
    message = str(exc.value)
    assert "not connected" in message.lower()
    assert "connect instamart" in message


def test_a_corrupt_token_file_does_not_crash_the_bot(tmp_path):
    """A truncated token file must produce a clear error, not a traceback
    from deep inside json parsing."""
    token_file = tmp_path / "token.json"
    token_file.write_text("{not json at all")
    source = InstamartSource(token_file=token_file)
    with pytest.raises(Exception) as exc:
        source._access_token()
    assert "not connected" in str(exc.value).lower() or "reconnect" in str(exc.value).lower()


def test_the_config_directory_is_not_world_readable(tmp_path):
    """Where the tokens and sessions live."""
    from kittybot.config import Config

    path = tmp_path / "config.toml"
    config = Config()
    config.telegram.bot_token = "secret-bot-token"
    config.kitty.kitty_url = "https://www.kittysplit.com/k/secret-1"
    saved = config.save(path)
    assert saved.exists()
    mode = saved.stat().st_mode & 0o777
    assert mode == 0o600, f"the config file is mode {oct(mode)}"
    # And the token really is in it, in a file that is not readable by
    # anyone else.
    assert "secret-bot-token" in saved.read_text()
