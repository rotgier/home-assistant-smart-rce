"""Forecast scoreboard — parity with the reference script, decisions, facts, ranking."""

import dataclasses
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain import charge_curve
from custom_components.smart_rce.domain.battery_schedule.morning_discharge import (
    MorningDischargePlan,
)
from custom_components.smart_rce.domain.charge_curve import SocBand
from custom_components.smart_rce.domain.charge_slots import ChargeSlots
from custom_components.smart_rce.domain.charge_start_shift import full_charge_minute
from custom_components.smart_rce.domain.consumption_percentile import percentile_by_key
from custom_components.smart_rce.domain.consumption_profiles import ConsumptionProfile
from custom_components.smart_rce.domain.energy_day import EnergyDay
from custom_components.smart_rce.domain.forecast_score import (
    day_score as day_score_module,
    variant_results as vr,
)
from custom_components.smart_rce.domain.forecast_score.consumption_models import (
    ConsumptionModel,
)
from custom_components.smart_rce.domain.forecast_score.day_actuals import DayActuals
from custom_components.smart_rce.domain.forecast_score.day_score import (
    DayInputs,
    DayScore,
    Pending,
    ScoreStatus,
    _without_diagnostics,
    has_morning_reserve,
    score_day,
)
from custom_components.smart_rce.domain.forecast_score.full_charge_score import (
    PlanReading,
    decided_plan,
    score_full_charge,
)
from custom_components.smart_rce.domain.forecast_score.production_outcome import (
    ChargeStartFact,
    production_outcome,
    start_from_soc_rise,
    struck_start,
)
from custom_components.smart_rce.domain.forecast_score.production_setup import (
    CURRENT,
    MarginRule,
    setup_on,
)
from custom_components.smart_rce.domain.forecast_score.ranking import rank
from custom_components.smart_rce.domain.forecast_score.reserve_score import (
    score_reserve,
)
from custom_components.smart_rce.domain.forecast_score.start_score import (
    forecast_half_hours,
    last_evaluation_before,
    score_start,
)
from custom_components.smart_rce.domain.pv_forecast import PvForecast
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (
    ForecastSnapshot,
)
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    surplus_window_end,
)
from custom_components.smart_rce.domain.rce import RceDayPrices
from custom_components.smart_rce.domain.target_soc import (
    TargetSoc,
    TargetSocContext,
    target_soc_for,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.forecast_score_archive import (
    ForecastScoreArchive,
)
import pytest

TZ = ZoneInfo("Europe/Warsaw")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "forecast_score"
DAYS = (date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14))
_BANDS_AT_FA81958 = (
    SocBand(10, 20, 0.148),
    SocBand(20, 30, 0.112),
    SocBand(30, 50, 0.114),
    SocBand(50, 75, 0.118),
    SocBand(75, 90, 0.108),
    SocBand(90, 100, 0.119),
)
CONSUMPTION = "consumption_minus_water_kwh"
VERDICTS = {
    "na czas": "on_time",
    "za wcześnie": "too_early",
    "za późno": "too_late",
    "słusznie nie starczy": "rightly_short",
    "niesłusznie nie starczy": "wrongly_short",
}
MODELS = {
    "flat 0,9 kWh/h": "flat",
    "poprz. dni: mediana (prod 15.10)": "p50",
    "poprz. dni: p60 (prod od 15.10 wieczór)": "p60",
    "prod do 14.10: max(flat, bez najg.)": "old_rule",
}


def _at_fa81958(scored: list[vr.ScoredSnapshot]) -> list[vr.ScoredSnapshot]:
    """Read snapshots as the reference script did: only those taken before a moment."""
    return [dataclasses.replace(x, solcast_fetched_at=None) for x in scored]


@pytest.fixture
def charge_curve_at_fa81958(monkeypatch):
    """Run on the charge curve the reference script had (before 18.10.2026)."""
    monkeypatch.setattr(charge_curve, "BANDS", _BANDS_AT_FA81958)


@pytest.fixture(scope="module")
def energy() -> dict[date, EnergyDay]:
    archive = DailyJsonlArchive(FIXTURE / "energy")
    out = {}
    day = date(2026, 10, 3)
    while day <= DAYS[-1]:
        found = EnergyDay.from_records(archive.read_day(day))
        if found is not None:
            out[day] = found
        day += timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def snapshots() -> dict[date, list[ForecastSnapshot]]:
    archive = DailyJsonlArchive(FIXTURE / "forecast")
    return {
        day: [ForecastSnapshot.from_dict(r) for r in archive.read_day(day)]
        for day in DAYS
    }


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads((FIXTURE / "expected_fa81958.json").read_text())["days"]


@pytest.fixture(scope="module")
def prices() -> dict:
    return json.loads((FIXTURE / "rce_prices.json").read_text())


def _earlier_workdays(day: date, energy: dict[date, EnergyDay]) -> list[dict]:
    """Walk back like the script: up to 8 Mon-Fri days within 20 days."""
    out = []
    back = day - timedelta(days=1)
    while len(out) < 8 and (day - back).days <= 20:
        if back.weekday() < 5 and back in energy:
            out.append(energy[back].energy_by_half_hour(CONSUMPTION, TZ))
        back -= timedelta(days=1)
    return out


