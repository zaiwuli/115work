from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def next_run(trigger: str, config: dict, timezone: str, now: float) -> float:
    zone = ZoneInfo(timezone)
    if trigger == "interval":
        seconds = config.get("seconds", 60)
        if type(seconds) is not int or not 15 <= seconds <= 31 * 86400:
            raise ValueError("INVALID_SCHEDULE_INTERVAL")
        return now + seconds
    if trigger == "cron":
        expression = config.get("expression", "")
        if not croniter.is_valid(expression):
            raise ValueError("INVALID_CRON")
        return croniter(expression, datetime.fromtimestamp(now, zone)).get_next(datetime).timestamp()
    if trigger == "once":
        value = float(config.get("at", 0))
        if value <= 0:
            raise ValueError("INVALID_ONE_TIME_SCHEDULE")
        return value
    if trigger == "event":
        return now + 365 * 86400
    raise ValueError("INVALID_SCHEDULE_TRIGGER")
