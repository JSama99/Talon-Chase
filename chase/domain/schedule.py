"""When to ask. Pure functions over integer milliseconds and an IANA time zone.

Default plan, all in the carrier's local time:
  first     as soon as the chase opens (pushed out of quiet hours)
  sms       first + 3h, text to the driver
  second    08:00 on the next business day after the first ask
  third     first + 48h, never sooner than 4h after the second
  escalate  third + 24h with no document
Nothing is sent between 20:00 and 07:00 local. Public holidays are not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chase.domain.models import BrokerConfig


def to_local(ts_ms: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts_ms // 1000, ZoneInfo(tz))


def to_ms(local: datetime) -> int:
    return int(local.timestamp()) * 1000


def _at_hour(day: datetime, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=day.tzinfo)


def in_quiet_hours(ts_ms: int, tz: str, cfg: BrokerConfig) -> bool:
    hour = to_local(ts_ms, tz).hour
    return hour >= cfg.quiet_start_hour or hour < cfg.quiet_end_hour


def next_allowed(ts_ms: int, tz: str, cfg: BrokerConfig) -> int:
    """The earliest time at or after ``ts_ms`` that is outside quiet hours."""
    local = to_local(ts_ms, tz)
    if local.hour >= cfg.quiet_start_hour:
        return to_ms(_at_hour(local + timedelta(days=1), cfg.quiet_end_hour))
    if local.hour < cfg.quiet_end_hour:
        return to_ms(_at_hour(local, cfg.quiet_end_hour))
    return ts_ms


def next_business_morning(ts_ms: int, tz: str, cfg: BrokerConfig) -> int:
    """Morning hour on the first Monday-to-Friday date after the local date of ``ts_ms``."""
    day = to_local(ts_ms, tz) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return to_ms(_at_hour(day, cfg.morning_hour))


@dataclass(frozen=True)
class Plan:
    first_ms: int
    sms_ms: int  # 0 means no driver text is planned
    second_ms: int
    third_ms: int
    escalate_ms: int

    def to_dict(self) -> dict[str, int]:
        return {
            "first_ms": self.first_ms,
            "sms_ms": self.sms_ms,
            "second_ms": self.second_ms,
            "third_ms": self.third_ms,
            "escalate_ms": self.escalate_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> Plan:
        return cls(**data)


def build_plan(t0_ms: int, tz: str, cfg: BrokerConfig) -> Plan:
    first = next_allowed(t0_ms, tz, cfg)
    second = next_business_morning(first, tz, cfg)
    sms = next_allowed(first + cfg.sms_delay_ms, tz, cfg)
    if sms >= second:
        sms = 0
    third = next_allowed(max(first + cfg.third_attempt_ms, second + cfg.third_min_gap_ms), tz, cfg)
    return Plan(first, sms, second, third, third + cfg.escalate_wait_ms)
