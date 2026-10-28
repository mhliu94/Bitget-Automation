from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import hashlib
from .errors import CommandError


def decimal_string(value):
    if isinstance(value, bool) or value is None:
        raise CommandError("Amount must be a finite positive decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CommandError("Amount must be a finite positive decimal") from None
    if not number.is_finite() or number <= 0 or len(number.as_tuple().digits) > 40 or abs(number.as_tuple().exponent) > 30:
        raise CommandError("Amount must be positive, finite and within supported precision")
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")


def parse_time(value):
    if isinstance(value, bool):
        raise CommandError("Timestamp must not be boolean")
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        stamp = float(value)
        result = datetime.fromtimestamp(stamp / 1000 if stamp > 100_000_000_000 else stamp, timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise CommandError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def in_window(value, start, end):
    return start <= parse_time(value) < end


def iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def today_window(tz="America/New_York", now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def client_order_id(command_id, max_length=48):
    return hashlib.sha256(str(command_id).encode()).hexdigest()[:max_length]
