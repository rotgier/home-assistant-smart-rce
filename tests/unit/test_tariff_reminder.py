"""December reminder that next year's tariff is not in the table yet.

Nothing else in the system notices a stale tariff: prices keep flowing and
slots keep being planned, only the break-even behind those decisions is wrong.
The reminder fills that gap, and silences itself when the row is added —
so the act of fixing the problem is what stops the nagging.
"""

from datetime import date

from custom_components.smart_rce.infrastructure.tariff_reminder import TariffReminder


def _season(day: date) -> bool:
    return TariffReminder._is_nagging_season(day)


def test_silent_before_the_ure_decision_is_normally_out():
    # URE publishes around 15-17 December; nagging earlier would be noise.
    assert not _season(date(2026, 12, 16))
    assert not _season(date(2026, 11, 30))


def test_nags_from_the_seventeenth_of_december():
    assert _season(date(2026, 12, 17))
    assert _season(date(2026, 12, 31))


def test_silent_for_the_rest_of_the_year():
    for month in range(1, 12):
        assert not _season(date(2026, month, 20)), month


def test_escalates_only_in_the_last_week():
    # Text first, a phone call once 1 January is close.
    assert date(2026, 12, 25).day < TariffReminder._nag.__globals__["_ESCALATE_DAY"]
    assert date(2026, 12, 26).day >= TariffReminder._nag.__globals__["_ESCALATE_DAY"]
