"""Facebook scheduled publish times."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from reelpost.constants import SCHEDULE_MAX_DAYS, SCHEDULE_MIN
from reelpost.errors import ReelpostError


def parse_schedule(value: str, *, now: datetime | None = None, local_tz=None) -> datetime:
    """Parse an ISO time. A value without a zone uses local_tz or the machine zone."""
    text = value.strip()
    if not text:
        raise ReelpostError("Schedule time is empty.")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReelpostError(
            f"Could not read schedule {value!r}. Use an ISO time such as 2026-10-29T09:00+01:00."
        ) from exc
    if parsed.tzinfo is None:
        zone = local_tz if local_tz is not None else datetime.now().astimezone().tzinfo
        parsed = parsed.replace(tzinfo=zone)
    when = parsed.astimezone(timezone.utc)
    moment = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    assert_schedule_window(when, moment)
    return when


def assert_schedule_window(when: datetime, now: datetime) -> None:
    if when.tzinfo is None or now.tzinfo is None:
        raise ReelpostError("Schedule times need a timezone.")
    delta = when - now
    if delta < timedelta(seconds=SCHEDULE_MIN):
        raise ReelpostError("Facebook schedule must be at least 10 minutes in the future.")
    if delta > timedelta(days=SCHEDULE_MAX_DAYS):
        raise ReelpostError("Facebook schedule must be within 6 months.")
