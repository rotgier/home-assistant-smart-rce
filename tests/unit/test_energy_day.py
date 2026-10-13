"""EnergyDay: 5-minute slot grid anchored at local midnight, DST-safe, lossless JSON."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.energy_day import EnergyDay
import orjson

_TZ = ZoneInfo("Europe/Warsaw")


def _local(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_TZ)


def test_regular_day_has_288_slots_indexed_from_midnight() -> None:
    day = date(2026, 10, 12)
    energy = EnergyDay.from_readings(
        day,
        _TZ,
        {"pv_kwh": {_local(day, 0, 0): 0.0, _local(day, 12, 25): 0.24699999999938882}},
    )
    assert energy.slot_count == 288
    assert energy.series["pv_kwh"][0] == 0.0
    assert energy.series["pv_kwh"][12 * 12 + 5] == 0.247  # rounded, float noise gone
    assert energy.filled_slots("pv_kwh") == 2


def test_readings_outside_the_day_and_none_values_are_dropped() -> None:
    day = date(2026, 10, 12)
    energy = EnergyDay.from_readings(
        day,
        _TZ,
        {
            "pv_kwh": {
                _local(day, 23, 55) + timedelta(minutes=5): 9.9,  # next day 00:00
                _local(day, 0, 0) - timedelta(minutes=5): 9.9,  # previous day
                _local(day, 6, 0): None,
            }
        },
    )
    assert energy.filled_slots("pv_kwh") == 0


def test_dst_days_have_their_real_length() -> None:
    autumn = date(2026, 10, 25)  # clocks go back: 25 h
    spring = date(2026, 3, 29)  # clocks go forward: 23 h
    assert EnergyDay.from_readings(autumn, _TZ, {"pv_kwh": {}}).slot_count == 300
    assert EnergyDay.from_readings(spring, _TZ, {"pv_kwh": {}}).slot_count == 276


def test_both_autumn_two_oclock_slots_are_kept_apart() -> None:
    """02:00 happens twice on the autumn DST day — absolute time keeps both."""
    day = date(2026, 10, 25)
    first = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST
    second = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 02:00 CET
    energy = EnergyDay.from_readings(day, _TZ, {"pv_kwh": {first: 1.0, second: 2.0}})
    assert energy.series["pv_kwh"][24] == 1.0
    assert energy.series["pv_kwh"][36] == 2.0


def test_round_trip_through_json() -> None:
    day = date(2026, 10, 12)
    energy = EnergyDay.from_readings(
        day,
        _TZ,
        {
            "pv_kwh": {_local(day, 12, 0): 0.382},
            "grid_net_export_kwh": {_local(day, 3, 0): -0.03},
            "battery_soc_pct": {_local(day, 0, 0): 68.0},
        },
        source="recorder_dump",
    )
    record = orjson.loads(orjson.dumps(energy.to_dict()))
    assert EnergyDay.from_dict(record) == energy
    assert record["kind"] == "energy_day"
    assert record["step_minutes"] == 5
    assert "+ export, - import" in record["units"]["grid_net_export_kwh"]