def _s0(day: date, prices: dict) -> int:
    window = ChargeSlots.compute(
        RceDayPrices(None, day, tuple(prices[day.isoformat()]))
    )
    assert window is not None
    return int(window.start_hour * 60)


class TestParityWithReferenceScript:
    """The domain reproduces scripts/forecast_decision_score.py at fa81958."""

    @pytest.mark.parametrize("day", DAYS)
    def test_reserve_variants_models_and_split(self, day, energy, snapshots, expected):
        replayed = _at_fa81958(vr.replay(snapshots[day]))
        moments, models = score_reserve(
            DayActuals(energy[day], TZ), replayed, _earlier_workdays(day, energy)
        )
        want = expected[day.isoformat()]
        # The script signed reserve errors needed − system; the domain signs
        # them system − needed (15.10.2026), hence the minus on its numbers.
        for label, name, with_model, with_actual in want["d1"]:
            moment = next(m for m in moments if m.label == label)
            variant = moment.variant(name)
            assert variant is not None, (label, name)
            assert (variant.pv_error_pp, variant.pv_error_actual_cons_pp) == (
                -with_model,
                -with_actual,
            ), (label, name)
        by_model = {m.model: m for m in models}
        for name, error, earlier in want["models"]:
            if name in MODELS:
                assert by_model[MODELS[name]].error_pp == -error, name
                assert by_model[MODELS[name]].earlier_days == earlier
        main = next(m for m in moments if m.label == "06:11")
        production = want["production"]
        assert main.needed == production["needed"]
        assert main.needed_strict == production["needed_strict"]
        assert [main.pv_part_pp, main.consumption_part_pp] == [
            -part for part in production["split"]
        ]

    @pytest.mark.parametrize("day", DAYS)
    @pytest.mark.usefixtures("charge_curve_at_fa81958")
    def test_start_on_actual_consumption(
        self, day, energy, snapshots, expected, prices
    ):
        moments = score_start(
            DayActuals(energy[day], TZ),
            _at_fa81958(vr.replay(snapshots[day])),
            prices[day.isoformat()],
            _s0(day, prices),
            [],
            None,
        )
        for label, name, verdict, delta_h, short, early in expected[day.isoformat()][
            "d2"
        ]:
            moment = next(m for m in moments if m.label == label)
            if name == "cons: flat":
                call = next(m for m in moment.models if m.model == "flat").call
            elif name.startswith("cons:"):
                continue  # the script's 5-day median is not production's model
            else:
                found = moment.variant(name)
                assert found is not None
                call = found.call_actual_cons
            assert (str(call.verdict), call.delta_h, call.shortfall_kwh) == (
                VERDICTS[verdict],
                delta_h,
                short,
            ), (label, name)
            assert call.early_kwh == early


class TestVariantResults:
    def test_replay_leaves_production_singletons_alone(self, snapshots):
        before = {v: v.strategy.result for v in PvForecast}
        vr.replay(snapshots[DAYS[-1]])
        assert {v: v.strategy.result for v in PvForecast} == before

    def test_replay_extrapolates_its_own_live(self, snapshots):
        replayed = vr.replay(snapshots[DAYS[-1]])
        noon = vr.consumed(replayed, datetime(2026, 10, 14, 11, 0, tzinfo=TZ))
        assert noon is not None
        assert noon.result(PvForecast.EXTRAP_PROPORTIONAL.key) is not None

    def test_archive_keeps_production_results_and_adds_the_rest(self, snapshots):
        day = snapshots[DAYS[-1]]  # 14.10: live snapshots, Live Soft alpha 0.5
        scored = vr.from_archive(day)
        at = vr.consumed(scored, datetime(2026, 10, 14, 8, 41, tzinfo=TZ))
        assert at is not None
        archived = next(s for s in day if s.taken_at == at.taken_at)
        live = archived.variants[PvForecast.LIVE.key].result
        assert at.result(PvForecast.LIVE.key) == live
        # Live Soft is recomputed per alpha: 0.5 equals what production ran on
        # 14.10, the ranking's `live_soft` is today's 0.4.
        production = archived.variants[PvForecast.LIVE_SOFT.key].result
        assert at.result("live_soft_0.5") == production
        assert at.result(vr.PRODUCTION_VARIANT) != production
        assert at.result("live_soft_0.3") is not None
        assert at.result(vr.RAW_SOLCAST) is not None

    def test_consumed_is_the_latest_before_the_decision(self, snapshots):
        scored = vr.from_archive(snapshots[DAYS[0]])
        decision = datetime(2026, 10, 12, 6, 11, tzinfo=TZ)
        found = vr.consumed(scored, decision)
        assert found is not None
        assert found.taken_at <= decision
        assert all(
            s.taken_at <= found.taken_at or s.taken_at > decision for s in scored
        )

    def test_consumed_takes_the_snapshot_of_a_fetch_before_the_decision(
        self, snapshots
    ):
        """20.10.2026: fetch 06:40:44, reserve moved 06:40:45, snapshot 06:41:04."""
        earlier, later = vr.from_archive(snapshots[DAYS[0]])[:2]
        at = lambda h, m, s=0: datetime(2026, 10, 20, h, m, s, tzinfo=TZ)  # noqa: E731
        old = dataclasses.replace(earlier, taken_at=at(6, 10, 58))
        fresh = dataclasses.replace(
            later, taken_at=at(6, 41, 4), solcast_fetched_at=at(6, 40, 44)
        )
        assert vr.consumed([old, fresh], at(6, 41)) is fresh
        # A fetch after the decision is not what it ran on.
        late = dataclasses.replace(fresh, solcast_fetched_at=at(6, 41, 10))
        assert vr.consumed([old, late], at(6, 41)) is old
        # Nor a snapshot well past the decision.
        stale = dataclasses.replace(fresh, taken_at=at(6, 43))
        assert vr.consumed([old, stale], at(6, 41)) is old


