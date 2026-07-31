from __future__ import annotations

from datetime import datetime

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def schedule_slot(
    now: datetime, schedules: tuple[str, ...], at_time: str, subscribed_at: float
) -> str | None:
    if not schedules or now.strftime("%H:%M") != at_time:
        return None
    active = False
    if "每天" in schedules:
        active = True
    if WEEKDAYS[now.weekday()] in schedules:
        active = True
    subscribed = datetime.fromtimestamp(subscribed_at, tz=now.tzinfo)
    if "每周" in schedules and subscribed.weekday() == now.weekday():
        active = True
    if "每月" in schedules and subscribed.day == now.day:
        active = True
    return now.strftime("%Y-%m-%dT%H:%M") if active else None
