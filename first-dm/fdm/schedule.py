"""Sending window, caps and spacing in America/New_York (DST-aware)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WINDOW_START_HOUR = 6   # 6:00 a.m. inclusive
WINDOW_END_HOUR = 23    # 11:00 p.m. exclusive
HOURLY_CAP = 5
DAILY_CAP = 80
MIN_SPACING = timedelta(minutes=3)


def to_et(dt_utc):
    return dt_utc.astimezone(ET)


def in_window(now_utc):
    h = to_et(now_utc).hour
    return WINDOW_START_HOUR <= h < WINDOW_END_HOUR


def hour_key(dt_utc):
    e = to_et(dt_utc)
    # fold distinguishes the repeated 1 a.m. hour at fall-back (outside window anyway)
    return (e.date().isoformat(), e.hour, e.fold)


def day_key(dt_utc):
    return to_et(dt_utc).date().isoformat()


def can_attempt(now_utc, attempt_times_utc):
    """attempt_times_utc: datetimes of every counted attempt (sent, uncertain,
    failed, pending). Returns (ok, reason)."""
    if not in_window(now_utc):
        return False, "outside_window"
    day = day_key(now_utc)
    hk = hour_key(now_utc)
    day_n = sum(1 for t in attempt_times_utc if day_key(t) == day)
    if day_n >= DAILY_CAP:
        return False, "daily_cap"
    hour_n = sum(1 for t in attempt_times_utc if hour_key(t) == hk)
    if hour_n >= HOURLY_CAP:
        return False, "hourly_cap"
    if attempt_times_utc:
        last = max(attempt_times_utc)
        if now_utc - last < MIN_SPACING:
            return False, "spacing"
    return True, "ok"


def utcnow():
    return datetime.now(timezone.utc)