class TestTargetSocFor:
    def test_matches_the_target_soc_entity_inside_the_window(self, snapshots):
        snap = vr.consumed(
            vr.from_archive(snapshots[DAYS[-1]]),
            datetime(2026, 10, 14, 8, 41, tzinfo=TZ),
        )
        assert snap is not None
        ctx = snap.context
        target_ctx = TargetSocContext(
            target_date=DAYS[-1],
            signals=ctx.signals,
            live_consumption_w=ctx.consumption_w,
            start_charge_hour=ctx.start_charge_hour,
            now=ctx.now,
            now_in_window=True,
        )
        cons = ConsumptionProfile.flat(0.4)

        class _Variant:  # what TargetSoc reads of a PvForecast member
            result = snap.result(PvForecast.LIVE.key)
            is_today = True

        entity = TargetSoc(variant=_Variant())  # type: ignore[arg-type]
        entity.recalculate(ConsumptionProfile.flat(), [cons], target_ctx)
        result = snap.result(PvForecast.LIVE.key)
        assert result is not None
        assert (
            target_soc_for(result, cons, target_ctx, is_today=True)
            == (entity.prev_days[0])
        )


class TestStart:
    @pytest.mark.parametrize(
        ("start", "evaluated"),
        [(9 * 60 + 21, 9 * 60 + 16), (9 * 60 + 16, 9 * 60 + 11), (7 * 60 + 41, None)],
    )
    def test_last_evaluation_before_the_start(self, start, evaluated):
        assert last_evaluation_before(start) == evaluated

    def test_struck_start_takes_the_start_in_force_when_the_clock_reaches_it(self):
        # 00:00 carried 09:00 from yesterday, 00:01 S0 11:00, 08:46 moved to 09:00.
        changes = [(0, 540), (1, 660), (526, 540)]
        assert struck_start(changes) == 540
        # A start set when its time has passed strikes at once.
        assert struck_start([(0, 660), (600, 540)]) == 600
        assert struck_start([]) is None

    def test_start_moment_scored_before_the_actual_start(
        self, energy, snapshots, prices
    ):
        day = DAYS[-1]
        moments = score_start(
            DayActuals(energy[day], TZ),
            vr.from_archive(snapshots[day]),
            prices[day.isoformat()],
            _s0(day, prices),
            [],
            8 * 60 + 15,
        )
        start = next(m for m in moments if m.label == "start")
        assert start.minute == 8 * 60 + 11
        assert start.production is not None
        # The production error splits exactly into the PV and consumption parts.
        assert start.pv_part_h is not None
        assert start.consumption_part_h is not None
        assert start.production.call.delta_h == pytest.approx(
            start.pv_part_h + start.consumption_part_h
        )


class TestProductionOutcome:
    def test_facts_of_a_day(self, energy):
        actuals = DayActuals(energy[DAYS[-1]], TZ)
        outcome = production_outcome(
            actuals, ChargeStartFact(495, "override"), [(14 * 60, 15 * 60)]
        )
        assert outcome.min_soc_t1 is not None
        assert outcome.t1_import_kwh is not None
        assert outcome.topups is not None
        topup = outcome.topups[0]
        assert topup.grid_kwh == pytest.approx(
            max(0.0, (topup.battery_kwh or 0.0) - topup.pv_surplus_kwh)
        )

    def test_unknown_afternoon_spans_leave_the_top_up_unknown(self, energy):
        actuals = DayActuals(energy[DAYS[-1]], TZ)
        outcome = production_outcome(actuals, ChargeStartFact(None, "none"), None)
        assert outcome.grid_topup_kwh is None
        assert production_outcome(
            actuals, ChargeStartFact(None, "none"), []
        ).grid_topup_kwh == pytest.approx(0.0)

    def test_soc_rise_finds_a_morning_start(self, energy):
        rise = start_from_soc_rise(DayActuals(energy[DAYS[-1]], TZ))
        assert rise is not None
        assert 7 * 60 <= rise < 16 * 60


