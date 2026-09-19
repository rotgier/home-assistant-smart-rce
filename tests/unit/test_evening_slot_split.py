"""Evening discharge split into EARLY + LATE.

Nights where the RCE peak is followed by another expensive hour need two
discharge windows with a hold between them. With a single evening slot that
second window was improvised by repointing DISCHARGE_MORNING at 21:00-22:00,
which cost both clarity and several edits per change (the `start < end`
invariant rejects the intermediate states). These tests cover the second slot
and the migration of stores written before the split.
"""

from datetime import time

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    SlotKind,
)

LEGACY_EVENING_KEY = "DISCHARGE_EVENING"


def test_both_evening_slots_exist_and_discharge():
    assert SlotKind.DISCHARGE_EVENING_EARLY.direction.is_discharge
    assert SlotKind.DISCHARGE_EVENING_LATE.direction.is_discharge


def test_default_windows_do_not_overlap():
    early = SlotKind.DISCHARGE_EVENING_EARLY.profile.default_window
    late = SlotKind.DISCHARGE_EVENING_LATE.profile.default_window

    assert early[1] <= late[0], f"{early} overlaps {late}"


def test_the_later_window_outranks_the_earlier_one():
    prec = SlotKind.by_precedence()

    assert prec.index(SlotKind.DISCHARGE_EVENING_LATE) > prec.index(
        SlotKind.DISCHARGE_EVENING_EARLY
    )


def test_a_store_from_before_the_split_keeps_the_tuned_window_as_early():
    # The saved window is the one the user actually tuned, so it must land on
    # EARLY rather than silently reverting to defaults.
    restored = BatterySchedule.from_dict(
        {
            "today": {
                LEGACY_EVENING_KEY: BatteryScheduleEntry(
                    kind=SlotKind.DISCHARGE_EVENING_EARLY,
                    enabled=True,
                    start=time(19, 0),
                    end=time(21, 0),
                    target_soc=33.0,
                ).to_dict()
            }
        }
    )

    early = restored.today_entry_for(SlotKind.DISCHARGE_EVENING_EARLY)
    assert early.enabled
    assert (early.start, early.end) == (time(19, 0), time(21, 0))
    assert early.target_soc == 33.0


def test_the_new_late_slot_starts_from_defaults_after_migration():
    restored = BatterySchedule.from_dict(
        {"today": {LEGACY_EVENING_KEY: {"enabled": True, "start": "19:00:00"}}}
    )

    late = restored.today_entry_for(SlotKind.DISCHARGE_EVENING_LATE)
    assert not late.enabled
    assert (late.start, late.end) == (
        SlotKind.DISCHARGE_EVENING_LATE.profile.default_window
    )


def test_an_engagement_saved_under_the_old_name_is_remapped():
    restored = BatterySchedule.from_dict({"currently_engaging": LEGACY_EVENING_KEY})

    assert restored.currently_engaging is SlotKind.DISCHARGE_EVENING_EARLY


def test_a_store_written_after_the_split_round_trips():
    schedule = BatterySchedule.from_dict(
        {
            "today": {
                SlotKind.DISCHARGE_EVENING_LATE.name: BatteryScheduleEntry(
                    kind=SlotKind.DISCHARGE_EVENING_LATE,
                    enabled=True,
                    start=time(21, 0),
                    end=time(22, 0),
                ).to_dict()
            }
        }
    )

    late = BatterySchedule.from_dict(schedule.to_dict()).today_entry_for(
        SlotKind.DISCHARGE_EVENING_LATE
    )
    assert late.enabled
    assert (late.start, late.end) == (time(21, 0), time(22, 0))
