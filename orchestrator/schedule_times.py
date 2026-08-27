"""When a schedule next runs.

Split out from the runner on purpose: due-time arithmetic is where scheduling
bugs live, and a pure function over an explicit "now" can be tested for the
awkward cases — a daily job created after today's slot, a weekly job created on
its own weekday, a service restarted after downtime — without waiting for a
clock or standing up a database.

Everything is UTC. A schedule that shifts by an hour twice a year because of
daylight saving is a bug nobody notices until the reports are already wrong.
"""
from datetime import datetime, timedelta, timezone

KINDS = ("hourly", "daily", "weekly")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def validate(kind: str, hour: int, minute: int, weekday=None) -> None:
    """Reject a schedule that could never fire, rather than storing it.

    A rejected schedule is visible immediately; one stored with hour=25 looks
    fine in the list and simply never runs.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown schedule kind: {kind}. Valid: {list(KINDS)}")
    if not 0 <= int(minute) <= 59:
        raise ValueError("minute must be 0-59")
    if kind in ("daily", "weekly") and not 0 <= int(hour) <= 23:
        raise ValueError("hour must be 0-23")
    if kind == "weekly":
        if weekday is None:
            raise ValueError("weekly schedules need a weekday (0=Monday)")
        if not 0 <= int(weekday) <= 6:
            raise ValueError("weekday must be 0-6, Monday first")


def next_due(kind: str, hour: int, minute: int, weekday=None,
             after: datetime = None) -> datetime:
    """The first firing time strictly after ``after``.

    Strictly after, not "at or after": otherwise a job that just ran would be
    due again immediately and loop.
    """
    validate(kind, hour, minute, weekday)
    now = _aware(after or datetime.now(timezone.utc)).replace(microsecond=0)

    if kind == "hourly":
        candidate = now.replace(minute=int(minute), second=0)
        if candidate <= now:
            candidate += timedelta(hours=1)
        return candidate

    candidate = now.replace(hour=int(hour), minute=int(minute), second=0)

    if kind == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # weekly
    days_ahead = (int(weekday) - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def catch_up(kind: str, hour: int, minute: int, weekday=None,
             now: datetime = None) -> datetime:
    """The next firing time after a missed window.

    The service being down for two days must not release two days of backlog at
    once: forty tool-calling runs would hammer five production APIs to produce
    reports nobody will read. A missed schedule fires once when it is next seen
    and is then scheduled forward from now.
    """
    return next_due(kind, hour, minute, weekday, after=now)


def describe(kind: str, hour: int, minute: int, weekday=None) -> str:
    """Human wording for the UI, so a stored schedule can be read back."""
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")
    if kind == "hourly":
        return f"every hour at :{int(minute):02d}"
    if kind == "daily":
        return f"daily at {int(hour):02d}:{int(minute):02d} UTC"
    return (f"every {days[int(weekday)]} at "
            f"{int(hour):02d}:{int(minute):02d} UTC")
