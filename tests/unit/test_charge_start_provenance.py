"""Charge-start provenance — manual values, tomorrow plans, restart safety.

The charge-start override is normally derived from RCE prices, but the user
can pin it by hand — for today, or ahead of time for tomorrow. Which of the
two wins is decided by comparing DATES held in `BatteryChargePolicy`, never
by having observed an event: `ChargeSlots` is rebuilt from scratch on every
restart, so anything gated on its in-memory previous value cannot tell
"unchanged" apart from "unknown". These tests pin that down, restarts and
missed midnights included.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
)
from custom_components.smart_rce.application.ems import Ems
from custom_components.smart_rce.domain.battery_charge_policy import (
    BatteryChargePolicy,
    ChargeStartPlan,
    ChargeStartSource,
)
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.rce import RceDayPrices, RcePrices

TODAY = date(2026, 9, 17)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)

COMPUTED = time(9, 30)
BY_HAND = time(10, 0)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


class _FakeChargeRepo:
    def __init__(self, policy: BatteryChargePolicy | None = None) -> None:
        self._policy = policy or BatteryChargePolicy()

    @property
    def policy(self) -> BatteryChargePolicy:
        return self._policy

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


def _service(
    now: datetime, policy: BatteryChargePolicy | None = None
) -> BatteryChargeService:
    return BatteryChargeService(
        repo=_FakeChargeRepo(policy), clock=lambda: now, actuator=MagicMock()
    )


# ─── auto-sync vs. hand-set value ───


def test_auto_sync_writes_when_nothing_was_set_by_hand():
    service = _service(_at(TODAY, 3))

    service.refresh_start_charge(COMPUTED, _at(TODAY, 3))

    assert service.start_charge_hour_override == COMPUTED


def test_auto_sync_leaves_a_value_set_by_hand_today_alone():
    policy = BatteryChargePolicy(
        start_charge_hour_override=BY_HAND, start_charge_manual_day=TODAY
    )
    service = _service(_at(TODAY, 3), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 3))

    assert service.start_charge_hour_override == BY_HAND


def test_manual_mark_expires_on_its_own_the_next_day():
    # Marked for YESTERDAY — no clearing step ever ran, and none is needed.
    policy = BatteryChargePolicy(
        start_charge_hour_override=BY_HAND, start_charge_manual_day=YESTERDAY
    )
    service = _service(_at(TODAY, 3), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 3))

    assert service.start_charge_hour_override == COMPUTED


def test_auto_sync_is_a_noop_without_a_computed_window():
    policy = BatteryChargePolicy(start_charge_hour_override=BY_HAND)
    service = _service(_at(TODAY, 3), policy)

    service.refresh_start_charge(None, _at(TODAY, 3))

    assert service.start_charge_hour_override == BY_HAND


async def test_setting_todays_start_by_hand_marks_it():
    service = _service(_at(TODAY, 15))

    await service.set_start_charge_hour_manual(BY_HAND)

    assert service.start_charge_hour_override == BY_HAND
    assert service.start_charge_manual_day == TODAY


async def test_force_sync_drops_the_manual_mark():
    # Turning a charge-window knob is an explicit "recompute this for me".
    service = _service(_at(TODAY, 15))
    await service.set_start_charge_hour_manual(BY_HAND)

    await service.force_sync_start_charge(COMPUTED)

    assert service.start_charge_hour_override == COMPUTED
    assert service.start_charge_manual_day is None


# ─── tomorrow plans ───


async def test_planning_tomorrow_does_not_touch_today():
    policy = BatteryChargePolicy(start_charge_hour_override=time(9, 0))
    service = _service(_at(TODAY, 22), policy)

    await service.set_tomorrow_plan(BY_HAND)

    assert service.tomorrow_plan == ChargeStartPlan(day=TOMORROW, value=BY_HAND)
    assert service.start_charge_hour_override == time(9, 0)
    assert service.start_charge_manual_day is None


def test_a_plan_for_tomorrow_is_left_pending():
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND)
    )
    service = _service(_at(TODAY, 22), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 22))

    assert service.tomorrow_plan is not None
    assert service.start_charge_hour_override == COMPUTED


def test_a_plan_naming_today_is_promoted_and_consumed():
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TODAY, value=BY_HAND)
    )
    service = _service(_at(TODAY, 0, 5), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 0, 5))

    assert service.start_charge_hour_override == BY_HAND
    assert service.start_charge_manual_day == TODAY
    assert service.tomorrow_plan is None


def test_promotion_wins_over_the_computed_window_in_the_same_pass():
    # Order guard: were the sync to run first, COMPUTED would briefly occupy
    # the override and the actuator could act on it.
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TODAY, value=BY_HAND)
    )
    service = _service(_at(TODAY, 0, 5), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 0, 5))

    assert service.start_charge_hour_override == BY_HAND


def test_promotion_still_happens_when_ha_slept_through_midnight():
    # First tick of the day is at 08:00 — no rotation was ever observed.
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TODAY, value=BY_HAND)
    )
    service = _service(_at(TODAY, 8), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 8))

    assert service.start_charge_hour_override == BY_HAND
    assert service.tomorrow_plan is None


def test_a_plan_whose_day_has_passed_is_dropped_unused():
    policy = BatteryChargePolicy(
        start_charge_hour_override=time(9, 0),
        tomorrow_plan=ChargeStartPlan(day=YESTERDAY, value=BY_HAND),
    )
    service = _service(_at(TODAY, 8), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 8))

    assert service.tomorrow_plan is None
    assert service.start_charge_hour_override == COMPUTED
    assert service.start_charge_manual_day is None


# ─── restart safety — the whole point of dating the mark ───


def test_a_promoted_plan_survives_a_restart_in_the_small_hours():
    # Restart at 04:30: policy comes back from the Store, ChargeSlots does
    # not. The old `0 <= hour < 6` gate would have overwritten BY_HAND here.
    policy = BatteryChargePolicy(
        start_charge_hour_override=BY_HAND, start_charge_manual_day=TODAY
    )
    service = _service(_at(TODAY, 4, 30), policy)

    service.refresh_start_charge(COMPUTED, _at(TODAY, 4, 30))

    assert service.start_charge_hour_override == BY_HAND


def test_a_restart_still_picks_up_a_genuinely_changed_window():
    # Nothing was set by hand, so a corrected price must land even though the
    # in-memory "previous" value is gone.
    policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
    service = _service(_at(TODAY, 4, 30), policy)

    service.refresh_start_charge(time(8, 0), _at(TODAY, 4, 30))

    assert service.start_charge_hour_override == time(8, 0)


def test_a_pending_plan_survives_a_restart_before_its_day():
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND)
    )
    _service(_at(TODAY, 23, 30), policy).refresh_start_charge(
        COMPUTED, _at(TODAY, 23, 30)
    )
    # Same policy comes back from the Store; ChargeSlots starts over.
    service = _service(_at(TOMORROW, 6), policy)

    service.refresh_start_charge(COMPUTED, _at(TOMORROW, 6))

    assert service.start_charge_hour_override == BY_HAND
    assert service.start_charge_manual_day == TOMORROW


# ─── persistence ───


def test_plan_and_mark_survive_a_store_round_trip():
    policy = BatteryChargePolicy(
        start_charge_hour_override=BY_HAND,
        start_charge_manual_day=TODAY,
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=time(7, 15)),
    )

    restored = BatteryChargePolicy.from_dict(policy.to_dict())

    assert restored.start_charge_hour_override == BY_HAND
    assert restored.start_charge_manual_day == TODAY
    assert restored.tomorrow_plan == ChargeStartPlan(day=TOMORROW, value=time(7, 15))


def test_a_store_written_before_this_change_restores_as_automatic():
    legacy = {"start_charge_hour_override": "09:30:00"}

    restored = BatteryChargePolicy.from_dict(legacy)

    assert restored.start_charge_hour_override == COMPUTED
    assert restored.start_charge_manual_day is None
    assert restored.tomorrow_plan is None


def test_unparsable_dates_restore_as_absent_rather_than_raising():
    restored = BatteryChargePolicy.from_dict(
        {
            "start_charge_manual_day": "not-a-date",
            "tomorrow_plan": {"day": "2026-09-18", "value": "nonsense"},
        }
    )

    assert restored.start_charge_manual_day is None
    assert restored.tomorrow_plan is None


def test_a_plan_that_is_not_even_a_mapping_is_ignored():
    assert (
        BatteryChargePolicy.from_dict({"tomorrow_plan": "10:00"}).tomorrow_plan is None
    )


# ─── Ems joins the two aggregates for the dashboard ───


def _ems(now: datetime, policy: BatteryChargePolicy | None = None) -> Ems:
    return Ems(
        dod_policy=MagicMock(),
        grid_export=GridExportManager(),
        water_heater=MagicMock(),
        battery_schedule_service=MagicMock(),
        battery_charge_service=_service(now, policy),
        water_heater_reserved_service=MagicMock(),
        dod_repository=MagicMock(),
        dod_logger=MagicMock(),
        dod_actuator=MagicMock(),
        goodwe_ems_actuator=MagicMock(),
    )


def _valley_at(hour: int) -> list[float]:
    prices = [500.0] * 24
    prices[hour] = prices[hour + 1] = 10.0
    return prices


def _feed_prices(ems: Ems, now: datetime, *, tomorrow: list[float] | None) -> None:
    ems.update_rce(
        now,
        RcePrices(
            fetched_at=now,
            today=RceDayPrices(
                published_at=None, day=TODAY, hour_price=tuple(_valley_at(12))
            ),
            tomorrow=(
                RceDayPrices(
                    published_at=None, day=TOMORROW, hour_price=tuple(tomorrow)
                )
                if tomorrow
                else None
            ),
        ),
    )


def test_tomorrow_view_falls_back_to_the_computed_window():
    now = _at(TODAY, 22)
    ems = _ems(now)

    _feed_prices(ems, now, tomorrow=_valley_at(11))

    assert ems.charge_start_tomorrow == ems.charge_slots.tomorrow_start
    assert ems.charge_start_tomorrow is not None


def test_tomorrow_view_is_unknown_before_prices_are_published():
    now = _at(TODAY, 10)
    ems = _ems(now)

    _feed_prices(ems, now, tomorrow=None)

    assert ems.charge_start_tomorrow is None


async def test_a_pinned_plan_wins_over_the_computed_window():
    now = _at(TODAY, 22)
    ems = _ems(now)
    _feed_prices(ems, now, tomorrow=_valley_at(11))
    computed = ems.charge_start_tomorrow

    await ems.set_charge_start_tomorrow(BY_HAND)

    assert computed != BY_HAND
    assert ems.charge_start_tomorrow == BY_HAND


# ─── provenance readout ───


def test_source_reads_auto_when_nothing_is_pinned():
    assert BatteryChargePolicy().start_charge_source(TODAY) is ChargeStartSource.AUTO


def test_source_reads_manual_today():
    policy = BatteryChargePolicy(start_charge_manual_day=TODAY)

    assert policy.start_charge_source(TODAY) is ChargeStartSource.MANUAL_TODAY


def test_source_reads_manual_tomorrow():
    policy = BatteryChargePolicy(
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND)
    )

    assert policy.start_charge_source(TODAY) is ChargeStartSource.MANUAL_TOMORROW


def test_source_reads_manual_both():
    policy = BatteryChargePolicy(
        start_charge_manual_day=TODAY,
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND),
    )

    assert policy.start_charge_source(TODAY) is ChargeStartSource.MANUAL_BOTH


def test_a_spent_manual_mark_reads_as_auto():
    # The mark is never cleared, only outdated — it must not read as manual.
    policy = BatteryChargePolicy(start_charge_manual_day=YESTERDAY)

    assert policy.start_charge_source(TODAY) is ChargeStartSource.AUTO


def test_service_resolves_the_source_against_its_own_clock():
    policy = BatteryChargePolicy(start_charge_manual_day=TODAY)

    assert _service(_at(TODAY, 9), policy).start_charge_source is (
        ChargeStartSource.MANUAL_TODAY
    )
    assert _service(_at(TOMORROW, 9), policy).start_charge_source is (
        ChargeStartSource.AUTO
    )


async def test_a_param_change_releases_tomorrow_back_to_automatic():
    # Turning a knob is how the user asks for a recompute; a plan surviving it
    # would keep showing the old choice and hide the effect of the change.
    policy = BatteryChargePolicy(
        start_charge_manual_day=TODAY,
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND),
    )
    service = _service(_at(TODAY, 15), policy)

    await service.force_sync_start_charge(COMPUTED)

    assert service.start_charge_source is ChargeStartSource.AUTO
    assert service.tomorrow_plan is None


async def test_marks_are_released_even_without_prices_for_today():
    policy = BatteryChargePolicy(
        start_charge_manual_day=TODAY,
        tomorrow_plan=ChargeStartPlan(day=TOMORROW, value=BY_HAND),
    )
    service = _service(_at(TODAY, 15), policy)

    await service.force_sync_start_charge(None)

    assert service.start_charge_source is ChargeStartSource.AUTO
