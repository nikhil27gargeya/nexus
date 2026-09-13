from datetime import UTC, date, datetime


def date_part(value: str) -> date:
    """Read the written calendar date without converting its timezone."""
    return date.fromisoformat(value[:10])


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc_timestamp(value: str | datetime) -> str:
    parsed = parse_datetime(value) if isinstance(value, str) else value
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
