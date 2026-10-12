"""Forecast shift inside BatteryChargeService — applied, kept, reset, reported.

The shift has to live in the same computation as the price-derived start
(`refresh_start_charge`), otherwise every price fetch and every hourly tick
would put S0 back.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
    ForecastShiftMove,
)
from custom_components.smart_rce.domain.battery_charge_policy import BatteryChargePolicy
from custom_components.smart_rce.domain.charge_start_shift import ChargeStartShift

TODAY = date(2026, 10, 13)
S0 = time(12, 0)
H = 60


def _at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


class _Repo:
    def __init__(self, policy: BatteryChargePolicy) -> None:
        self.policy = policy
        self.saves = 0

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        self.saves += 1


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _service(
    policy: BatteryChargePolicy | None = None, now: datetime | None = None
) -> tuple[BatteryChargeService, _Clock]:
    clock = _Clock(now or _at(7, 41))
    service = BatteryChargeService(
        repo=_Repo(policy or BatteryChargePolicy()), clock=clock, actuator=MagicMock()
    )
    return service, clock


def _evaluate_three(service, clock, candidate, *, soc=None, start=(8, 1)):
    moves = []
    for i in range(3):
        clock.now = _at(start[0], start[1] + 5 * i)
        moves.append(service.evaluate_forecast_shift(clock.now, S0, candidate, soc))
    return moves


def test_three_earlier_candidates_move_the_override():
    service, clock = _service()
    service.refresh_start_charge(S0, clock.now)

    moves = _evaluate_three(service, clock, 10 * H)

    assert moves[:2] == [None, None]
    assert moves[2] == ForecastShiftMove(before=12 * H, after=10 * H, manual_start=None)
    assert service.start_charge_hour_override == time(10, 0)


def test_price_refresh_keeps_the_shifted_start():
    service, clock = _service()
    _evaluate_three(service, clock, 10 * H)

    service.refresh_start_charge(S0, _at(9, 0))  # hourly tick / price fetch

    assert service.start_charge_hour_override == time(10, 0)


def test_new_s0_from_prices_starts_the_shift_over():
    service, clock = _service()
    _evaluate_three(service, clock, 10 * H)

    service.refresh_start_charge(time(11, 0), _at(9, 0))

    assert service.start_charge_hour_override == time(11, 0)
    shift = service.forecast_shift
    assert shift is not None and (shift.s0, shift.start) == (11 * H, 11 * H)


def test_hand_set_start_wins_but_the_move_is_reported():
    service, clock = _service()
    service.refresh_start_charge(S0, clock.now)
    service._repo.policy.set_start_charge_hour_manual(time(11, 30), TODAY)

    moves = _evaluate_three(service, clock, 9 * H)

    assert moves[2] == ForecastShiftMove(
        before=12 * H, after=9 * H, manual_start=time(11, 30)
    )
    assert service.start_charge_hour_override == time(11, 30)


def test_yesterdays_shift_does_not_move_today():
    yesterday = TODAY - timedelta(days=1)
    policy = BatteryChargePolicy(
        forecast_shift=ChargeStartShift(day=yesterday, s0=12 * H, start=9 * H)
    )
    service, clock = _service(policy, _at(0, 0))

    service.refresh_start_charge(S0, clock.now)

    assert service.start_charge_hour_override == S0
    assert policy.forecast_shift is None


async def test_recompute_request_resets_an_unlocked_shift():
    service, clock = _service()
    _evaluate_three(service, clock, 10 * H)

    await service.force_sync_start_charge(time(11, 0))

    assert service.start_charge_hour_override == time(11, 0)


async def test_recompute_request_keeps_a_locked_shift():
    policy = BatteryChargePolicy(
        forecast_shift=ChargeStartShift(
            day=TODAY, s0=12 * H, start=10 * H, struck_start=10 * H, locked=True
        )
    )
    service, clock = _service(policy, _at(10, 30))

    await service.force_sync_start_charge(time(11, 0))

    assert service.start_charge_hour_override == time(10, 0)


def test_lock_baseline_survives_a_restart():
    service, clock = _service()
    _evaluate_three(service, clock, 10 * H)
    clock.now = _at(10, 1)
    service.evaluate_forecast_shift(clock.now, S0, 10 * H, 35.0)  # baseline

    restored = BatteryChargePolicy.from_dict(service._repo.policy.to_dict())
    shift = restored.forecast_shift
    assert shift is not None
    assert (shift.start, shift.soc_at_start) == (10 * H, 35.0)
    assert not shift.evaluate(10 * H + 6, 12 * H, 36.0) and shift.locked
