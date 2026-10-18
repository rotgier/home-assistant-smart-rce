"""PV charge — when the battery took the sun today, and when it will.

The charge start opens the battery to the PV surplus (the time gate of
`BatteryChargePolicy`; before it the surplus is exported). From there the
battery fills fast up to the BMS plateau (90%) and slowly over the top.
The price chart shows both stretches twice: what happened (`PvChargeRun`,
persisted, so a restart mid-day keeps it) and what the forecast expects
(`charge_milestones` on Live Soft) — a read model, nothing steers on it.

Times are minutes of the day, as in `charge_start_shift`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum
from typing import Any

from .charge_curve import FULL_SOC
from .charge_start_shift import PLATEAU_SOC, ChargeMilestones, to_time


class PvChargeRun:
    """Today's PV charge as it happened: opened, reached the plateau, got full.

    Plain class (entity with a lifecycle), observed once a minute until the
    end of the PV window (T2): the first minute the time gate is open marks
    the start, the first reading at or above a level marks reaching it.
    """

    def __init__(
        self,
        *,
        day: date,
        start: int | None = None,
        plateau_at: int | None = None,
        full_at: int | None = None,
        end_soc: float | None = None,
    ) -> None:
        self.day = day
        self.start = start
        self.plateau_at = plateau_at
        self.full_at = full_at
        # SoC at the end of the PV window — the level an unfinished charge stopped at.
        self.end_soc = end_soc

    def observe(
        self,
        minute: int,
        soc: float,
        *,
        opened: bool,
        start_in_force: int | None,
        window_end: int,
    ) -> bool:
        """Take one reading; return True when anything was recorded.

        `opened`: the time gate lets the battery take PV now (a charge slot
        from the grid does not count). A start found already open (HA was down
        when it struck) is dated back to the start in force.
        """
        if minute >= window_end:
            return self._close(soc)
        if self.start is None:
            if not opened:
                return False
            self.start = (
                start_in_force
                if start_in_force is not None and start_in_force <= minute
                else minute
            )
            self._reach(self.start, soc)
            return True
        return self._reach(minute, soc)

    def _close(self, soc: float) -> bool:
        if self.start is None or self.end_soc is not None:
            return False
        self.end_soc = soc
        return True

    def _reach(self, minute: int, soc: float) -> bool:
        changed = False
        if self.plateau_at is None and soc >= PLATEAU_SOC:
            self.plateau_at = minute
            changed = True
        if self.full_at is None and soc >= FULL_SOC:
            self.full_at = minute
            changed = True
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "start": self.start,
            "plateau_at": self.plateau_at,
            "full_at": self.full_at,
            "end_soc": self.end_soc,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PvChargeRun | None:
        """Restore from persisted dict; None on missing / unparsable input."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                day=date.fromisoformat(data["day"]),
                start=_optional_int(data.get("start")),
                plateau_at=_optional_int(data.get("plateau_at")),
                full_at=_optional_int(data.get("full_at")),
                end_soc=(
                    float(data["end_soc"]) if data.get("end_soc") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class PvChargeOutlook:
    """The plan day's PV charge for the chart: bands done and bands expected.

    `plateau_at` / `full_at`: when the battery got to 90% / full, or is
    forecast to (`*_forecast` says which); None when neither — the forecast
    stops short of it before T2.
    """

    day: date
    start: int | None
    bands: tuple[PvChargeBand, ...]
    plateau_at: int | None
    plateau_forecast: bool
    full_at: int | None
    full_forecast: bool

    @classmethod
    def build(
        cls,
        day: date,
        *,
        run: PvChargeRun | None,
        forecast: PvChargeForecast | None,
        now: int,
        soc: float | None,
        window_end: int,
    ) -> PvChargeOutlook:
        """Compose the bands: the run up to now, the forecast from where it stands.

        `run` is today's (None for tomorrow); `now` caps a band still under
        way, `soc` labels it. The forecast fills only what the run has not
        reached yet.
        """
        bands = [
            *(cls._run_bands(run, now, soc, window_end) if run is not None else ()),
            *(
                cls._forecast_bands(forecast, run, window_end)
                if forecast is not None
                else ()
            ),
        ]
        milestones = forecast.milestones if forecast is not None else None
        plateau_at, plateau_forecast = cls._reached(
            run.plateau_at if run is not None else None,
            milestones.plateau_at if milestones is not None else None,
        )
        full_at, full_forecast = cls._reached(
            run.full_at if run is not None else None,
            milestones.full_at if milestones is not None else None,
        )
        start = run.start if run is not None and run.start is not None else None
        if start is None and forecast is not None:
            start = forecast.start
        return cls(
            day=day,
            start=start,
            bands=tuple(b for b in bands if b.end > b.start),
            plateau_at=plateau_at,
            plateau_forecast=plateau_forecast,
            full_at=full_at,
            full_forecast=full_forecast,
        )

    @staticmethod
    def _run_bands(
        run: PvChargeRun, now: int, soc: float | None, window_end: int
    ) -> list[PvChargeBand]:
        if run.start is None:
            return []
        cut = min(now, window_end)
        ongoing = now < window_end
        level = run.end_soc if run.end_soc is not None else soc
        bands = [
            PvChargeBand.happened(
                BandKind.TO_PLATEAU,
                run.start,
                reached_at=run.plateau_at,
                level=PLATEAU_SOC,
                cut=cut,
                ongoing=ongoing,
                soc=level,
            )
        ]
        if run.plateau_at is not None:
            bands.append(
                PvChargeBand.happened(
                    BandKind.TO_FULL,
                    run.plateau_at,
                    reached_at=run.full_at,
                    level=FULL_SOC,
                    cut=cut,
                    ongoing=ongoing,
                    soc=level,
                )
            )
        return bands

    @staticmethod
    def _forecast_bands(
        forecast: PvChargeForecast, run: PvChargeRun | None, window_end: int
    ) -> list[PvChargeBand]:
        milestones = forecast.milestones
        bands: list[PvChargeBand] = []
        if run is None or run.plateau_at is None:
            bands.append(
                PvChargeBand.expected(
                    BandKind.TO_PLATEAU,
                    forecast.start,
                    reached_at=milestones.plateau_at,
                    level=PLATEAU_SOC,
                    window_end=window_end,
                    end_soc=milestones.end_soc,
                    starts_now=forecast.from_now,
                )
            )
        if (run is None or run.full_at is None) and milestones.plateau_at is not None:
            bands.append(
                PvChargeBand.expected(
                    BandKind.TO_FULL,
                    milestones.plateau_at,
                    reached_at=milestones.full_at,
                    level=FULL_SOC,
                    window_end=window_end,
                    end_soc=milestones.end_soc,
                    starts_now=(
                        forecast.from_now and milestones.plateau_at == forecast.start
                    ),
                )
            )
        return bands

    @staticmethod
    def _reached(happened: int | None, forecast: int | None) -> tuple[int | None, bool]:
        """Return the minute a level was reached, else its forecast (and which)."""
        if happened is not None:
            return happened, False
        if forecast is not None:
            return forecast, True
        return None, False

    def to_dict(self, tz: tzinfo | None) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "start": self._stamp(self.start, tz),
            "plateau_at": self._stamp(self.plateau_at, tz),
            "plateau_forecast": self.plateau_forecast,
            "full_at": self._stamp(self.full_at, tz),
            "full_forecast": self.full_forecast,
            "bands": [b.to_dict(self.day, tz) for b in self.bands],
        }

    def _stamp(self, minute: int | None, tz: tzinfo | None) -> str | None:
        return _stamp(self.day, minute, tz) if minute is not None else None


