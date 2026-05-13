"""Tests for tgbot.scheduler.triggers."""
import pytest

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tgbot.scheduler.triggers import build_trigger, describe_trigger


def test_build_interval():
    t = build_trigger("interval", {"minutes": 10})
    assert isinstance(t, IntervalTrigger)
    # IntervalTrigger stores interval as datetime.timedelta
    assert t.interval.total_seconds() == 600


def test_build_daily():
    t = build_trigger("daily", {"hour": 4, "minute": 30})
    assert isinstance(t, CronTrigger)


def test_build_weekly():
    t = build_trigger("weekly", {"day_of_week": "mon", "hour": 3, "minute": 0})
    assert isinstance(t, CronTrigger)


def test_build_cron_valid():
    t = build_trigger("cron", {"expr": "0 4 * * 1"})
    assert isinstance(t, CronTrigger)


def test_build_cron_invalid_raises():
    with pytest.raises(ValueError):
        build_trigger("cron", {"expr": "not a cron expr"})


def test_build_unknown_kind_raises():
    with pytest.raises(ValueError):
        build_trigger("monthly", {})


def test_describe_interval_minutes():
    assert describe_trigger("interval", {"minutes": 10}) == "toutes les 10 min"


def test_describe_interval_hours():
    assert describe_trigger("interval", {"hours": 2}) == "toutes les 2 h"


def test_describe_daily():
    assert describe_trigger("daily", {"hour": 4, "minute": 0}) == "quotidien 04:00"


def test_describe_weekly():
    assert (
        describe_trigger("weekly", {"day_of_week": "mon", "hour": 3, "minute": 0})
        == "hebdo lundi 03:00"
    )


def test_describe_cron():
    assert describe_trigger("cron", {"expr": "0 4 * * 1"}) == "cron `0 4 * * 1`"
