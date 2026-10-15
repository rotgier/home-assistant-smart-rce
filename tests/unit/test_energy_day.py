"""EnergyDay: 5-minute slot grid anchored at local midnight, DST-safe, lossless JSON."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.energy_day import EnergyDay
import orjson
import pytest

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


def _full_day(day: date, kwh: float = 0.01) -> EnergyDay:
    """Every 5-min slot of the day filled with `kwh` (both series)."""
    start = datetime(day.year, day.month, day.day, tzinfo=_TZ).astimezone(UTC)
    end = (
        datetime(day.year, day.month, day.day, tzinfo=_TZ) + timedelta(days=1)
    ).astimezone(UTC)
    count = int((end - start) / EnergyDay.STEP)
    readings = {start + i * EnergyDay.STEP: kwh for i in range(count)}
    return EnergyDay.from_readings(
        day, _TZ, {"consumption_kwh": readings, "pv_kwh": readings}
    )


def test_half_hours_sum_six_slots_and_drop_incomplete_ones() -> None:
    day = date(2026, 10, 12)
    energy = _full_day(day)
    energy.series["consumption_kwh"][7 * 12 + 2] = None  # 07:10 missing
    halves = energy.energy_by_half_hour("consumption_kwh", _TZ)
    assert len(halves) == 47
    assert 14 not in halves  # 07:00-07:30 lost a value
    assert halves[15] == pytest.approx(0.06)  # 07:30-08:00
    assert energy.energy_by_half_hour("missing_series", _TZ) == {}


def test_half_hours_of_an_archived_dst_day_follow_the_zone() -> None:
    """Read back, `start` keeps a fixed +02:00; the zone moves 03:00 to CET."""
    day = date(2026, 10, 25)
    energy = EnergyDay.from_dict(orjson.loads(orjson.dumps(_full_day(day).to_dict())))
    noon_cet = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    energy.series["consumption_kwh"][
        int((noon_cet - energy.start.astimezone(UTC)) / EnergyDay.STEP)
    ] = 1.0
    halves = energy.energy_by_half_hour("consumption_kwh", _TZ)
    assert len(halves) == 48
    assert halves[4] == pytest.approx(0.12)  # 02:00-02:30 happens twice
    assert halves[24] == pytest.approx(1.05)  # 12:00 CET, not 13:00 (+02:00)
    assert halves[26] == pytest.approx(0.06)


def test_from_records_picks_the_energy_record() -> None:
    day = date(2026, 10, 12)
    energy = _full_day(day)
    assert EnergyDay.from_records([{"kind": "other"}, energy.to_dict()]) is not None
    assert EnergyDay.from_records([{"kept": True}]) is None


def test_completeness_and_improvement() -> None:
    day = date(2026, 10, 12)
    full = _full_day(day)
    late = _full_day(day)
    late.series["consumption_kwh"][-1] = None  # 23:55 not compiled yet
    assert full.is_complete()
    assert not late.is_complete()
    assert full.improves_on(late)
    assert not late.improves_on(full)
    assert not full.improves_on(full)
    other = _full_day(day)
    other.series["pv_kwh"][0] = None
    assert not other.improves_on(late)  # gains consumption, loses pv
