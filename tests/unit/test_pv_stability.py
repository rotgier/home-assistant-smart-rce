"""Tests for `PvStability` — derivative stability run-length entity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smart_rce.domain.pv_stability import (
    PV_STABILITY_THRESHOLD_W_PER_MIN,
    PvStability,
)

_TZ = timezone.utc


def _at(min_offset: int) -> datetime:
    return datetime(2026, 5, 15, 9, 0, tzinfo=_TZ) + timedelta(minutes=min_offset)


def test_initial_state_is_unstable() -> None:
    stability = PvStability()
    assert stability.run_start is None
    assert not stability.is_stable()
    assert stability.run_length_sec_at(_at(0)) == 0.0


def test_stable_reading_starts_run() -> None:
    stability = PvStability()
    stability.update(_at(0), derivative_w_per_min=30.0, stability_value=5.0)
    assert stability.is_stable()
    assert stability.run_start == _at(0)


def test_consecutive_stable_readings_extend_run() -> None:
    stability = PvStability()
    stability.update(_at(0), 30.0, 5.0)
    stability.update(_at(2), 32.0, 4.0)
    stability.update(_at(5), 28.0, 6.0)
    assert stability.run_start == _at(0)  # start unchanged
    assert stability.run_length_sec_at(_at(5)) == 5 * 60


def test_unstable_reading_resets_run() -> None:
    stability = PvStability()
    stability.update(_at(0), 30.0, 5.0)
    stability.update(_at(3), 60.0, 50.0)  # stability above threshold
    assert not stability.is_stable()
    assert stability.run_start is None


def test_threshold_boundary_at_exact_value_is_unstable() -> None:
    """Threshold uses strict `<`, so exactly == threshold counts as unstable."""
    stability = PvStability()
    stability.update(_at(0), 30.0, PV_STABILITY_THRESHOLD_W_PER_MIN)
    assert not stability.is_stable()


def test_threshold_boundary_just_below_is_stable() -> None:
    stability = PvStability()
    stability.update(_at(0), 30.0, PV_STABILITY_THRESHOLD_W_PER_MIN - 0.01)
    assert stability.is_stable()


def test_none_stability_value_counts_as_unstable() -> None:
    """Missing sensor reading (None) → don't trust as stable."""
    stability = PvStability()
    stability.update(_at(0), 30.0, None)
    assert not stability.is_stable()


def test_unstable_after_stable_clears_run_only_on_state_flip() -> None:
    stability = PvStability()
    stability.update(_at(0), 30.0, 5.0)
    stability.update(_at(1), 31.0, 4.0)
    stability.update(_at(2), 60.0, 50.0)  # flip to unstable
    assert stability.run_start is None
    stability.update(_at(3), 40.0, 30.0)  # still unstable
    assert stability.run_start is None
    stability.update(_at(4), 30.0, 8.0)  # back to stable — new run_start
    assert stability.run_start == _at(4)


def test_transient_fields_updated_every_call() -> None:
    stability = PvStability()
    stability.update(_at(0), 25.5, 7.2)
    assert stability.last_derivative_w_per_min == 25.5
    assert stability.last_stability_value == 7.2
    assert stability.last_update == _at(0)
    stability.update(_at(1), -10.0, 100.0)
    assert stability.last_derivative_w_per_min == -10.0
    assert stability.last_stability_value == 100.0
    assert stability.last_update == _at(1)


def test_to_dict_persists_only_run_start_when_stable() -> None:
    stability = PvStability()
    stability.update(_at(0), 30.0, 5.0)
    snapshot = stability.to_dict()
    assert snapshot == {"run_start": _at(0).isoformat()}
    # Transient fields explicitly absent.
    assert "last_derivative_w_per_min" not in snapshot
    assert "last_stability_value" not in snapshot
    assert "last_update" not in snapshot


def test_to_dict_persists_none_when_unstable() -> None:
    stability = PvStability()
    snapshot = stability.to_dict()
    assert snapshot == {"run_start": None}


def test_from_dict_restores_run_start() -> None:
    iso = _at(0).isoformat()
    restored = PvStability.from_dict({"run_start": iso})
    assert restored.run_start == _at(0)
    # Transients stay None — they refresh on next live update.
    assert restored.last_derivative_w_per_min is None
    assert restored.last_stability_value is None
    assert restored.last_update is None


def test_from_dict_none_run_start() -> None:
    restored = PvStability.from_dict({"run_start": None})
    assert restored.run_start is None


def test_from_dict_missing_key_defaults_to_none() -> None:
    restored = PvStability.from_dict({})
    assert restored.run_start is None


def test_roundtrip_to_from_dict_preserves_state() -> None:
    original = PvStability()
    original.update(_at(0), 30.0, 5.0)
    original.update(_at(3), 31.0, 6.0)
    snapshot = original.to_dict()
    restored = PvStability.from_dict(snapshot)
    assert restored.run_start == original.run_start


def test_run_length_after_restore_continues_from_persisted_start() -> None:
    """After HA restart, run_length is computed from restored run_start."""
    iso = _at(0).isoformat()
    restored = PvStability.from_dict({"run_start": iso})
    # 10 minutes later (after HA restart)
    assert restored.run_length_sec_at(_at(10)) == 10 * 60


def test_save_only_changes_on_transition_not_per_update() -> None:
    """to_dict() unchanged across consecutive stable readings — disk write avoided."""
    stability = PvStability()
    stability.update(_at(0), 30.0, 5.0)
    snap1 = stability.to_dict()
    stability.update(_at(1), 31.0, 6.0)  # still stable, different derivative
    snap2 = stability.to_dict()
    stability.update(_at(2), 29.0, 4.0)  # still stable, different derivative
    snap3 = stability.to_dict()
    assert snap1 == snap2 == snap3  # run_start unchanged → no disk write needed
