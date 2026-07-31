from datetime import datetime

from smart_collector.schedule import schedule_slot


def test_schedule_modes() -> None:
    now = datetime(2026, 7, 31, 23, 0).astimezone()
    subscribed = datetime(2026, 7, 24, 12, 0).astimezone().timestamp()
    assert schedule_slot(now, ("每天",), "23:00", subscribed)
    assert schedule_slot(now, ("周五",), "23:00", subscribed)
    assert schedule_slot(now, ("每周",), "23:00", subscribed)
    assert not schedule_slot(now, ("每天",), "22:00", subscribed)
