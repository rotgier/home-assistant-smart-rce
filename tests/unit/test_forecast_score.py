"""Forecast scoreboard — parity with the reference script, decisions, facts, ranking."""

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.charge_slots import ChargeSlots
from custom_components.smart_rce.domain.consumption_profiles import ConsumptionProfile
from custom_components.smart_rce.domain.energy_day import EnergyDay
from custom_components.smart_rce.domain.forecast_score import variant_results as vr
from custom_components.smart_rce.domain.forecast_score.consumption_models import (
    ConsumptionModel,
)
from custom_components.smart_rce.domain.forecast_score.day_actuals import DayActuals
from custom_components.smart_rce.domain.forecast_score.day_score import (
    DayInputs,
    DayScore,
    ScoreStatus,
    score_day,
)
from custom_components.smart_rce.domain.forecast_score.production_outcome import (
    ChargeStartFact,
    production_outcome,
    start_from_soc_rise,
    struck_start,
)
from custom_components.smart_rce.domain.forecast_score.production_setup import (
    CURRENT,
    setup_on,
)
from custom_components.smart_rce.domain.forecast_score.ranking import rank
from custom_components.smart_rce.domain.forecast_score.reserve_score import (
    score_reserve,
)
from custom_components.smart_rce.domain.forecast_score.start_score import (
    last_evaluation_before,
    score_start,
)
from custom_components.smart_rce.domain.pv_forecast import PvForecast
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (
    ForecastSnapshot,
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
        replayed = vr.replay(snapshots[day])
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
    def test_start_on_actual_consumption(
        self, day, energy, snapshots, expected, prices
    ):
        moments = score_start(
            DayActuals(energy[day], TZ),
            vr.replay(snapshots[day]),
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


def _day_score(day, energy, snapshots, prices) -> DayScore:
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
            s0=_s0(day, prices),
            start=ChargeStartFact(start_from_soc_rise(actuals), "soc_rise"),
            afternoon_charge=None,
        ),
        status=ScoreStatus.FINAL,
        computed_at=datetime(2026, 10, 15, 21, 6, tzinfo=TZ),
        code_version="test",
    )


class TestProductionSetup:
    def test_a_day_runs_on_the_setup_in_force_at_0611(self):
        assert setup_on(date(2026, 10, 13), TZ).pv_variant == "live"
        assert setup_on(date(2026, 10, 14), TZ).pv_variant == "live_soft_0.5"
        assert setup_on(date(2026, 10, 15), TZ).reserve_model is ConsumptionModel.P50
        assert setup_on(date(2026, 10, 16), TZ) is CURRENT

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
