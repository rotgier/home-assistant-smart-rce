"""ForecastSnapshot — what every PV forecast variant saw and said at one moment.

The archive stores inputs, not only outputs: `context` is the exact
`ForecastContext` of the latest dispatch, so any variant — including one
written after the snapshot was taken — can be replayed on it. `variants`
holds what production computed from that context, which is both the
scoreboard input for variants that already ran and the parity reference
for the replay tool.

Records are self-describing (`schema_version`, `code_version`, `source`)
because they outlive the code that wrote them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Final

from .forecast_enum import PvForecast
from .strategy_base import ForecastContext, PvForecastResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .forecasts import PvForecasts

SCHEMA_VERSION: Final = 1


@dataclass(frozen=True)
class ForecastSnapshot:
    """One archived moment: forecast inputs plus every variant's outcome."""

    _KIND: ClassVar[str] = "forecast_snapshot"

    taken_at: datetime
    trigger: SnapshotTrigger
    # Value of Solcast's "last API update" sensor: freshness is a fact of the
    # record, never assumed from the time of day (fetches do fail — 12.10.2026).
    solcast_fetched_at: str | None
    code_version: str
    context: ForecastContext
    variants: dict[str, VariantOutcome]
    # "live" = captured in production; "recorder_backfill" = rebuilt offline,
    # approximate inputs, excluded from replay parity checks.
    source: str = "live"

    @classmethod
    def capture(
        cls,
        forecasts: PvForecasts,
        *,
        taken_at: datetime,
        trigger: SnapshotTrigger,
        solcast_fetched_at: str | None,
        code_version: str,
    ) -> ForecastSnapshot | None:
        """Snapshot the latest dispatch; None before the first one."""
        context = forecasts.last_context
        if context is None:
            return None
        return cls(
            taken_at=taken_at,
            trigger=trigger,
            solcast_fetched_at=solcast_fetched_at,
            code_version=code_version,
            context=context,
            variants={
                variant.key: VariantOutcome(
                    result=variant.result,
                    remaining_kwh=variant.strategy.remaining_kwh,
                )
                for variant in PvForecast
            },
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ForecastSnapshot:
        return cls(
            taken_at=datetime.fromisoformat(data["taken_at"]),
            trigger=SnapshotTrigger(data["trigger"]),
            solcast_fetched_at=data.get("solcast_fetched_at"),
            code_version=str(data.get("code_version", "unknown")),
            context=ForecastContext.from_dict(data["context"]),
            variants={
                key: VariantOutcome.from_dict(outcome)
                for key, outcome in data.get("variants", {}).items()
            },
            source=str(data.get("source", "live")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self._KIND,
            "source": self.source,
            "taken_at": self.taken_at.isoformat(),
            "trigger": str(self.trigger),
            "solcast_fetched_at": self.solcast_fetched_at,
            "code_version": self.code_version,
            "context": self.context.to_dict(),
            "variants": {
                key: outcome.to_dict() for key, outcome in self.variants.items()
            },
        }


class SnapshotTrigger(StrEnum):
    """Why a snapshot was taken."""

    SOLCAST = "solcast"  # right after a successful Solcast API fetch
    GRID = "grid"  # half-hourly fallback when no fetch-driven snapshot came
    MORNING_CHARGE = "decision:morning_charge"  # morning charge window opens


@dataclass(frozen=True)
class VariantOutcome:
    """What one variant computed from the snapshot's context."""

    result: PvForecastResult | None
    remaining_kwh: float | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariantOutcome:
        raw = data.get("result")
        return cls(
            result=PvForecastResult.from_dict(raw) if raw is not None else None,
            remaining_kwh=data.get("remaining_kwh"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict() if self.result is not None else None,
            "remaining_kwh": self.remaining_kwh,
        }
