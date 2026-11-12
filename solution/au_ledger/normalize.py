"""Pure value-normalisation rules from the spec. No I/O, no database: easy to test."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_WS_RUN = re.compile(r"[_\s]+")
_NON_DIGIT = re.compile(r"\D")


def normalise_header(h: str) -> str:
    """Lower-case, trim, and collapse every run of `_`/whitespace into one space."""
    return _WS_RUN.sub(" ", h.strip().lower()).strip()


def normalise_phone(raw: str | None) -> str | None:
    """Return the canonical `+91XXXXXXXXXX` form, or None if it isn't a valid Indian mobile."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_DATE = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")
_DMONY_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


def parse_date(raw: str) -> date | None:
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day-first). None if invalid."""
    s = raw.strip()
    try:
        if m := _ISO_DATE.match(s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DMY_DATE.match(s):
            return date(int(m[4]), int(m[3]), int(m[1]))
        if m := _DMONY_DATE.match(s):
            month = _MONTHS.get(m[2].lower())
            return date(int(m[3]), month, int(m[1])) if month else None
    except ValueError:  # impossible dates such as 31/02
        return None
    return None


_CURRENCY = re.compile(r"^(?:₹|rs\.?|inr)?\s*(.*)$", re.IGNORECASE)
_NUMBER = re.compile(r"^\d[\d,]*(?:\.\d+)?$")


def parse_decimal(raw: str) -> Decimal | None:
    """Optional ₹ / Rs / Rs. / INR prefix, commas as separators, must be >= 0, 2 dp half-up."""
    body = _CURRENCY.match(raw.strip())[1].strip()
    if not _NUMBER.match(body):
        return None
    try:
        value = Decimal(body.replace(",", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_enum(raw: str, values: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


def parse_timestamp(raw) -> datetime | None:
    """UTC (`Z`), offset, or naive (naive means IST). Returns an aware datetime, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def to_epoch_us(dt: datetime) -> int:
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
