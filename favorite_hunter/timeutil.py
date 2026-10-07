"""Time helpers: parsing API timestamps, humanising durations, time buckets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# (label, lower bound in hours inclusive, upper bound exclusive)
TIME_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<1h", 0.0, 1.0),
    ("1-6h", 1.0, 6.0),
    ("6-24h", 6.0, 24.0),
    ("1-3d", 24.0, 72.0),
    ("3d+", 72.0, float("inf")),
)
PAST_END_BUCKET = "past_end"
UNKNOWN_BUCKET = "unknown"
TIME_BUCKET_ORDER = [label for label, _, _ in TIME_BUCKETS] + [PAST_END_BUCKET, UNKNOWN_BUCKET]


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: object) -> datetime | None:
    """Parse ISO-8601 strings, epoch seconds or epoch milliseconds into aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").replace(".", "", 1).isdigit():
            return _from_epoch(float(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # Gamma sometimes sends "2026-10-07 18:00:00+00" style offsets.
        if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit() and ":" not in text[-3:]:
            text = text + ":00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _from_epoch(number: float) -> datetime | None:
    # Heuristic: values above ~1e11 are milliseconds.
    seconds = number / 1000.0 if abs(number) > 1e11 else number
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def time_bucket(hours: float | None) -> str:
    if hours is None:
        return UNKNOWN_BUCKET
    if hours < 0:
        return PAST_END_BUCKET
    for label, low, high in TIME_BUCKETS:
        if low <= hours < high:
            return label
    return TIME_BUCKETS[-1][0]


def humanize_hours(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 0:
        return f"ended {humanize_hours(-hours)} ago"
    minutes = int(round(hours * 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m:02d}m"
    days, rem = divmod(minutes, 24 * 60)
    return f"{days}d {rem // 60}h"


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


__all__ = [
    "PAST_END_BUCKET",
    "TIME_BUCKETS",
    "TIME_BUCKET_ORDER",
    "UNKNOWN_BUCKET",
    "floor_minute",
    "hours_between",
    "humanize_hours",
    "iso",
    "parse_dt",
    "time_bucket",
    "timedelta",
    "utcnow",
]
