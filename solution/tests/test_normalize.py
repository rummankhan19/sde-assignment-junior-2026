from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from au_ledger.normalize import (IST, normalise_header, normalise_phone, parse_date, parse_decimal,
                                 parse_enum, parse_timestamp, to_epoch_us)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", " 98765-43210 ", "919876543210",
])
def test_phone_all_generator_formats_normalise(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad leading digit
    "022-2345-6789",      # landline: 11 digits, drop 0 -> starts with 2
    "N/A", "", "+1 415 555 0134",
    "9198765432101",      # 13 digits
    "0919876543210",      # 0 + 91 + number: 13 digits, only one prefix rule applies
    "91987654321",        # 11 digits starting 91 is NOT stripped (only the 12-digit rule)
])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


def test_phone_twelve_digits_starting_91_that_is_itself_a_number():
    # 12 digits starting with 91 -> always strip 91 first
    assert normalise_phone("916123456789") == "+916123456789"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),   # day-first
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-dec-2026", date(2026, 12, 1)),
    ("1/2/2026", date(2026, 2, 1)),
    ("29/02/2028", date(2028, 2, 29)),   # leap year
])
def test_dates_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2026", "2026-13-01", "soon", "TBD", "12/31/2026", "01-Dez-2026",
    "01/12-2026", "2026/12/01", "",
])
def test_dates_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12000", "12000.00"), ("₹28,397", "28397.00"), ("Rs. 24787", "24787.00"),
    ("Rs24787", "24787.00"), ("rs 1.5", "1.50"), ("INR 18678.3", "18678.30"),
    ("inr18678.345", "18678.35"),  # half-up rounding
    ("0", "0.00"), ("₹ 1,00,000", "100000.00"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-500", "Rs.", "1.2.3", "$100"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum_case_insensitive_and_lowercased():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None


def test_header_normalisation():
    assert normalise_header("  Mobile__No ") == "mobile no"
    assert normalise_header("Insured\t Name") == "insured name"
    assert normalise_header("agent_notes") == "agent notes"


@pytest.mark.parametrize("raw", ["2026-10-05T07:50:21Z", "2026-10-05T13:20:21+05:30",
                                 "2026-10-05 13:20:21"])
def test_timestamps_three_forms_same_instant(raw):
    assert parse_timestamp(raw) == datetime(2026, 10, 5, 7, 50, 21, tzinfo=timezone.utc)


def test_naive_timestamp_is_ist_not_utc():
    ts = parse_timestamp("2026-10-03 01:00:00")
    assert ts.astimezone(IST).date() == date(2026, 10, 3)
    assert ts.astimezone(timezone.utc).date() == date(2026, 10, 2)


@pytest.mark.parametrize("raw", [None, "", "yesterday", 1234, "2026-10-05T25:00:00Z"])
def test_timestamps_invalid(raw):
    assert parse_timestamp(raw) is None


def test_epoch_us():
    assert to_epoch_us(datetime(1970, 1, 1, 0, 0, 1, 5, tzinfo=timezone.utc)) == 1_000_005
