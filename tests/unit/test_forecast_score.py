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
from custom_components.smart_rce.domain.cheap_hours import is_dear_day
from custom_components.smart_rce.domain.consumption_percentile import percentile_by_key
from custom_components.smart_rce.domain.consumption_profiles import ConsumptionProfile
from custom_components.smart_rce.domain.energy_day import EnergyDay
from custom_components.smart_rce.domain.forecast_score import (
    day_score as day_score_module,
    start_score as start_score_module,
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
from custom_components.smart_rce.domain.forecast_score.moment_chart import (
    MOMENT_CHARTS_SCHEMA,
    moment_charts,
)
from custom_components.smart_rce.domain.forecast_score.moment_weather import (
    MomentForecast,
    WeatherInputs,
)
from custom_components.smart_rce.domain.forecast_score.production_outcome import (
    ChargeStartFact,
    production_outcome,
    start_from_soc_rise,
    start_in_force,
    struck_start,
)
from custom_components.smart_rce.domain.forecast_score.production_setup import (
    CURRENT,
    MarginRule,
    StartRule,
    setup_on,
    start_setup_on,
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

    @pytest.mark.parametrize(
        ("changes", "struck"),
        [
            # 28.10: 07:51 later to 10:00, 09:41 "now", 09:50 set by hand to
            # 10:41 then 10:00, a restart wrote 10:00 again at 09:52.
            (
                [
                    *[(0, 540), (413, 600), (429, 540), (451, 540), (471, 600)],
                    *[(495, 600), (581, 581), (590, 641), (590, 600), (592, 600)],
                ],
                600,
            ),
            # 29.10: 08:00 struck, 08:51 back to 10:00, 09:51 struck for good.
            ([(0, 600), (431, 600), (476, 480), (531, 600), (591, 591)], 591),
            # 30.10: 07:51 later to 12:00, 11:51 "now".
            ([(0, 660), (471, 720), (711, 711)], 711),
            # A restart writing the struck start again is no new strike.
            ([(0, 600), (630, 600)], 600),
            # A start set in the past while the battery is open changes nothing.
            ([(0, 600), (660, 540)], 600),
        ],
    )
    def test_struck_start_takes_the_last_strike(self, changes, struck):
        assert struck_start(changes) == struck

    def test_start_in_force_excludes_the_evaluations_own_move(self):
        changes = [(0, 540), (471, 600), (581, 581)]
        assert start_in_force(changes, 7 * 60 + 41, 540) == 540
        assert start_in_force(changes, 7 * 60 + 51, 540) == 540  # its own move
        assert start_in_force(changes, 7 * 60 + 56, 540) == 600
        assert start_in_force(None, 7 * 60 + 56, 540) == 540

    def test_moments_run_until_a_start_moved_past_s0(self, energy, snapshots, prices):
        """Production evaluates until the start, not S0 (28.10.2026: 09:00 → 10:00)."""
        day = DAYS[-1]
        args = (
            DayActuals(energy[day], TZ),
            vr.from_archive(snapshots[day]),
            prices[day.isoformat()],
            9 * 60,
            [],
        )
        up_to_s0 = score_start(*args, 9 * 60)
        assert [m.label for m in up_to_s0] == ["07:41", "08:41", "start"]
        past_s0 = score_start(*args, 10 * 60, changes=[(0, 540), (471, 600)])
        assert [m.label for m in past_s0] == [
            "07:41",
            "08:41",
            "09:16",
            "09:41",
            "start",
        ]
        assert next(m for m in past_s0 if m.label == "start").minute == 9 * 60 + 56

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
    def test_the_start_fact_keeps_its_history(self):
        fact = ChargeStartFact(
            591, "override", 600, False, False, ((0, 600), (591, 591))
        )
        assert ChargeStartFact.from_dict(fact.to_dict()) == fact
        older = {"start": 480, "source": "override", "s0": 600}
        assert ChargeStartFact.from_dict(older).changes is None

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
        assert setup_on(date(2026, 10, 18), TZ).margin is MarginRule.BLEND_CAPS
        assert setup_on(date(2026, 10, 29), TZ).dear_day_mode
        assert not setup_on(date(2026, 11, 2), TZ).dear_day_choice
        assert setup_on(date(2026, 11, 9), TZ) is CURRENT
        assert CURRENT.dear_day_choice
        assert CURRENT.margin is MarginRule.BLEND_CAPS

    def test_the_charge_start_runs_on_the_setup_of_its_first_evaluation(self):
        # 28.10 switched at 07:32: the reserve's 06:11 still the old setup, the
        # start (first evaluated at 07:41) already the cheaper later one.
        assert setup_on(date(2026, 10, 28), TZ).start_rule is StartRule.UP_TO_S0
        assert not setup_on(date(2026, 10, 28), TZ).dear_day_mode
        assert (
            start_setup_on(date(2026, 10, 28), TZ).start_rule is StartRule.CHEAPER_LATER
        )
        assert CURRENT.start_rule is StartRule.CHEAPER_LATER

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

    def test_dear_days_are_ranked_apart(self, energy, snapshots, prices):
        scores = [_day_score(d, energy, snapshots, prices) for d in DAYS]
        assert all(s.dear_day is False for s in scores)  # before the dear-day mode
        scores[0] = dataclasses.replace(scores[0], dear_day=True)
        ranking = rank(scores)
        assert ranking["days"] == [d.isoformat() for d in DAYS]
        assert ranking["dear_days"] == [DAYS[0].isoformat()]
        rows = {r["variant"]: r for r in ranking["reserve"]["all"]["06:11"]}
        assert rows["solcast_p50"]["n"] == 2
        dear = {r["variant"]: r for r in ranking["dear"]["reserve"]["all"]["06:11"]}
        assert dear["solcast_p50"]["n"] == 1

    def test_days_whose_start_the_day_off_rule_set_rank_d2_apart(
        self, energy, snapshots, prices
    ):
        scores = [_day_score(d, energy, snapshots, prices) for d in DAYS]
        kept = dataclasses.replace(scores[1], day_off_choice="s0")
        moved = dataclasses.replace(scores[0], day_off_choice="none")
        assert not kept.day_off_moved_start
        assert moved.day_off_moved_start
        ranking = rank([moved, kept, scores[2]])
        assert ranking["day_off_days"] == [DAYS[0].isoformat()]
        rows = {r["variant"]: r for r in ranking["start"]["all"]["start"]}
        apart = {r["variant"]: r for r in ranking["day_off"]["start"]["all"]["start"]}
        assert rows["solcast_p50"]["n"] == 2
        assert apart["solcast_p50"]["n"] == 1
        assert DayScore.from_dict(moved.to_dict()).day_off_choice == "none"

    def test_a_dear_day_is_a_workday_of_the_dear_day_setup(self, energy):
        actuals = DayActuals(energy[DAYS[-1]], TZ)
        inputs = DayInputs(
            actuals=actuals,
            snapshots=(),
            earlier_workdays={},
            earlier_same_type={},
            prices=[800.0] * 24,
            s0=None,
            start=ChargeStartFact(None, "none"),
            afternoon_charge=None,
            export_threshold=750.0,
        )
        dear_setup = dataclasses.replace(
            CURRENT, dear_day_mode=True, dear_day_choice=False
        )
        assert day_score_module._dear_day(inputs, dear_setup) is True
        old_setup = dataclasses.replace(CURRENT, dear_day_mode=False)
        assert day_score_module._dear_day(inputs, old_setup) is False
        # From 08.11.2026 the dear day picks a variant: only FULL runs as a dear day.
        choosing = dataclasses.replace(dear_setup, dear_day_choice=True)
        assert day_score_module._dear_day(inputs, choosing) is False  # no choice
        for chosen, dear in (("full", True), ("normal", False)):
            picked = dataclasses.replace(inputs, dear_day_choice=chosen)
            assert day_score_module._dear_day(picked, choosing) is dear
        saturday = dataclasses.replace(
            inputs, actuals=_ShiftedDay(actuals, date(2026, 10, 31))
        )
        assert day_score_module._dear_day(saturday, dear_setup) is False

    def test_a_dear_day_has_no_hour_from_7_to_t2_below_the_bar(self):
        day = date(2026, 10, 29)
        dear = RceDayPrices(None, day, tuple([800.0] * 24))
        assert is_dear_day(day, dear, 750.0)
        cheap = RceDayPrices(None, day, tuple([800.0] * 11 + [500.0] + [800.0] * 12))
        assert not is_dear_day(day, cheap, 750.0)


class TestMomentCharts:
    def test_each_moment_draws_the_snapshot_the_table_scores(self, energy, snapshots):
        day = DAYS[-1]
        actuals = DayActuals(energy[day], TZ)
        scored = vr.from_archive(snapshots[day])
        charts = moment_charts(actuals, scored, dict.fromkeys(range(48), 0.5))
        assert charts["schema"] == MOMENT_CHARTS_SCHEMA
        assert charts["unit"] == "kW"
        n = len(charts["times"])
        assert n == 34  # 04:00-21:00
        assert (
            charts["times"][0]
            == datetime(2026, 10, 14, 4, 0, tzinfo=TZ).timestamp() * 1000
        )
        assert charts["consumption_model"][0] == 1.0  # 0.5 kWh in half an hour = 1 kW
        at_0741 = charts["moments"]["07:41"]
        snapshot = vr.consumed(scored, datetime(2026, 10, 14, 7, 41, tzinfo=TZ))
        assert snapshot is not None
        assert at_0741["snapshot_at"] == snapshot.taken_at.isoformat()
        live = forecast_half_hours(snapshot.result("live"), day)
        slot_10 = 20 - 8  # 10:00 is slot 20, the chart starts at slot 8
        assert at_0741["variants"]["live"][slot_10] == pytest.approx(
            live[20] * 2, abs=1e-3
        )
        assert all(len(v) == n for v in at_0741["variants"].values())
        assert len(charts["pv"]) == n
        assert at_0741["weather"]
        assert all(
            level in {10, 20, 40, 60, 80, 100} for _, level in at_0741["weather"]
        )
        assert len(json.dumps(charts)) < 30_000


class TestMomentWeather:
    def test_observed_until_the_moment_the_forecast_after_it(self):
        """31.10.2026 at 09:41: the 09:40 forecast's quarters, then its hours."""
        day = date(2026, 10, 31)

        def at(h, m=0):
            return datetime(2026, 10, 31, h, m, tzinfo=TZ)

        forecast = MomentForecast.from_forecast(
            [
                {
                    "datetime": at(9).isoformat(),
                    "condition_custom": "rainy-light",
                    "nowcast_15min": [
                        {"date": at(9, 45).isoformat(), "condition_custom": "rainy"}
                    ],
                },
                {
                    "datetime": at(11).isoformat(),
                    "condition_custom": "rainy-light",
                    "nowcast_15min": [
                        {"date": at(11).isoformat(), "condition_custom": "rainy-light"},
                        {
                            "date": at(11, 15).isoformat(),
                            "condition_custom": "partlycloudy",
                        },
                    ],
                },
                {"datetime": at(12).isoformat(), "condition_custom": "cloudy"},
                {"datetime": "2026-11-01T12:00:00+01:00", "condition_custom": "sunny"},
            ],
            day,
        )
        weather = WeatherInputs(
            day=day,
            observed=(
                (at(9, 10), "cloudy"),
                (at(9, 35), "rainy-light"),
                (at(9, 50), "x"),
            ),
            forecasts={"09:41": forecast},
        )
        points = weather.points("09:41", at(9, 41))
        assert [(w.strftime("%H:%M"), c) for w, c in points] == [
            ("09:10", "cloudy"),
            ("09:35", "rainy-light"),  # observed; 09:50 is after the moment
            ("09:45", "rainy"),  # nowcast after the moment
            ("11:00", "rainy-light"),  # the 11:00 hour covered by its quarters
            ("11:15", "partlycloudy"),
            ("12:00", "cloudy"),  # the hour; tomorrow left out
        ]
        assert WeatherInputs.from_dict(weather.to_dict()) == weather
        assert weather.points("06:41", at(6, 41)) == []  # nothing observed yet


class _ShiftedDay:
    """DayActuals stand-in that reports another day (only `.day` is read)."""

    def __init__(self, actuals: DayActuals, day: date) -> None:
        self._actuals = actuals
        self.day = day


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


def test_ninety_nine_on_the_bms_counts_as_full(energy, prices):
    """22.10.2026: the BMS sat at 99% for hours before 100% (21.10: 13:08 → 16:36)."""
    from custom_components.smart_rce.domain.forecast_score.production_outcome import (
        FULL_READ_SOC,
        FULL_SOC,
    )

    assert FULL_READ_SOC == 99.0
    assert FULL_SOC == 98.5  # the mean of the slot it reaches 99 in
    day = DAYS[-1]
    actuals = DayActuals(energy[day], TZ)
    profile = SurplusProfile.build(
        day, actuals.half_hours("pv_kwh"), actuals.half_hours(CONSUMPTION)
    )
    t2 = surplus_window_end(day) * 60
    to_99 = full_charge_minute(profile, 9 * 60, 30.0, t2, 99.0)
    to_100 = full_charge_minute(profile, 9 * 60, 30.0, t2)
    if to_99 is not None and to_100 is not None:
        assert to_99 < to_100  # the last point alone takes 450 s
    plan = PlanReading(
        at=datetime.combine(day, time(6, 59), TZ),
        full_at=14 * 60,
        hours=(7,),
        start_soc=30.0,
    )
    score = score_full_charge(
        actuals, plan, prices[day.isoformat()], _s0(day, prices), None, 9 * 60
    )
    soc = actuals.soc_near(actuals.at(9 * 60))
    assert score.model == full_charge_minute(profile, 9 * 60, soc, t2, 99.0)


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
        self, energy, snapshots, prices, monkeypatch
    ):
        """12.10 run as if started at S0: full at 11:30, D2 settled by 12:00.

        With the rule up to S0. Today's rule weighs a later start on the
        surplus to its price window's end, so the ranking's calls wait for it.
        """
        day = DAYS[0]
        actuals = DayActuals(energy[day], TZ)
        start = start_from_soc_rise(actuals)
        at_noon = datetime.combine(day, time(12, 0), TZ)
        waiting = _day_score(day, energy, snapshots, prices, cutoff=at_noon, s0=start)
        assert waiting.start == ()
        assert Pending.START in waiting.pending
        monkeypatch.setattr(
            start_score_module,
            "CURRENT",
            dataclasses.replace(CURRENT, start_rule=StartRule.UP_TO_S0),
        )
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
    tables = {t["id"]: t for t in card["tables"]}
    assert tables["so_far"]["sort"] == {"column": expected, "descending": False}
    assert len(card["so_far_day_summary"]) == len(score.so_far_day)
    assert DayScore.from_dict(json.loads(json.dumps(score.to_dict()))) == score


class TestTables:
    """The card's tables: ready rows, the card only draws (20.10.2026)."""

    @staticmethod
    def _card(energy, snapshots, prices):
        from custom_components.smart_rce.sensor.forecast_score_sensor import day_card

        day = DAYS[-1]
        plan = PlanReading(
            at=datetime.combine(day, time(6, 59), TZ),
            full_at=14 * 60,
            hours=(7, 8),
            start_soc=30.0,
        )
        return day_card(_day_score(day, energy, snapshots, prices, plan=plan))

    def test_day_tables_follow_the_contract(self, energy, snapshots, prices):
        from custom_components.smart_rce.sensor.forecast_score_tables import (
            TABLES_SCHEMA,
            VARIANT_LABELS,
        )

        card = self._card(energy, snapshots, prices)
        json.dumps(card)  # attributes must serialise
        assert card["tables_schema"] == TABLES_SCHEMA
        tables = {t["id"]: t for t in card["tables"]}
        assert list(tables) == ["so_far", "so_far_day", "reserve", "start"]
        for table in tables.values():
            keys = {c["key"] for c in table["columns"]}
            assert table["group"] == "day_moments"
            for row in table["rows"]:
                assert set(row["cells"]) <= keys
        rows = tables["so_far"]["rows"]
        assert [r["key"] for r in rows] == list(VARIANT_LABELS)
        system = [r for r in rows if r["system"]]
        assert [r["label"] for r in system] == ["Soft 0,5 (system)"]  # 14.10
        cell = rows[0]["cells"]["06:11"]
        assert cell["sort"] == pytest.approx(abs(float(cell["text"])), abs=0.005)

    def test_reserve_cells_carry_the_dangerous_side(self, energy, snapshots, prices):
        card = self._card(energy, snapshots, prices)
        reserve = next(t for t in card["tables"] if t["id"] == "reserve")
        cells = [c for r in reserve["rows"] for c in r["cells"].values() if c]
        assert cells
        for c in cells:
            value = int(c["text"])
            expected = "bad" if value < -3 else "warn" if value > 3 else None
            assert c["severity"] == expected
            assert c["sort"] == abs(value)

    def test_accuracy_severity_needs_share_and_amount(self):
        from custom_components.smart_rce.sensor.forecast_score_tables import (
            _accuracy_severity,
        )

        assert _accuracy_severity(0.25, 0.5) is None  # 50% but a quarter kWh
        assert _accuracy_severity(0.4, 1.0) == "warn"
        assert _accuracy_severity(-1.2, 2.0) == "bad"
        assert _accuracy_severity(1.2, 10.0) is None  # 12%
        assert _accuracy_severity(0.5, 0.0) is None

    def test_ranking_tables_sort_by_mae(self, energy, snapshots, prices):
        from custom_components.smart_rce.sensor.forecast_score_tables import (
            ranking_tables,
        )

        scores = [_day_score(d, energy, snapshots, prices) for d in DAYS]
        tables = {t["id"]: t for t in ranking_tables(rank(scores))}
        assert list(tables) == [
            "ranking_reserve_06:11",
            "ranking_reserve_08:41",
            "ranking_start",
            "ranking_models",
            "ranking_start_models",
            "ranking_dear_start",
            "ranking_dear_reserve_06:11",
            "ranking_day_off_start",
        ]
        reserve = tables["ranking_reserve_06:11"]
        assert reserve["sort"] == {"column": "mae", "descending": False}
        assert reserve["group"] is None
        assert reserve["rows"]
        mae = reserve["rows"][0]["cells"]["mae"]
        assert mae["strong"] is True
        json.dumps(list(tables.values()))


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
