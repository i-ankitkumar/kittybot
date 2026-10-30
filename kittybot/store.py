"""SQLite persistence for proposals and their decisions.

The database is the audit trail: every market order that was seen, the split
that was proposed, what the user decided, and the Kittysplit entry it
produced. Nothing reaches Kittysplit without a row here saying a human
approved it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import ApprovalStatus, ExpenseDraft, Share, Split

DEFAULT_DB_PATH = Path.home() / ".config" / "kittybot" / "kittybot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT UNIQUE,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    description     TEXT NOT NULL,
    amount          TEXT NOT NULL,
    paid_by         TEXT NOT NULL,
    split_mode      TEXT NOT NULL,
    split_json      TEXT NOT NULL,
    date            TEXT NOT NULL,
    category_id     INTEGER,
    items_json      TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    kitty_entry_id  INTEGER,
    kitty_url       TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS orders_seen (
    dedupe_key   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    order_id     TEXT NOT NULL,
    total        TEXT NOT NULL,
    placed_at    TEXT,
    raw_json     TEXT NOT NULL DEFAULT '{}',
    seen_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_source ON proposals(source);
"""


class Store:
    """A thin SQLite wrapper for proposals and seen orders.

    Each thread gets its own SQLite connection. A single shared connection
    is not safe: concurrent ``execute()`` calls on one connection interleave
    their cursors and SQLite raises ``InterfaceError: bad parameter or other
    API misuse``, which silently breaks the atomic claim that stops a
    double-submit from writing an expense twice. Separate connections plus
    WAL give real concurrency and correct compare-and-set semantics.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()

        if str(self.path) == ":memory:":
            # Every thread must see the same in-memory database, so use a
            # named shared-cache URI and keep one connection alive for the
            # lifetime of the store.
            self._uri = f"file:kittybot-{id(self)}?mode=memory&cache=shared"
            self._keepalive = sqlite3.connect(self._uri, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._uri = str(self.path)

        conn = self._conn
        conn.executescript(SCHEMA)
        conn.commit()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._uri, check_same_thread=False, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 15000")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        keepalive = getattr(self, "_keepalive", None)
        if keepalive is not None:
            try:
                keepalive.close()
            except Exception:
                pass
            self._keepalive = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # proposals
    # ------------------------------------------------------------------
    def add_proposal(self, draft: ExpenseDraft) -> tuple[ExpenseDraft, bool]:
        """Store a proposal. Returns (draft, created).

        ``created`` is False when the same order was already proposed, which
        is how repeated syncs avoid double-counting.
        """
        if draft.dedupe_key:
            existing = self.get_by_dedupe_key(draft.dedupe_key)
            if existing:
                return existing, False

        now = datetime.now().isoformat()
        cursor = self._conn.execute(
            """
            INSERT INTO proposals (
                dedupe_key, source, source_ref, description, amount, paid_by,
                split_mode, split_json, date, category_id, items_json,
                status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                draft.dedupe_key,
                draft.source,
                draft.source_ref,
                draft.description,
                str(draft.amount),
                draft.paid_by,
                draft.split.mode,
                json.dumps(_split_to_json(draft.split)),
                draft.date,
                draft.category_id,
                json.dumps(
                    [
                        {"name": i.name, "quantity": str(i.quantity), "price": str(i.price)}
                        for i in draft.items
                    ]
                ),
                draft.status.value,
                now,
            ),
        )
        self._conn.commit()
        draft.id = cursor.lastrowid  # type: ignore[attr-defined]
        return draft, True

    def get_proposal(self, proposal_id: int) -> ExpenseDraft | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return _row_to_draft(row) if row else None

    def get_by_dedupe_key(self, key: str) -> ExpenseDraft | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE dedupe_key = ?", (key,)
        ).fetchone()
        return _row_to_draft(row) if row else None

    def list_proposals(
        self, status: ApprovalStatus | str | None = None, limit: int = 50
    ) -> list[ExpenseDraft]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            value = status.value if isinstance(status, ApprovalStatus) else status
            rows = self._conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
                (value, limit),
            ).fetchall()
        return [_row_to_draft(row) for row in rows]

    def pending_proposals(self, limit: int = 50) -> list[ExpenseDraft]:
        return self.list_proposals(ApprovalStatus.PENDING, limit)

    def claim_proposal(self, proposal_id: int) -> bool:
        """Atomically claim a pending proposal for writing.

        Returns True for exactly one caller. The UPDATE's WHERE clause is
        what makes it safe: two concurrent approvers both see
        ``status = 'pending'``, but only one UPDATE can match the row, so
        only one of them ever writes the expense.
        """
        cursor = self._conn.execute(
            """
            UPDATE proposals
               SET status = ?, decided_at = ?
             WHERE id = ? AND status = ?
            """,
            (
                ApprovalStatus.APPROVING.value,
                datetime.now().isoformat(),
                proposal_id,
                ApprovalStatus.PENDING.value,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def release_stale_claims(self, max_age_seconds: int = 300) -> int:
        """Return proposals stuck in ``approving`` back to ``pending``.

        If the process died mid-write the proposal would otherwise be stuck
        forever. Anything claimed longer than ``max_age_seconds`` ago is
        treated as abandoned.
        """
        cutoff = (
            datetime.now() - timedelta(seconds=max_age_seconds)
        ).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE proposals
               SET status = ?, decided_at = NULL
             WHERE status = ? AND decided_at < ?
            """,
            (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVING.value, cutoff),
        )
        self._conn.commit()
        return cursor.rowcount

    def decide(
        self,
        proposal_id: int,
        status: ApprovalStatus,
        *,
        split: Split | None = None,
        kitty_entry_id: int | None = None,
        kitty_url: str | None = None,
        error: str | None = None,
    ) -> ExpenseDraft | None:
        """Record the human decision."""
        fields = ["status = ?", "decided_at = ?"]
        values: list[object] = [status.value, datetime.now().isoformat()]
        if split is not None:
            fields.append("split_mode = ?")
            values.append(split.mode)
            fields.append("split_json = ?")
            values.append(json.dumps(_split_to_json(split)))
        if kitty_entry_id is not None:
            fields.append("kitty_entry_id = ?")
            values.append(kitty_entry_id)
        if kitty_url is not None:
            fields.append("kitty_url = ?")
            values.append(kitty_url)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        values.append(proposal_id)
        self._conn.execute(
            f"UPDATE proposals SET {', '.join(fields)} WHERE id = ?", values
        )
        self._conn.commit()
        return self.get_proposal(proposal_id)

    def delete_proposal(self, proposal_id: int) -> None:
        self._conn.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # seen orders
    # ------------------------------------------------------------------
    def mark_order_seen(
        self,
        dedupe_key: str,
        source: str,
        order_id: str,
        total: Decimal,
        placed_at: str | None,
        raw: dict | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO orders_seen
                (dedupe_key, source, order_id, total, placed_at, raw_json, seen_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                dedupe_key,
                source,
                order_id,
                str(total),
                placed_at,
                json.dumps(raw or {}, default=str),
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()

    def has_seen_order(self, dedupe_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM orders_seen WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row is not None


# ----------------------------------------------------------------------
# (de)serialisation helpers
# ----------------------------------------------------------------------
def _split_to_json(split: Split) -> list[dict]:
    return [
        {
            "participant": share.participant,
            "weight": str(share.weight) if share.weight is not None else None,
            "percent": str(share.percent) if share.percent is not None else None,
            "amount": str(share.amount) if share.amount is not None else None,
            "involved": share.involved,
        }
        for share in split.shares
    ]


def _split_from_json(data: list[dict]) -> Split:
    shares = []
    for entry in data:
        shares.append(
            Share(
                participant=entry["participant"],
                weight=Decimal(entry["weight"]) if entry.get("weight") else None,
                percent=Decimal(entry["percent"]) if entry.get("percent") else None,
                amount=Decimal(entry["amount"]) if entry.get("amount") else None,
                involved=bool(entry.get("involved", True)),
            )
        )
    return Split(mode="equal", shares=shares)


def _row_to_draft(row: sqlite3.Row) -> ExpenseDraft:
    from .models import LineItem

    split_data = json.loads(row["split_json"])
    split = _split_from_json(split_data)
    split.mode = row["split_mode"]

    items = [
        LineItem(
            name=item["name"],
            quantity=Decimal(item.get("quantity", "1")),
            price=Decimal(item["price"]) if item.get("price") else None,
        )
        for item in json.loads(row["items_json"] or "[]")
    ]

    draft = ExpenseDraft(
        description=row["description"],
        amount=Decimal(row["amount"]),
        paid_by=row["paid_by"],
        split=split,
        date=row["date"],
        category_id=row["category_id"],
        source=row["source"],
        source_ref=row["source_ref"],
        items=items,
        status=ApprovalStatus(row["status"]),
        kitty_entry_id=row["kitty_entry_id"],
        error=row["error"],
    )
    draft.id = row["id"]  # type: ignore[attr-defined]
    draft.kitty_url = row["kitty_url"]  # type: ignore[attr-defined]
    return draft
