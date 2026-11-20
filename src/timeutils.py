from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_time_of_day(value: str) -> time:
    hour_text, minute_text = value.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def latest_due_schedule_date(now: datetime, run_time: time) -> date:
    if now.timetz().replace(tzinfo=None) >= run_time:
        return now.date()
    return now.date() - timedelta(days=1)


def scheduled_window(schedule_date: date, run_time: time, lookback_hours: int, timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    until = datetime.combine(schedule_date, run_time, tzinfo=tz)
    since = until - timedelta(hours=lookback_hours)
    return since, until


def calendar_window(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    return datetime.combine(day, time.min, tzinfo=tz), datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)


def parse_datetime_value(value: object, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    tz = ZoneInfo(timezone_name)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime_value(int(text), timezone_name)

    normalized = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .replace("T", " ")
        .replace("Z", "+00:00")
        .strip()
    )

    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d %H:%M",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt)
            if fmt == "%m-%d %H:%M":
                parsed = parsed.replace(year=datetime.now(tz).year)
            return parsed.replace(tzinfo=tz)
        except ValueError:
            continue
    return None