def _day_score(  # noqa: PLR0913 — test helper
    day, energy, snapshots, prices, *, cutoff=None, plan=None, s0=None, readings=()
) -> DayScore:
    actuals = DayActuals(energy[day], TZ)
    earlier = {
        d: energy[d].energy_by_half_hour(CONSUMPTION, TZ)
        for d in sorted(energy)
        if d < day and d.weekday() < 5
    }
    return score_day(
        DayInputs(
            actuals=actuals,
            snapshots=vr.from_archive(snapshots[day]),
            earlier_workdays=dict(list(earlier.items())[-8:]),
            earlier_same_type=dict(list(earlier.items())[-10:]),
            prices=prices[day.isoformat()],
            s0=_s0(day, prices) if s0 is None else s0,
            start=ChargeStartFact(start_from_soc_rise(actuals), "soc_rise"),
            afternoon_charge=None,
            morning_plan=plan,
            plan_readings=readings,
            cutoff=cutoff,
        ),
        status=ScoreStatus.FINAL if cutoff is None else ScoreStatus.PRELIMINARY,
        computed_at=cutoff or datetime(2026, 10, 15, 21, 6, tzinfo=TZ),
        code_version="test",
    )


class TestProductionSetup:
    def test_a_day_runs_on_the_setup_in_force_at_0611(self):
        assert setup_on(date(2026, 10, 13), TZ).pv_variant == "live"
        assert setup_on(date(2026, 10, 14), TZ).pv_variant == "live_soft_0.5"
        assert setup_on(date(2026, 10, 15), TZ).reserve_model is ConsumptionModel.P50
        assert setup_on(date(2026, 10, 16), TZ).margin is MarginRule.LIVE_CAPS_0809
        assert setup_on(date(2026, 10, 17), TZ).margin is MarginRule.CAPPED
        assert setup_on(date(2026, 10, 18), TZ) is CURRENT
        assert CURRENT.margin is MarginRule.BLEND_CAPS

    def test_production_panel_uses_the_days_rule_and_buffer(
        self, energy, snapshots, prices
    ):
        score = _day_score(DAYS[-1], energy, snapshots, prices)  # 14.10
        main = score.reserve_moment("06:11")
        assert main is not None
        assert main.production is not None
        assert main.production.setup == setup_on(DAYS[-1], TZ).label
        # Buffer 12 that day against today's 10.
        assert main.production.needed == main.needed + 2
        assert score.setup == main.production.setup


class TestMargin:
    def test_the_system_reserve_carries_the_time_of_day_margin(self, energy, snapshots):
        day = DAYS[-1]
        assert setup_on(date(2026, 10, 16), TZ).margin
        moments, _models = score_reserve(
            DayActuals(energy[day], TZ),
            vr.from_archive(snapshots[day]),
            _earlier_workdays(day, energy),
            CURRENT,  # the margin setup, scored on 14.10's data
        )
        margins = {m.label: m.production.margin_pp for m in moments if m.production}
        assert margins == {
            "06:11": 15,
            "06:41": 15,
            "07:41": 10,
            "08:41": 5,
            "09:41": 0,
        }
        for m in moments:
            assert m.production is not None
            assert m.production_error_pp == (
                (m.pv_part_pp or 0)
                + (m.consumption_part_pp or 0)
                + (m.margin_part_pp or 0)
            )


class TestDayScore:
    def test_round_trips_through_the_archive_form(self, energy, snapshots, prices):
        score = _day_score(DAYS[-1], energy, snapshots, prices)
        assert DayScore.from_dict(json.loads(json.dumps(score.to_dict()))) == score

    def test_production_error_splits_into_pv_and_consumption(
        self, energy, snapshots, prices
    ):
        score = _day_score(DAYS[-1], energy, snapshots, prices)
        assert score.reserve_error_pp is not None
        assert score.reserve_error_pp == (
            (score.reserve_error_pv_pp or 0) + (score.reserve_error_consumption_pp or 0)
        )
        assert [m.label for m in score.reserve] == [
            "06:11",
            "06:41",
            "07:41",
            "08:41",
            "09:41",
        ]
        assert score.day_type.kind is not None

    def test_latest_of_prefers_the_last_final(self, energy, snapshots, prices):
        score = _day_score(DAYS[-1], energy, snapshots, prices)
        preliminary = {**score.to_dict(), "status": "preliminary"}
        replaced = {**score.to_dict(), "replaced": ["outcome"]}
        latest = DayScore.latest_of([score.to_dict(), replaced, preliminary])
        assert latest is not None
        assert latest.replaced == ("outcome",)


class TestRanking:
    def test_tables_per_kind(self, energy, snapshots, prices):
        scores = [_day_score(d, energy, snapshots, prices) for d in DAYS]
        ranking = rank(scores)
        assert ranking["days"] == [d.isoformat() for d in DAYS]
        rows = {r["variant"]: r for r in ranking["reserve"]["all"]["06:11"]}
        assert rows["solcast_p50"]["n"] == 3
        assert rows["solcast_p50"]["ties"] == 3
        assert set(ranking["reserve"]["cloudy"]) <= {"06:11", "08:41"}


class TestScoreArchive:
    def test_a_closed_day_is_rewritten_not_appended(self, tmp_path):
        archive = ForecastScoreArchive(DailyJsonlArchive(tmp_path))
        day = date(2026, 10, 14)
        archive.add(day, {"n": 1})
        assert archive.close_days_before(date(2026, 10, 15)) == [day]
        archive.add(day, {"n": 2})
        assert archive.records(day) == [{"n": 1}, {"n": 2}]
        assert not (tmp_path / "2026" / "2026-10-14.jsonl").exists()


