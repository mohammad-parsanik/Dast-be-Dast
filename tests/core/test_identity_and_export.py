import pytest

from dastbedast.core.bulk_transfer import BulkTransferError, PayoutRow, export_placeholder_csv
from dastbedast.core.identity import is_valid_iban, mask_iban, normalize_iban, normalize_phone
from dastbedast.core.money import format_tomans, parse_amount

# A syntactically valid, made-up IBAN (checksum computed, not a real account).
IBAN = "IR160120000000001234567890"


def test_iban():
    assert is_valid_iban(IBAN)
    assert not is_valid_iban(IBAN[:-1] + "3")
    assert normalize_iban(" ir16 0120 0000 0000 1234 5678 90") == IBAN
    assert normalize_iban("۱۶۰۱۲۰۰۰۰۰۰۰۰۰۱۲۳۴۵۶۷۸۹۰") == IBAN
    assert mask_iban(IBAN) == "IR16…7890"


@pytest.mark.parametrize("raw", ["09121234567", "+989121234567", "989121234567", "۰۹۱۲ ۱۲۳ ۴۵۶۷"])
def test_phone(raw):
    assert normalize_phone(raw) == "+989121234567"


def test_phone_rejects_landline():
    with pytest.raises(ValueError):
        normalize_phone("02112345678")


def test_money():
    assert parse_amount("۱۲,۰۰۰,۰۰۰") == 12_000_000
    assert format_tomans(45_670_068) == "4,567,006.8"
    assert format_tomans(-120) == "-12"


def test_placeholder_export():
    csv = export_placeholder_csv([PayoutRow(IBAN, "Test Holder", 10_000_000, "1405/08")])
    assert csv.splitlines()[1] == f"1,{IBAN},Test Holder,10000000,1405/08"
    with pytest.raises(BulkTransferError):
        export_placeholder_csv([PayoutRow("IR00", "x", 1)])
