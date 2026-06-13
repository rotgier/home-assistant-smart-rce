"""Unit tests for RainReader.is_raining_now (legacy mute-condition parity)."""

from unittest.mock import MagicMock

from custom_components.smart_rce.garden.infrastructure.rain_reader import RainReader


def _hass(states: dict[str, str | None]) -> MagicMock:
    hass = MagicMock()

    def _get(entity_id: str) -> MagicMock | None:
        value = states.get(entity_id)
        return MagicMock(state=value) if value is not None else None

    hass.states.get.side_effect = _get
    return hass


def _reader(weather: str | None, precip: str | None) -> RainReader:
    return _reader_from({RainReader._WEATHER: weather, RainReader._PRECIP: precip})


def _reader_from(states: dict[str, str | None]) -> RainReader:
    return RainReader(_hass(states))


def test_rainy_and_high_precip_is_wet() -> None:
    assert _reader("rainy", "80").is_raining_now() is True


def test_rainy_but_low_precip_not_wet() -> None:
    assert _reader("rainy", "50").is_raining_now() is False  # ≤ 70


def test_sunny_high_precip_not_wet() -> None:
    assert _reader("sunny", "80").is_raining_now() is False  # no wet token


def test_pouring_is_wet() -> None:
    assert _reader("pouring", "75").is_raining_now() is True


def test_lightning_is_wet() -> None:
    assert _reader("lightning-rainy", "90").is_raining_now() is True


def test_missing_weather_not_wet() -> None:
    assert _reader(None, "90").is_raining_now() is False


def test_unparsable_precip_treated_as_zero() -> None:
    assert _reader("rainy", "unknown").is_raining_now() is False