def test_day_actuals_from_now_counts_only_what_came_after(energy):
    actuals = DayActuals(energy[DAYS[-1]], TZ)
    whole = actuals.window_buckets("pv_kwh")
    now = datetime.combine(DAYS[-1], time(8, 41), TZ)
    later = actuals.window_buckets("pv_kwh", now)
    assert later.get(8, 0) == 0.0
    assert later.get(9, 0) == whole.get(9, 0)
    assert 0.0 <= later.get(8, 30) <= whole.get(8, 30)


# 16-17.10: 07-08 capped by max(Live, Prop, Blend), 08-09 by Live (43d266b).
_CAPPED = setup_on(date(2026, 10, 17), TZ)


def test_the_0809_step_was_capped_by_live(energy, snapshots):
    """CAPPED, 08-09: max(0, min(5, Live - Soft)), as production computed it."""
    day = DAYS[-1]
    moments, _ = score_reserve(
        DayActuals(energy[day], TZ),
        vr.from_archive(snapshots[day]),
        _earlier_workdays(day, energy),
        _CAPPED,
    )
    line = next(m for m in moments if m.label == "08:41")
    soft, live = line.variant("live_soft"), line.variant("live")
    assert line.production is not None
    assert soft is not None
    assert live is not None
    assert line.production.margin_pp == max(0, min(5, live.reserve - soft.reserve))
    assert _CAPPED.margin is MarginRule.CAPPED


def test_the_0708_step_was_capped_by_live_and_extrap(energy, snapshots):
    """CAPPED, 07-08: max(0, min(10, max(Live, Extrap Prop, Extrap Blend) - Soft))."""
    day = DAYS[-1]
    moments, _ = score_reserve(
        DayActuals(energy[day], TZ),
        vr.from_archive(snapshots[day]),
        _earlier_workdays(day, energy),
        _CAPPED,
    )
    hour7 = next(m for m in moments if m.label == "07:41")
    soft = hour7.variant("live_soft")
    caps = [
        v.reserve
        for key in (
            "live",
            "extrapolated_live_proportional",
            "extrapolated_live_blend",
        )
        if (v := hour7.variant(key)) is not None
    ]
    assert hour7.production is not None
    assert soft is not None
    assert hour7.production.margin_pp == max(0, min(10, max(caps) - soft.reserve))


@pytest.mark.parametrize(
    ("label", "step", "buckets"), [("07:41", 10, 2), ("08:41", 5, 4)]
)
def test_both_steps_are_capped_by_live_and_a_settled_blend(
    energy, snapshots, label, step, buckets
):
    """BLEND_CAPS: max(0, min(step, max(Live, Blend) - Soft)), Blend from 2 buckets.

    On a replay every Extrap result carries its bucket count, so the gate is
    exercised the way production builds its caps: 12.10 at 07:41 the decision
    read the 07:41:01 snapshot (fetch 07:40:40), Blend on two buckets.
    """
    day = DAYS[0]
    scored = vr.replay(snapshots[day])
    moments, _ = score_reserve(
        DayActuals(energy[day], TZ),
        scored,
        _earlier_workdays(day, energy),
        CURRENT,
    )
    line = next(m for m in moments if m.label == label)
    snapshot = next(s for s in scored if s.taken_at == line.snapshot_at)
    blend = snapshot.result("extrapolated_live_blend")
    assert blend is not None
    assert blend.scored_buckets == buckets
    soft, live = line.variant("live_soft"), line.variant("live")
    blend_reading = line.variant("extrapolated_live_blend")
    assert soft is not None
    assert live is not None
    assert blend_reading is not None
    caps = [live.reserve] + ([blend_reading.reserve] if buckets >= 2 else [])
    assert line.production is not None
    assert line.production.margin_pp == max(0, min(step, max(caps) - soft.reserve))


def test_hour_net_export_counts_this_hour_only(energy):
    """Net export since the full hour, the slot holding `now` in proportion."""
    actuals = DayActuals(energy[DAYS[-1]], TZ)
    at = lambda h, m, sec=0: datetime.combine(DAYS[-1], time(h, m, sec), TZ)  # noqa: E731
    first, second = actuals.series_between("grid_net_export_kwh", at(10, 0), at(10, 10))
    assert actuals.hour_net_export(at(10, 7, 30)) == pytest.approx(
        max(0.0, first + second / 2)
    )
    assert actuals.hour_net_export(at(10, 0)) == 0.0


def test_extra_days_get_a_morning_reserve():
    assert has_morning_reserve(date(2026, 10, 17))  # Saturday, slots ran by exception
    assert not has_morning_reserve(date(2026, 10, 18))  # Sunday
    assert has_morning_reserve(date(2026, 10, 16))


