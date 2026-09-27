from datetime import date, datetime

import pytest

from dastbedast.core.sms import Bank, Direction, SmsParseError, dedupe_key, parse_sms

TODAY = date(2026, 10, 10)  # 1405/07/18

PASARGAD = "201.8000.2xxxxxx9.1\n+12,000,000\n07/04_17:41\nمانده: 150,300,000\n"
MELLI = "بانك ملي ايران\nپايا:80,000,000+\nحساب:82xxx\nمانده:95,000,000\n0701-19:18 "
RESALAT = "10.9xxxx11.1\n+30,000,000\n07/14_09:18\nمانده: 41,000,000"
MEHR = "پرداخت گروهي\nحساب: 300xxxxxx000000001\nمبلغ:25,000,000\nمانده:33,000,000\nزمان:1405/6/23-10:07"


def test_pasargad():
    r = parse_sms(PASARGAD, TODAY)
    assert (r.bank, r.direction, r.amount, r.balance) == (Bank.PASARGAD, Direction.IN, 12_000_000, 150_300_000)
    assert r.occurred_at == datetime(2026, 9, 26, 17, 41)  # 1405/07/04
    assert r.uncertain == ()


def test_melli():
    r = parse_sms(MELLI, TODAY)
    assert (r.bank, r.direction, r.amount, r.balance) == (Bank.MELLI, Direction.IN, 80_000_000, 95_000_000)
    assert r.channel == "پایا"
    assert r.account == "82xxx"
    assert r.occurred_at == datetime(2026, 9, 23, 19, 18)  # 1405/07/01


def test_resalat():
    r = parse_sms(RESALAT, TODAY)
    assert (r.bank, r.amount, r.balance) == (Bank.RESALAT, 30_000_000, 41_000_000)
    assert r.occurred_at == datetime(2026, 10, 6, 9, 18)  # 1405/07/14


def test_mehr_group_payment_is_outgoing():
    r = parse_sms(MEHR, TODAY)
    assert (r.bank, r.direction, r.amount, r.balance) == (Bank.MEHR, Direction.OUT, 25_000_000, 33_000_000)
    assert r.occurred_at == datetime(2026, 9, 14, 10, 7)


def test_mehr_unknown_label_asks():
    r = parse_sms(MEHR.replace("پرداخت گروهي", "واریز"), TODAY)
    assert r.direction is None
    assert "direction" in r.uncertain


def test_persian_digits_and_debit():
    text = "201.8000.2xxxxxx9.1\n-۱,۵۰۰,۰۰۰\n۰۷/۰۴_۱۷:۴۱\nمانده: ۱۰۰"
    r = parse_sms(text, TODAY)
    assert (r.direction, r.amount, r.balance) == (Direction.OUT, 1_500_000, 100)


def test_missing_year_in_future_means_last_year():
    r = parse_sms(PASARGAD.replace("07/04", "12/20"), TODAY)
    assert r.occurred_at is not None and r.occurred_at.year == 2026 and r.occurred_at.month == 3  # 1404/12/20


def test_bank_hint_overrides_account_heuristic():
    assert parse_sms(RESALAT, TODAY, bank=Bank.PASARGAD).bank == Bank.PASARGAD


def test_unrecognised():
    with pytest.raises(SmsParseError):
        parse_sms("hello", TODAY)


def test_dedupe_key_stable():
    r = parse_sms(PASARGAD, TODAY)
    assert dedupe_key(r.bank, r.amount, r.occurred_at, r.balance) == dedupe_key(
        "pasargad", 12_000_000, datetime(2026, 9, 26, 17, 41), 150_300_000
    )
