"""Parsing split specifications from text.

Supported forms (case-insensitive, whitespace tolerant):

    50/50          equal split between everyone
    70/30          weighted split
    60% / 40%      percentage split
    800 400        fixed rupee amounts
    all            equal split between everyone
    me / personal  only me (a personal expense)
    <name>         only that person
    me 70 yuvraj 30   explicit weights
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .models import Share, Split

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_FRACTION_RE = re.compile(r"(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)+)")
_AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)")


class SplitParseError(ValueError):
    """Raised when a split specification cannot be understood."""


def _decimals(text: str) -> list[Decimal]:
    out = []
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            out.append(Decimal(token))
        except InvalidOperation:
            continue
    return out


def parse_split(spec: str, participants: list[str], total: Decimal | None = None) -> Split:
    """Turn a user's split instruction into a :class:`Split`.

    ``participants`` is the full list of people in the kitty. Names may be
    matched loosely: "yuvraj", "yuv" all resolve to a participant whose name
    starts with that text.

    ``total`` is the expense amount when it is known. It disambiguates the
    numeric forms: "800 400" on a 1200 rupee bill means fixed amounts of
    800 and 400, but on a 5000 rupee bill it can only be a ratio.
    """
    spec = (spec or "").strip()
    lowered = spec.lower()

    if not spec:
        return Split(mode="equal", shares=[Share(participant=p) for p in participants])

    # --- personal / single person -----------------------------------
    if lowered in {"me", "mine", "myself", "personal", "self", "only me"}:
        me = _resolve("me", participants) or (participants[0] if participants else None)
        if me is None:
            raise SplitParseError("no participants in the kitty")
        return Split(mode="equal", shares=[Share(participant=me)])

    if lowered in {"all", "equal", "equally", "split", "split equally", "50/50", "everyone"}:
        return Split(mode="equal", shares=[Share(participant=p) for p in participants])

    # --- percentage --------------------------------------------------
    percents = _PERCENT_RE.findall(spec)
    if len(percents) >= 2:
        names = _split_names(spec, participants, len(percents))
        return Split(
            mode="percent",
            shares=[
                Share(participant=name, percent=Decimal(value))
                for name, value in zip(names, percents, strict=False)
            ],
        )

    # --- fraction like 70/30 -----------------------------------------
    fractions = _FRACTION_RE.findall(spec)
    if fractions:
        values = [Decimal(part) for group in fractions for part in re.split(r"\s*/\s*", group)]
        if len(fractions) == 1 and len(values) == 2:
            numerator, denominator = values
            if denominator == 0:
                raise SplitParseError("cannot divide by zero")
            ratio = numerator / denominator
            if ratio > 1:
                # "70/30" means 70 percent / 30 percent, not a fraction.
                total = numerator + denominator
                if total == 0:
                    raise SplitParseError("both parts of the split are zero")
                shares = [
                    Share(participant=p, percent=numerator / total * 100)
                    for p in participants[:1]
                ]
                for participant in participants[1:]:
                    shares.append(Share(participant=participant, percent=denominator / total * 100))
                return Split(mode="percent", shares=shares)
            shares = [Share(participant=p, weight=ratio) for p in participants[:1]]
            for participant in participants[1:]:
                shares.append(Share(participant=participant, weight=Decimal(1) - ratio))
            return Split(mode="weight", shares=shares)

        # "50/30/20" - a chain of parts. Percentages if they add up to 100,
        # otherwise weights.
        total = sum(values, Decimal(0))
        if total == 0:
            raise SplitParseError("the split parts add up to zero")
        names = _names_for_count(spec, participants, len(values))
        if total == 100:
            return Split(
                mode="percent",
                shares=[Share(participant=n, percent=v) for n, v in zip(names, values, strict=False)],
            )
        return Split(
            mode="weight",
            shares=[
                Share(participant=n, weight=v / total)
                for n, v in zip(names, values, strict=False)
            ],
        )

    # --- named people with numbers -----------------------------------
    if re.search(r"[a-z]", lowered) and _decimals(spec):
        return _parse_named(spec, participants, total)

    # --- bare amounts: "800 400" -------------------------------------
    amounts = _decimals(spec)
    if len(amounts) >= 2:
        names = _split_names(spec, participants, len(amounts))
        total_amount = sum(amounts, Decimal(0))
        if total is not None and abs(total_amount - total) <= Decimal("0.01"):
            # The numbers add up to the bill, so they are rupee amounts.
            return Split(
                mode="amount",
                shares=[
                    Share(participant=name, amount=amount)
                    for name, amount in zip(names, amounts, strict=False)
                ],
            )
        if total_amount == 100:
            return Split(
                mode="percent",
                shares=[
                    Share(participant=name, percent=amount)
                    for name, amount in zip(names, amounts, strict=False)
                ],
            )
        if total_amount > 0:
            return Split(
                mode="weight",
                shares=[
                    Share(participant=name, weight=amount / total_amount)
                    for name, amount in zip(names, amounts, strict=False)
                ],
            )
        raise SplitParseError("the split parts add up to zero")
    if len(amounts) == 1:
        raise SplitParseError(
            "give one amount per person, for example: 800 400 (or 'me 800 yuvraj')"
        )

    # --- a bare name --------------------------------------------------
    single = _resolve(spec, participants)
    if single:
        return Split(mode="equal", shares=[Share(participant=single)])

    raise SplitParseError(
        f"could not understand the split {spec!r}. "
        "Try 50/50, 70/30, '60% 40%', '800 400', 'me' or a person's name."
    )


def _split_names(spec: str, participants: list[str], count: int) -> list[str]:
    """Work out which participant each number belongs to.

    If the spec names as many people as there are numbers, pair them up in
    order. Otherwise fall back to the kitty's participant order.
    """
    tokens = re.split(r"[\s,]+", spec.strip())
    named: list[str] = []
    pending: list[str] = []
    for token in tokens:
        resolved = _resolve(token, participants)
        if resolved and not re.fullmatch(r"[\d.,]+", token):
            if pending:
                named.extend(pending)
                pending = []
            named.append(resolved)
        elif re.fullmatch(r"[\d.,]+[%rsin₹]*", token) and pending:
            pending.append(token)
    if pending:
        named.extend(pending)

    if len(named) == count:
        return named
    if len(named) > count:
        return named[:count]
    return list(participants[:count]) + named[len(participants[:count]) :][: 0]


def _parse_named(spec: str, participants: list[str], total: Decimal | None = None) -> Split:
    """Parse "me 800 yuvraj 400" and "me 70 yuvraj 30" style splits."""
    tokens = re.split(r"[\s,]+", spec.strip())
    pairs: list[tuple[str, Decimal]] = []
    current_name: str | None = None
    for token in tokens:
        if re.fullmatch(r"[\d.,]+[%rsin₹]*", token):
            value = _decimals(token)
            if not value:
                continue
            amount = value[0]
            if current_name is None:
                raise SplitParseError(
                    "a number must follow the person's name, for example: me 70 yuvraj 30"
                )
            pairs.append((current_name, amount))
            current_name = None
        else:
            resolved = _resolve(token, participants)
            if resolved:
                current_name = resolved
            elif token.lower() in {"me", "mine", "myself"}:
                current_name = _resolve("me", participants) or (
                    participants[0] if participants else None
                )
            else:
                raise SplitParseError(f"unknown person {token!r}")

    if not pairs:
        raise SplitParseError(f"could not understand the split {spec!r}")
    if len(pairs) == 1:
        name, _ = pairs[0]
        return Split(mode="equal", shares=[Share(participant=name)])

    values_total = sum((amount for _, amount in pairs), Decimal(0))
    if values_total == 0:
        raise SplitParseError("the split parts add up to zero")

    # Decide what the numbers mean:
    #   - they add up to 100       -> percentages
    #   - they add up to the total -> fixed rupee amounts
    #   - otherwise                -> a ratio (weights)
    if abs(values_total - Decimal(100)) <= Decimal("0.01"):
        return Split(
            mode="percent",
            shares=[Share(participant=name, percent=amount) for name, amount in pairs],
        )
    if total is not None and abs(values_total - total) <= Decimal("0.01"):
        return Split(
            mode="amount",
            shares=[Share(participant=name, amount=amount) for name, amount in pairs],
        )
    return Split(
        mode="weight",
        shares=[Share(participant=name, weight=amount) for name, amount in pairs],
    )


def _names_for_count(spec: str, participants: list[str], count: int) -> list[str]:
    """Pick ``count`` participant names for a numeric split."""
    tokens = re.split(r"[\s,]+", spec.strip())
    named: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[\d.,]+[%rsin₹/]*", token):
            continue
        resolved = _resolve(token, participants)
        if resolved:
            named.append(resolved)
    if len(named) == count:
        return named
    if len(named) > count:
        return named[:count]
    out = list(participants[:count])
    for name in named:
        if name not in out:
            out.append(name)
    return out[:count]


def _resolve(token: str, participants: list[str]) -> str | None:
    """Resolve a token to a participant name, loosely."""
    token = token.strip().strip(".,:").lower()
    if not token:
        return None
    for participant in participants:
        if participant.strip().lower() == token:
            return participant
    if token in {"me", "mine", "myself", "i"} and participants:
        return participants[0]
    for participant in participants:
        lowered = participant.strip().lower()
        if lowered.startswith(token) or token.startswith(lowered):
            return participant
    return None