def test_full_charge_against_the_same_computation_on_what_came(energy, prices):
    """Optimal = full_charge_minute on actual PV and consumption, same start and SoC."""
    day = DAYS[-1]
    actuals = DayActuals(energy[day], TZ)
    s0 = _s0(day, prices)
    plan = PlanReading(
        at=datetime(2026, 10, 14, 6, 59, tzinfo=TZ),
        full_at=14 * 60,
        hours=(7,),
        start_soc=30.0,
    )
    earlier = [
        energy[d].energy_by_half_hour(CONSUMPTION, TZ)
        for d in sorted(energy)
        if d < day and d.weekday() < 5
    ]
    model = percentile_by_key(earlier)
    score = score_full_charge(
        actuals, plan, prices[day.isoformat()], s0, 15 * 60, 8 * 60 + 15, model
    )
    profile = SurplusProfile.build(
        day, actuals.half_hours("pv_kwh"), actuals.half_hours(CONSUMPTION)
    )
    t2 = surplus_window_end(day) * 60
    assert score.optimal == full_charge_minute(profile, s0, 30.0, t2)
    assert score.error_actual_min == -60
    # The surplus error splits into the PV forecast and the consumption model.
    with_model = full_charge_minute(
        SurplusProfile.build(day, actuals.half_hours("pv_kwh"), model), s0, 30.0, t2
    )
    assert score.with_model == with_model
    if score.optimal is not None and with_model is not None:
        assert score.pv_part_min == 14 * 60 - with_model
        assert score.pv_part_min + score.consumption_part_min == score.error_min
    if score.optimal is not None:
        assert score.error_min == 14 * 60 - score.optimal
    soc = actuals.soc_near(actuals.at(8 * 60 + 15))
    assert score.actual_start_soc == soc
    assert score.model == full_charge_minute(profile, 8 * 60 + 15, soc, t2)
    roundtrip = type(score).from_dict(json.loads(json.dumps(score.to_dict())))
    assert roundtrip == score