@dataclass(frozen=True)
class PvChargeForecast:
    """Forecast charge from `start` at `start_soc` — `charge_milestones` of it.

    `from_now`: the charge is under way and the forecast walks from the current
    minute (the chart takes that edge from the clock).
    """

    start: int
    start_soc: float
    milestones: ChargeMilestones
    from_now: bool = False


@dataclass(frozen=True)
class PvChargeBand:
    """One stretch of the charge on the chart.

    `done` — it happened (solid band); otherwise forecast (dashed frame).
    `reached` — it got to its level (90 / 100); otherwise it ends at the end
    of the PV window, or at now while still under way (`ongoing`), and `soc`
    is the level it stands at there.
    Edges at "now" (`ongoing` end, `starts_now` start) go out as None: the
    chart takes them from the clock, so the attributes don't change every
    minute.
    """

    kind: BandKind
    start: int
    end: int
    done: bool
    reached: bool
    ongoing: bool
    soc: float | None
    starts_now: bool = False

    @classmethod
    def happened(
        cls,
        kind: BandKind,
        start: int,
        *,
        reached_at: int | None,
        level: float,
        cut: int,
        ongoing: bool,
        soc: float | None,
    ) -> PvChargeBand:
        """Build a stretch that happened; an unfinished one ends at `cut` (now or T2)."""
        if reached_at is not None:
            return cls(kind, start, reached_at, True, True, False, level)
        return cls(kind, start, cut, True, False, ongoing, soc)

    @classmethod
    def expected(
        cls,
        kind: BandKind,
        start: int,
        *,
        reached_at: int | None,
        level: float,
        window_end: int,
        end_soc: float,
        starts_now: bool = False,
    ) -> PvChargeBand:
        """Build a forecast stretch; one short of its level ends at the window end."""
        if reached_at is not None:
            return cls(kind, start, reached_at, False, True, False, level, starts_now)
        return cls(
            kind, start, window_end, False, False, False, round(end_soc, 1), starts_now
        )

    def to_dict(self, day: date, tz: tzinfo | None) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "start": None if self.starts_now else _stamp(day, self.start, tz),
            "end": None if self.ongoing else _stamp(day, self.end, tz),
            "done": self.done,
            "reached": self.reached,
            "ongoing": self.ongoing,
            "soc": None if self.ongoing else self.soc,
        }


class BandKind(StrEnum):
    """Fast charge up to the plateau, slow charge over the top."""

    TO_PLATEAU = "to_plateau"
    TO_FULL = "to_full"


def _stamp(day: date, minute: int, tz: tzinfo | None) -> str:
    # To the second: the dashboard parses these in the browser (WKWebView).
    return datetime.combine(day, to_time(minute), tzinfo=tz).isoformat(
        timespec="seconds"
    )


def _optional_int(raw: Any) -> int | None:
    return None if raw is None else int(raw)
