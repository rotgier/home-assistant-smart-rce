"""Weather Forecast History — tracks hourly forecast conditions throughout the day."""

from __future__ import annotations

from datetime import date, datetime
import logging

from .domain.pv_forecast import WeatherConditionAtHour

_LOGGER = logging.getLogger(__name__)


class WeatherForecastHistory:
    """Tracks hourly weather forecast conditions throughout the day.

    Updated every ~5 min from wetteronline hourly forecast.
    Hours that have already passed are kept (not overwritten).
    Resets at midnight.
    """

    def __init__(self) -> None:
        self._hours: dict[int, str] = {}  # hour -> condition_custom
        self._today: date | None = None

    def update_from_forecast(
        self, forecast_hourly: list[dict] | None, today: date, now: datetime
    ) -> str | None:
        """Update from wetteronline hourly forecast.

        Overwrites hours present in forecast (future hours).
        Hours not in forecast (already passed) are kept unchanged.

        Returns diff text if forecast changed for future hours, None otherwise.
        """
        if not forecast_hourly:
            return None

        if self._today != today:
            _LOGGER.debug("New day %s, resetting weather history", today)
            self._hours = {}
            self._today = today

        # Collect new conditions from forecast
        new_conditions: dict[int, str] = {}
        for item in forecast_hourly:
            dt_str = item.get("datetime", "")
            if not dt_str:
                continue
            dt = datetime.fromisoformat(dt_str)
            if dt.date() == today:
                new_conditions[dt.hour] = item.get("condition_custom", "cloudy")

        # Diff: compare future hours with current self._hours
        current_hour = now.hour
        diffs: dict[int, str] = {}  # hour -> old_condition
        for h, new_cond in new_conditions.items():
            if h > current_hour:
                old_cond = self._hours.get(h)
                if old_cond and old_cond != new_cond:
                    diffs[h] = old_cond

        diff_text = (
            self._format_diff(now, current_hour, new_conditions, diffs)
            if diffs
            else None
        )

        # Update hours
        for h, cond in new_conditions.items():
            self._hours[h] = cond

        return diff_text

    def _format_diff(
        self,
        now: datetime,
        current_hour: int,
        new_conditions: dict[int, str],
        diffs: dict[int, str],
    ) -> str:
        """Format lightweight diff of condition_custom changes."""
        lines = [now.strftime("%Y-%m-%d %H:%M"), ""]
        for h in range(24):
            cond = new_conditions.get(h, self._hours.get(h, "—"))
            marker = "  <--" if h == current_hour else ""
            was = f"  (was: {diffs[h]})" if h in diffs else ""
            lines.append(f"{h:2d}:00  {cond}{marker}{was}")
        return "\n".join(lines) + "\n"

    def restore(self, hours_attr: dict[str, str], today: date) -> None:
        """Restore from RestoreSensor after restart."""
        if not hours_attr:
            return
        self._today = today
        self._hours = {int(k): v for k, v in hours_attr.items()}
        _LOGGER.debug("Restored weather history: %d hours", len(self._hours))

    def get_condition(self, hour: int) -> str:
        """Get condition for given hour. Fallback to cloudy."""
        return self._hours.get(hour, "cloudy")

    def get_conditions_for_date(
        self, target_date: date
    ) -> list[WeatherConditionAtHour]:
        """Return conditions as domain objects for matching."""
        if target_date != self._today:
            return []
        return [
            WeatherConditionAtHour(
                hour=h, condition_custom=c, forecast_date=target_date
            )
            for h, c in self._hours.items()
        ]

    @property
    def hours_attribute(self) -> dict[str, str]:
        """For sensor extra_state_attributes."""
        return {str(h): c for h, c in sorted(self._hours.items())}
