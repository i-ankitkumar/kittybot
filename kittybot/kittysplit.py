"""Read/write client for Kittysplit.

Kittysplit has no public REST API: the web app is a Phoenix server that
renders HTML and accepts classic form posts, and the mobile apps talk to an
internal ``/api`` surface that is version-gated behind
``client_upgrade_required``.

What does work, and is what this module uses:

* the secret kitty link is the only credential (``/k/<key>``),
* the "who are you" identity is a cookie called ``kittysplit_viewing_parties``
  holding base64 of ``[{"key": <kitty key>, "pid": <party id>}]``,
* expenses are created by POSTing the ``entries/new/expense`` form,
* expenses are listed at ``entries/`` with machine-readable ``data-*``
  attributes on every row,
* the whole kitty can be exported as XLSX at ``export/xlsx``.

Every endpoint used here was verified against a live kitty.
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .models import (
    Expense,
    KittySummary,
    Participant,
    Share,
    Split,
)

BASE_URL = "https://www.kittysplit.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Kittysplit's own category ids (from the entry form's category select).
CATEGORY_FOOD = 3
CATEGORY_HOUSEHOLD = 1
CATEGORY_UTILITIES = 6
CATEGORY_TRANSPORT = 5
CATEGORY_DINING = 4

_CSRF_RE = re.compile(r'name="_csrf_token"[^>]*value="([^"]+)"')
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


#: "Ankit gives ₹ 150.00 to Yuvraj". The amount is written with a space
#: between the symbol and the number, so the middle group has to allow one.
_SETTLEMENT_RE = re.compile(
    r"^(.+?)\s+gives\s+(.+?)\s+to\s+(.+?)(?:\s+Mark as settled)?\s*$", re.I
)


def _settlement_texts(soup: Any) -> list[tuple[str, str, str]]:
    """Pull the settlement plan out of the kitty page.

    Kittysplit renders each settlement as::

        <li class="transaction ks-data-row">
          <div class="transaction-text">Ankit gives ₹ 150.00 to Yuvraj</div>
          ... Mark as settled
        </li>

    The old parser looked for a text node matching ``gives <non-space> to``
    and then applied a regex that expected the amount to be a single token.
    Both assumptions were wrong: the amount is "₹ 150.00" with a space in
    it, so the search never matched and the settlement plan was always
    empty. Parse the structure instead, and keep a text fallback for the
    case where the class name changes.
    """
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for node in soup.select(".transaction-text"):
        text = " ".join(node.get_text(" ", strip=True).split())
        match = _SETTLEMENT_RE.match(text)
        if match and text not in seen:
            seen.add(text)
            found.append((match.group(1), match.group(2), match.group(3)))

    if not found:
        for node in soup.find_all(string=re.compile(r"\bgives\b", re.I)):
            parent = node.parent
            text = " ".join(parent.get_text(" ", strip=True).split()) if parent else ""
            match = _SETTLEMENT_RE.match(text)
            if match and text not in seen:
                seen.add(text)
                found.append((match.group(1), match.group(2), match.group(3)))
    return found


def _parse_money(text: str) -> Decimal | None:
    """Pull a number out of a currency blob.

    Kittysplit renders the symbol *inside* the amount span, e.g.
    ``<span class="currency"><span class="currency-symbol">₹</span>1,234.50</span>``
    so the raw text is "₹1,234.50". We take the first numeric run.
    """
    match = _NUMBER_RE.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except Exception:
        return None


def _money_in(node: Any, selector: str) -> Decimal | None:
    """Read the first matching currency span inside ``node``."""
    target = node.select_one(selector)
    if not target:
        return None
    return _parse_money(target.get_text(strip=True))


class KittysplitError(RuntimeError):
    """Raised when Kittysplit rejects or cannot serve a request."""


class KittysplitClient:
    """A logged-in view of one kitty.

    >>> client = KittysplitClient("https://www.kittysplit.com/k/AbCdEf-123")
    >>> client.participants()
    ['Ankit', 'Yuvraj']
    """

    def __init__(
        self,
        kitty_url: str,
        party_id: int | None = None,
        *,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.kitty_url = kitty_url.rstrip("/")
        parsed = urlparse(self.kitty_url)
        if not parsed.netloc:
            raise KittysplitError(f"not a valid kitty url: {kitty_url!r}")
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise KittysplitError(
                f"kitty url must contain /<group>/<key>, got {parsed.path!r}"
            )
        # The kitty key is the LAST path segment; the group slug is the one
        # before it. Verified: https://www.kittysplit.com/k/<key>
        self.kitty_key = path_parts[-1]
        self.kitty_slug = path_parts[-2]
        self.kitty_path = "/".join(path_parts)

        self._party_id = party_id
        self._http = client or httpx.Client(
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._owns_http = client is None

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> KittysplitClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str = "") -> str:
        return f"{self.base_url}/{self.kitty_path}{path}"

    def _csrf(self, html: str) -> str:
        match = _CSRF_RE.search(html)
        if not match:
            raise KittysplitError("could not find a CSRF token in the response")
        return match.group(1)

    def _set_identity(self, party_id: int) -> None:
        """Install the ``kittysplit_viewing_parties`` identity cookie.

        This is the trick the Kittysplit Scriptable widget uses and it avoids
        the "Who are you?" interstitial entirely.
        """
        payload = [{"key": self.kitty_key, "pid": int(party_id)}]
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self._http.cookies.set(
            "kittysplit_viewing_parties", encoded, domain="www.kittysplit.com", path="/"
        )
        self._http.cookies.set("_selected_locale", "ZW4=", domain="www.kittysplit.com", path="/")
        self._party_id = int(party_id)

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _party_map(self) -> dict[int, str]:
        """Map party id -> participant name, from the party chooser page."""
        response = self._http.get(self._url("/parties/choose"))
        if response.status_code != 200:
            raise KittysplitError(
                f"party chooser returned {response.status_code}; is the kitty link valid?"
            )
        soup = BeautifulSoup(response.text, "html.parser")
        result: dict[int, str] = {}
        for button in soup.select("button.set-viewing-party"):
            formaction = button.get("formaction") or ""
            match = re.search(r"/parties/(\d+)/set", formaction)
            if not match:
                continue
            result[int(match.group(1))] = button.get_text(strip=True)
        return result

    def participants(self) -> list[Participant]:
        """Everyone in the kitty."""
        return [Participant(name=name, party_id=pid) for pid, name in self._party_map().items()]

    def resolve_party_id(self, name: str) -> int:
        """Find a participant's party id by (case-insensitive) name."""
        parties = self._party_map()
        wanted = name.strip().lower()
        for pid, pname in parties.items():
            if pname.strip().lower() == wanted:
                return pid
        raise KittysplitError(
            f"no participant named {name!r}; kitty has: {', '.join(parties.values())}"
        )

    def participant_ids(self) -> dict[str, int]:
        return {name: pid for pid, name in self._party_map().items()}

    def ensure_identity(self, name: str | None = None) -> int:
        """Pick our viewing party so the kitty pages are usable.

        ``name`` selects which participant we are. Without it, the first
        participant is used.
        """
        parties = self._party_map()
        if not parties:
            raise KittysplitError("kitty has no participants")
        if name:
            wanted = name.strip().lower()
            chosen = next(
                (pid for pid, pname in parties.items() if pname.strip().lower() == wanted), None
            )
            if chosen is None:
                raise KittysplitError(
                    f"no participant named {name!r}; kitty has: {', '.join(parties.values())}"
                )
        else:
            chosen = next(iter(parties))
        self._set_identity(chosen)
        return chosen

    def _expense_rows(self) -> list[dict[str, Any]]:
        """Parse the expense list page into structured rows."""
        response = self._http.get(self._url("/entries/"))
        if response.status_code != 200:
            raise KittysplitError(f"entries page returned {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        rows: list[dict[str, Any]] = []
        for item in soup.select("li.entry-list-item"):
            link = item.select_one("a.entry-link")
            href = link.get("href") if link else ""
            entry_id = None
            match = re.search(r"/entries/(\d+)", href or "")
            if match:
                entry_id = int(match.group(1))

            amount = _money_in(item, ".entry-main .currency")
            your_share = _money_in(item, ".entry-label-share .currency")
            category = item.get("data-category-id")
            paid_by = item.get("data-paid-by-id")
            share_ids = item.get("data-share-party-ids") or ""
            rows.append(
                {
                    "entry_id": entry_id,
                    "url": f"{self.base_url}{href}" if href else None,
                    "description": (item.get("data-search") or "").strip(),
                    "amount": amount,
                    "your_share": your_share,
                    "category_id": int(category) if category and category.isdigit() else None,
                    "paid_by_id": int(paid_by) if paid_by and paid_by.isdigit() else None,
                    "share_party_ids": [
                        int(x) for x in share_ids.split(",") if x.strip().isdigit()
                    ],
                }
            )
        return rows

    def list_expenses(self) -> list[dict[str, Any]]:
        """All expenses in the kitty, newest first."""
        return self._expense_rows()

    def summary(self) -> KittySummary:
        """Totals and the optimal settlement plan."""
        response = self._http.get(self._url(""))
        if response.status_code != 200:
            raise KittysplitError(f"kitty page returned {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")

        title = soup.find("title")
        kitty_name = (title.get_text(strip=True).split(" - ")[0] if title else "") or "Kitty"

        symbol_node = soup.select_one(".kitty-stat-value .currency-symbol")
        symbol = symbol_node.get_text(strip=True) if symbol_node else ""

        values: list[Decimal] = []
        for stat in soup.select(".kitty-stat"):
            value_node = stat.select_one(".kitty-stat-value .currency")
            if not value_node:
                continue
            value = _parse_money(value_node.get_text(strip=True))
            if value is not None:
                values.append(value)

        total_cost = values[0] if len(values) > 0 else Decimal(0)
        your_cost = values[1] if len(values) > 1 else Decimal(0)
        you_paid = values[2] if len(values) > 2 else Decimal(0)

        owed = Decimal(0)
        for stat in soup.select(".kitty-stat"):
            title_node = stat.select_one(".kitty-stat-title")
            if title_node and "owed" in title_node.get_text(strip=True).lower():
                value_node = stat.select_one(".kitty-stat-value .currency")
                if value_node:
                    value = _parse_money(value_node.get_text(strip=True))
                    if value is not None:
                        owed = value

        settlements: list[tuple[str, str, Decimal]] = []
        for payer, amount_text, recipient in _settlement_texts(soup):
            amount = _parse_money(amount_text)
            if amount is not None:
                settlements.append((payer.strip(), recipient.strip(), amount))

        return KittySummary(
            kitty_name=kitty_name,
            currency_symbol=symbol,
            total_cost=total_cost,
            your_cost=your_cost,
            you_paid=you_paid,
            you_are_owed=owed,
            settlements=settlements,
        )

    def export_xlsx(self) -> bytes:
        """The kitty as a spreadsheet (expenses, balances, settlements)."""
        response = self._http.get(self._url("/export/xlsx"))
        if response.status_code != 200:
            raise KittysplitError(f"xlsx export returned {response.status_code}")
        return response.content

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def _split_payload(
        self,
        split: Split,
        party_ids: dict[str, int],
        total: Decimal,
    ) -> dict[str, str]:
        """Translate a :class:`Split` into Kittysplit's form fields.

        Verified against a live kitty for all four modes plus the
        single-participant (personal expense) case.
        """
        involved = split.involved
        if not involved:
            raise KittysplitError("cannot record an expense with nobody involved")
        split.validate(total)

        payload: dict[str, str] = {}
        for index, share in enumerate(involved):
            participant = share.participant.strip()
            party_id = party_ids.get(participant)
            if party_id is None:
                party_id = party_ids.get(
                    next(
                        (k for k in party_ids if k.strip().lower() == participant.lower()),
                        "",
                    )
                )
            if party_id is None:
                raise KittysplitError(f"unknown participant {participant!r}")

            payload[f"entry[entry_shares][{index}][party_id]"] = str(party_id)
            payload[f"entry[entry_shares][{index}][involved?]"] = "true"
            payload[f"entry[entry_shares][{index}][_persistent_id]"] = str(index)

            if split.mode == "weight" and len(involved) > 1:
                payload[f"entry[entry_shares][{index}][weight]"] = str(share.weight or 0)
            elif split.mode == "percent" and len(involved) > 1:
                payload[f"entry[entry_shares][{index}][percent]"] = str(share.percent or 0)
                payload[f"entry[entry_shares][{index}][percent_display]"] = str(
                    share.percent or 0
                )
            elif split.mode == "amount" and len(involved) > 1:
                payload[f"entry[entry_shares][{index}][share_str]"] = str(share.amount or 0)
                payload[f"entry[entry_shares][{index}][share_display]"] = str(
                    share.amount or 0
                )
        return payload

    def add_expense(
        self,
        description: str,
        amount: Decimal | str | float,
        paid_by: str,
        split: Split,
        date: str | None = None,
        category_id: int | None = None,
    ) -> dict[str, Any]:
        """Create an expense and return the created entry.

        ``split`` must already be validated by the caller (the approval flow
        does this). The write only happens for an explicitly approved expense.
        """
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        party_ids = self.participant_ids()
        if paid_by not in party_ids:
            raise KittysplitError(f"unknown payer {paid_by!r}")

        form = self._http.get(self._url("/entries/new/expense"))
        if form.status_code != 200:
            raise KittysplitError(f"expense form returned {form.status_code}")
        csrf = self._csrf(form.text)

        data: dict[str, str] = {
            "_csrf_token": csrf,
            "_dontcare": "true",
            "back_to": "",
            "entry[entry_type]": "expense",
            "entry[description]": description,
            "entry[amount]": f"{amount:.2f}",
            "entry[entry_date_str]": date or _today(),
            "entry[party_id]": str(party_ids[paid_by]),
            "entry[split_all_mode]": "none",
            # Kittysplit's radio values are equally/weight/percent/amount.
            "entry[split_mode]": "equally" if split.mode == "equal" else split.mode,
        }
        if category_id is not None:
            data["entry[category_id]"] = str(category_id)
        data.update(self._split_payload(split, party_ids, amount))

        # NOTE: no trailing slash on the POST target. With a trailing slash
        # Kittysplit answers 200 and silently re-renders the form instead of
        # creating the entry.
        response = self._http.post(self._url("/entries/new/expense"), data=data)
        if response.status_code not in (200, 303):
            raise KittysplitError(f"creating the expense returned {response.status_code}")

        entry_id = self._find_entry_id(description, amount)
        return {
            "entry_id": entry_id,
            "url": f"{self._url('')}/entries/{entry_id}" if entry_id else None,
            "description": description,
            "amount": amount,
        }

    def _find_entry_id(self, description: str, amount: Decimal) -> int | None:
        """Locate the entry we just created by matching description and amount."""
        for row in self._expense_rows():
            if row["description"] == description and row["amount"] == amount:
                return row["entry_id"]
        for row in self._expense_rows():
            if row["description"] == description:
                return row["entry_id"]
        return None

    def delete_expense(self, entry_id: int) -> bool:
        """Remove an expense. Used to roll back a bad write."""
        page = self._http.get(self._url(f"/entries/{entry_id}/edit"))
        if page.status_code != 200:
            raise KittysplitError(f"cannot open entry {entry_id} for deletion")
        csrf = self._csrf(page.text)
        response = self._http.post(
            self._url(f"/entries/{entry_id}/delete"), data={"_csrf_token": csrf}
        )
        return response.status_code == 200

    # ------------------------------------------------------------------
    # convenience builders
    # ------------------------------------------------------------------
    @staticmethod
    def equal_split(participants: list[str]) -> Split:
        return Split(
            mode="equal",
            shares=[Share(participant=name) for name in participants],
        )

    @staticmethod
    def solo_split(participant: str) -> Split:
        """A personal expense: one person eats 100% of it."""
        return Split(mode="equal", shares=[Share(participant=participant)])

    @staticmethod
    def weighted_split(weights: dict[str, Decimal | float]) -> Split:
        return Split(
            mode="weight",
            shares=[
                Share(participant=name, weight=Decimal(str(value)))
                for name, value in weights.items()
            ],
        )

    @staticmethod
    def percent_split(percents: dict[str, Decimal | float]) -> Split:
        return Split(
            mode="percent",
            shares=[
                Share(participant=name, percent=Decimal(str(value)))
                for name, value in percents.items()
            ],
        )

    @staticmethod
    def amount_split(amounts: dict[str, Decimal | float]) -> Split:
        return Split(
            mode="amount",
            shares=[
                Share(participant=name, amount=Decimal(str(value)))
                for name, value in amounts.items()
            ],
        )


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def expense_from_row(row: dict[str, Any], party_names: dict[int, str]) -> Expense:
    """Rebuild an :class:`Expense` from a parsed entry row."""
    shares = [
        Share(participant=party_names.get(pid, f"party-{pid}"), involved=True)
        for pid in row.get("share_party_ids", [])
    ]
    split = Split(mode="equal", shares=shares)
    return Expense(
        description=row.get("description") or "",
        amount=row.get("amount") or Decimal(0),
        paid_by=party_names.get(row.get("paid_by_id"), "unknown"),
        split=split,
        date="",
        category_id=row.get("category_id"),
        entry_id=row.get("entry_id"),
        url=row.get("url"),
    )
