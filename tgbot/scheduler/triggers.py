"""Convert (trigger_kind, trigger_spec) records to APScheduler triggers + labels."""
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


_WEEKDAYS_FR = {
    "mon": "lundi", "tue": "mardi", "wed": "mercredi", "thu": "jeudi",
    "fri": "vendredi", "sat": "samedi", "sun": "dimanche",
}


def build_trigger(kind: str, spec: dict):
    """Return an APScheduler trigger for the given (kind, spec).

    Raises ValueError on unknown kind or invalid cron expression.
    """
    if kind == "interval":
        # spec keys: any of seconds/minutes/hours/days (all int)
        return IntervalTrigger(**spec)
    if kind == "daily":
        return CronTrigger(hour=spec["hour"], minute=spec["minute"])
    if kind == "weekly":
        return CronTrigger(
            day_of_week=spec["day_of_week"],
            hour=spec["hour"],
            minute=spec["minute"],
        )
    if kind == "cron":
        try:
            return CronTrigger.from_crontab(spec["expr"])
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid cron expression: {e}") from e
    raise ValueError(f"Unknown trigger kind: {kind}")


def describe_trigger(kind: str, spec: dict) -> str:
    """Human-readable French summary of a trigger, used in list/card UI."""
    if kind == "interval":
        if "minutes" in spec:
            return f"toutes les {spec['minutes']} min"
        if "hours" in spec:
            return f"toutes les {spec['hours']} h"
        if "seconds" in spec:
            return f"toutes les {spec['seconds']} s"
        if "days" in spec:
            return f"tous les {spec['days']} j"
        return "intervalle"
    if kind == "daily":
        return f"quotidien {spec['hour']:02d}:{spec['minute']:02d}"
    if kind == "weekly":
        day = _WEEKDAYS_FR.get(spec["day_of_week"], spec["day_of_week"])
        return f"hebdo {day} {spec['hour']:02d}:{spec['minute']:02d}"
    if kind == "cron":
        return f"cron `{spec['expr']}`"
    return kind
