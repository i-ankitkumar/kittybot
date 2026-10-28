"""Tests for the split-spec parser."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kittybot.splits import SplitParseError, parse_split

PEOPLE = ["Ankit", "Yuvraj"]


def test_empty_spec_is_equal():
    split = parse_split("", PEOPLE)
    assert split.mode == "equal"
    assert split.participant_names() == PEOPLE


@pytest.mark.parametrize("spec", ["all", "equal", "equally", "50/50", "everyone", "split"])
def test_equal_aliases(spec):
    split = parse_split(spec, PEOPLE)
    assert split.mode == "equal"
    assert split.participant_names() == PEOPLE


@pytest.mark.parametrize("spec", ["me", "mine", "personal", "myself"])
def test_personal(spec):
    split = parse_split(spec, PEOPLE)
    assert len(split.involved) == 1
    assert split.involved[0].participant == "Ankit"


def test_single_name():
    split = parse_split("yuvraj", PEOPLE)
    assert split.participant_names() == ["Yuvraj"]


def test_ratio_becomes_percent():
    split = parse_split("70/30", PEOPLE)
    assert split.mode == "percent"
    assert split.involved[0].percent == Decimal(70)
    assert split.involved[1].percent == Decimal(30)


def test_ratio_below_one_is_weight():
    split = parse_split("3/4", PEOPLE)
    assert split.mode == "weight"
    assert split.involved[0].weight == Decimal("0.75")
    assert split.involved[1].weight == Decimal("0.25")


def test_explicit_percentages():
    split = parse_split("60% 40%", PEOPLE)
    assert split.mode == "percent"
    assert [s.percent for s in split.involved] == [Decimal(60), Decimal(40)]


def test_fixed_amounts():
    # The numbers add up to the bill, so they are rupee amounts.
    split = parse_split("800 400", PEOPLE, total=Decimal("1200"))
    assert split.mode == "amount"
    assert split.involved[0].amount == Decimal(800)
    assert split.involved[1].amount == Decimal(400)


def test_amounts_without_a_matching_total_are_weights():
    split = parse_split("800 400", PEOPLE, total=Decimal("5000"))
    assert split.mode == "weight"


def test_named_weights():
    split = parse_split("me 70 yuvraj 30", PEOPLE)
    assert split.mode in {"weight", "percent"}
    assert {s.participant for s in split.involved} == {"Ankit", "Yuvraj"}


def test_named_fixed_amounts():
    split = parse_split("me 800 yuvraj 400", PEOPLE, total=Decimal("1200"))
    assert split.mode == "amount"
    assert split.involved[0].amount == Decimal(800)
    assert split.involved[1].amount == Decimal(400)


def test_unparseable_raises():
    with pytest.raises(SplitParseError):
        parse_split("banana", PEOPLE)


def test_single_amount_raises():
    with pytest.raises(SplitParseError):
        parse_split("800", PEOPLE)


def test_three_way():
    people = ["A", "B", "C"]
    split = parse_split("50/30/20", people)
    assert split.mode == "percent"
    assert [s.percent for s in split.involved] == [Decimal(50), Decimal(30), Decimal(20)]
