from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")  # Google's free-tier quotas reset at midnight here
SYDNEY = ZoneInfo("Australia/Sydney")


def pacific_day(now: datetime) -> date:
    return now.astimezone(PACIFIC).date()


def next_reset(now: datetime) -> datetime:
    """The next midnight Pacific, as an aware UTC datetime."""
    tomorrow = pacific_day(now) + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=PACIFIC).astimezone(UTC)


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str | None  # day_budget, google_quota
    message: str
    reset_at_utc: datetime
    reset_at_sydney: datetime
    day: date


def availability_message(reset_at: datetime) -> str:
    local = reset_at.astimezone(SYDNEY)
    return (
        "The planner has used up today's AI capacity. It resets at "
        f"{reset_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC "
        f"({local:%a %d %b, %H:%M} Sydney time)."
    )


def build_availability(now: datetime, reason: str | None) -> Availability:
    reset = next_reset(now)
    return Availability(
        available=reason is None,
        reason=reason,
        message="available" if reason is None else availability_message(reset),
        reset_at_utc=reset,
        reset_at_sydney=reset.astimezone(SYDNEY),
        day=pacific_day(now),
    )