def test_full_charge_on_a_day_off_searches_from_five(energy, prices):
    """A day off (b6343a5): the dearest hour is sought from 05:00, as production does."""
    day = DAYS[-1]
    actuals = DayActuals(energy[day], TZ)
    s0 = _s0(day, prices)
    day_prices = prices[day.isoformat()]
    plan = PlanReading(
        at=datetime(2026, 10, 14, 4, 59, tzinfo=TZ),
        full_at=14 * 60,
        hours=(5,),
        start_soc=10.0,
        day_off=True,
    )
    score = score_full_charge(actuals, plan, day_prices, s0, None, None)
    rce = RceDayPrices(None, day, tuple(day_prices))
    best = MorningDischargePlan.best_hour(rce, 5, min(10, s0 // 60), day_off=True)
    assert best is not None
    refill_end = min(surplus_window_end(day), 14)  # the hour after full_at 14:00
    assert score.hours_recomputed == MorningDischargePlan.hours_from(
        rce, best, s0 // 60, refill_end
    )
    assert score.start_soc == 10.0


class TestFullChargeMoments:
    """D3 at the moments before the charge start (18.10.2026)."""

    @staticmethod
    def _reading(h: int, m: int, full_at: int, soc: float = 30.0) -> PlanReading:
        return PlanReading(
            at=datetime.combine(DAYS[-1], time(h, m, 12), TZ),
            full_at=full_at,
            hours=(7, 8),
            start_soc=soc,
        )

    def _readings(self) -> list[PlanReading]:
        return [
            self._reading(4, 41, 13 * 60),
            self._reading(6, 11, 13 * 60 + 5, 32.0),
            self._reading(6, 55, 12 * 60 + 50),
            self._reading(8, 55, 12 * 60 + 43),
        ]

    def test_the_plan_in_force_at_each_moment_up_to_the_start(self, energy, prices):
        day = DAYS[-1]
        actuals = DayActuals(energy[day], TZ)
        s0 = _s0(day, prices)
        readings = self._readings()
        plan = decided_plan(readings, day)
        assert plan == readings[2]  # the last one before the first hour (07:00)
        score = score_full_charge(
            actuals, plan, prices[day.isoformat()], s0, None, 9 * 60, None, readings
        )
        rows = [(m.label, m.at, m.system, m.start_soc) for m in score.moments]
        assert rows == [
            ("06:11", 6 * 60 + 11, 13 * 60 + 5, 32.0),
            ("06:41", 6 * 60 + 41, 13 * 60 + 5, 32.0),
            ("07:41", 7 * 60 + 41, 12 * 60 + 50, 30.0),
            ("08:41", 8 * 60 + 41, 12 * 60 + 50, 30.0),
            ("start", 8 * 60 + 56, 12 * 60 + 43, 30.0),
        ]
        profile = SurplusProfile.build(
            day, actuals.half_hours("pv_kwh"), actuals.half_hours(CONSUMPTION)
        )
        t2 = surplus_window_end(day) * 60
        first = score.moments[0]
        assert first.optimal == full_charge_minute(profile, s0, 32.0, t2)
        if first.optimal is not None and first.with_model is not None:
            assert first.pv_part_min + first.consumption_part_min == first.error_min
        roundtrip = type(score).from_dict(json.loads(json.dumps(score.to_dict())))
        assert roundtrip == score

    def test_no_moments_after_the_start(self, energy, prices):
        day = DAYS[-1]
        actuals = DayActuals(energy[day], TZ)
        readings = self._readings()
        score = score_full_charge(
            actuals,
            readings[2],
            prices[day.isoformat()],
            _s0(day, prices),
            None,
            7 * 60,
            None,
            readings,
        )
        # The shift evaluates from 07:41 on: a 07:00 start has no "start" row.
        assert [m.label for m in score.moments] == ["06:11", "06:41"]

    def test_a_run_waits_for_every_moments_full_charge(self, energy, snapshots, prices):
        readings = self._readings()
        late = _day_score(
            DAYS[-1],
            energy,
            snapshots,
            prices,
            cutoff=datetime.combine(DAYS[-1], time(16, 6), TZ),
            plan=readings[2],
            readings=readings,
        )
        assert late.full_charge is not None
        assert late.full_charge.moments
        from custom_components.smart_rce.sensor.forecast_score_sensor import day_card

        card = day_card(late)
        assert card["full_charge_summary"]["moments"][0]["label"] == "06:11"


class TestDuringTheDay:
    """Runs before the day closes (18.10.2026): accuracy so far, pending parts."""

    @staticmethod
    def _at(h: int, m: int = 0) -> datetime:
        return datetime.combine(DAYS[-1], time(h, m), TZ)

    def test_a_run_at_the_charge_start_waits_for_d1_and_d2(
        self, energy, snapshots, prices
    ):
        score = _day_score(DAYS[-1], energy, snapshots, prices, cutoff=self._at(9))
        assert score.reserve == ()
        assert score.reserve_models == ()
        assert score.start == ()
        assert set(score.pending) >= {
            Pending.RESERVE,
            Pending.START,
            Pending.DAY_TYPE,
        }
        assert [m.label for m in score.so_far] == ["06:11", "06:41", "07:41", "08:41"]
        assert DayScore.from_dict(json.loads(json.dumps(score.to_dict()))) == score

    def test_d1_complete_once_the_window_closed_and_d2_at_four(
        self, energy, snapshots, prices
    ):
        at_1306 = _day_score(
            DAYS[-1], energy, snapshots, prices, cutoff=self._at(13, 6)
        )
        assert at_1306.reserve
        assert at_1306.start == ()
        assert at_1306.pending == (Pending.START,)
        at_1606 = _day_score(
            DAYS[-1], energy, snapshots, prices, cutoff=self._at(16, 6)
        )
        assert at_1606.start
        assert at_1606.pending == ()
        whole = _day_score(DAYS[-1], energy, snapshots, prices)
        assert at_1606.reserve == whole.reserve

    def test_d2_settles_once_the_rest_of_the_day_cannot_move_it(
        self, energy, snapshots, prices
    ):
        """12.10 run as if started at S0: full at 11:30, D2 settled by 12:00."""
        day = DAYS[0]
        actuals = DayActuals(energy[day], TZ)
        start = start_from_soc_rise(actuals)
        at_noon = datetime.combine(day, time(12, 0), TZ)
        early = _day_score(day, energy, snapshots, prices, cutoff=at_noon, s0=start)
        whole = _day_score(day, energy, snapshots, prices, s0=start)
        assert early.start
        assert Pending.START not in early.pending
        assert Pending.START_DIAGNOSTICS in early.pending
        assert _without_diagnostics(early.start) == _without_diagnostics(whole.start)
        # Started before S0 (11:00): whether 11:00 would still fill needs the
        # afternoon — the score waits.
        real = _day_score(day, energy, snapshots, prices, cutoff=at_noon)
        assert real.start == ()
        assert Pending.START in real.pending

    def test_so_far_is_what_was_promised_against_what_came(
        self, energy, snapshots, prices
    ):
        day = DAYS[-1]
        score = _day_score(day, energy, snapshots, prices, cutoff=self._at(10))
        row = next(m for m in score.so_far if m.label == "06:11")
        # 14.10 ran the old max-of rule — no profile, so the flat stood in.
        assert (row.flat_model, row.model_day_count) == (True, 0)
        actuals = DayActuals(energy[day], TZ)
        assert (row.since, row.until) == (self._at(7), self._at(10))
        assert row.pv_actual_kwh == round(
            actuals.energy_between("pv_kwh", self._at(7), self._at(10)) or 0, 3
        )
        snapshot = vr.consumed(vr.from_archive(snapshots[day]), self._at(6, 11))
        assert snapshot is not None
        system = snapshot.result(row.system_variant)
        assert system is not None
        slots = forecast_half_hours(system, day)
        promised = sum(slots.get(slot, 0.0) for slot in range(14, 20))
        variant = row.variant(row.system_variant)
        assert variant is not None
        assert variant.pv_kwh == pytest.approx(promised, abs=1e-3)
        error = row.pv_error_kwh(row.system_variant)
        assert error is not None
        # + = promised more than came, in kWh and in percent of what came.
        assert error == round(variant.pv_kwh - row.pv_actual_kwh, 3)
        assert row.pv_error_pct(row.system_variant) == round(
            100 * error / row.pv_actual_kwh, 1
        )
        assert row.consumption_error_kwh == round(
            row.consumption_model_kwh - row.consumption_actual_kwh, 3
        )

    def test_a_day_off_learns_its_consumption_from_its_own_kind(
        self, energy, snapshots, prices, monkeypatch
    ):
        """No T1 reserve: the model reads the earlier days of the same type."""
        day = DAYS[-1]
        monkeypatch.setattr(day_score_module, "setup_on", lambda _d, _tz: CURRENT)
        actuals = DayActuals(energy[day], TZ)
        half = {
            d: energy[d].energy_by_half_hour(CONSUMPTION, TZ)
            for d in sorted(energy)
            if d < day
        }
        workdays = {d: h for d, h in half.items() if d.weekday() < 5}
        sundays = {d: h for d, h in half.items() if d.weekday() == 6}

        def so_far(with_reserve: bool) -> tuple:
            monkeypatch.setattr(
                day_score_module, "has_morning_reserve", lambda _d: with_reserve
            )
            return score_day(
                DayInputs(
                    actuals=actuals,
                    snapshots=vr.from_archive(snapshots[day]),
                    earlier_workdays=workdays,
                    earlier_same_type=sundays,
                    prices=None,
                    s0=None,
                    start=ChargeStartFact(None, "none"),
                    afternoon_charge=None,
                    cutoff=self._at(10),
                ),
                status=ScoreStatus.PRELIMINARY,
                computed_at=self._at(10),
                code_version="test",
            ).so_far

        workday, day_off = so_far(True)[0], so_far(False)[0]
        assert (workday.model_days, workday.model_day_count) == (
            "workday",
            len(workdays),
        )
        # 14.10 is a Tuesday: with no reserve it reads its own kind's days.
        assert (day_off.model_day_count, day_off.flat_model) == (len(sundays), False)
        assert day_off.consumption_model_kwh != workday.consumption_model_kwh

    def test_a_moment_inside_the_window_counts_from_itself(
        self, energy, snapshots, prices
    ):
        score = _day_score(DAYS[-1], energy, snapshots, prices, cutoff=self._at(10))
        row = next(m for m in score.so_far if m.label == "08:41")
        assert row.since == self._at(8, 41)

    def test_a_closed_day_covers_the_whole_window(self, energy, snapshots, prices):
        score = _day_score(DAYS[-1], energy, snapshots, prices)
        assert all(m.until == self._at(13) for m in score.so_far)

    def test_full_charge_waits_until_every_full_charge_is_measured(
        self, energy, snapshots, prices
    ):
        plan = PlanReading(
            at=self._at(6, 59), full_at=14 * 60, hours=(7,), start_soc=30.0
        )
        early = _day_score(
            DAYS[-1], energy, snapshots, prices, cutoff=self._at(10), plan=plan
        )
        assert early.full_charge is None
        assert Pending.FULL_CHARGE in early.pending
        late = _day_score(
            DAYS[-1], energy, snapshots, prices, cutoff=self._at(16, 6), plan=plan
        )
        assert late.full_charge is not None


def test_accuracy_over_the_charging_day_runs_to_four(energy, snapshots, prices):
    """20.10.2026: the second table, from each moment to 16:00."""
    day = DAYS[-1]
    plan = PlanReading(
        at=datetime.combine(day, time(6, 59), TZ),
        full_at=14 * 60,
        hours=(7, 8),
        start_soc=30.0,
    )
    score = _day_score(day, energy, snapshots, prices, plan=plan)
    four = datetime.combine(day, time(16), TZ)
    assert score.so_far_day
    assert all(m.until == four for m in score.so_far_day)
    assert [m.label for m in score.so_far_day] == [m.label for m in score.so_far]
    longer = {m.label: m for m in score.so_far_day}
    for short in score.so_far:
        assert longer[short.label].pv_actual_kwh >= short.pv_actual_kwh
    day_prices = prices[day.isoformat()]
    assert score.discharge_peak_hour == max((7, 8), key=lambda h: day_prices[h])
    from custom_components.smart_rce.sensor.forecast_score_sensor import day_card

    card = day_card(score)
    expected = "06:41" if score.discharge_peak_hour == 7 else "07:41"
    assert card["sort_moment"] == expected
    assert len(card["so_far_day_summary"]) == len(score.so_far_day)
    assert DayScore.from_dict(json.loads(json.dumps(score.to_dict()))) == score


def test_energy_between_takes_edge_slots_in_proportion(energy):
    actuals = DayActuals(energy[DAYS[-1]], TZ)
    at = lambda h, m, sec=0: datetime.combine(DAYS[-1], time(h, m, sec), TZ)  # noqa: E731
    first, second = actuals.series_between("pv_kwh", at(10, 0), at(10, 10))
    assert actuals.energy_between("pv_kwh", at(10, 2, 30), at(10, 10)) == (
        pytest.approx(first / 2 + second)
    )
    assert actuals.energy_between("pv_kwh", at(23, 59), at(23, 59)) is None


def test_day_card_carries_accuracy_so_far_and_pending(energy, snapshots, prices):
    from custom_components.smart_rce.sensor.forecast_score_sensor import day_card

    cutoff = datetime.combine(DAYS[-1], time(10, 0), TZ)
    card = day_card(_day_score(DAYS[-1], energy, snapshots, prices, cutoff=cutoff))
    json.dumps(card)  # attributes must serialise
    row = card["so_far_summary"][0]
    assert row["label"] == "06:11"
    assert row["pv_error_pct"] is not None
    assert row["model_days"] == "workday"
    assert "live_soft" in row["variants"]
    assert set(card["pending"]) >= {"reserve", "start"}
    assert card["cutoff_hm"] is not None
