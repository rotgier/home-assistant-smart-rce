"""Constants for Smart RCE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "smart_rce"

# VAT 23% — RCE spot price netto × GROSS_MULTIPLIER = brutto.
# Opłaty dystrybucyjne (G12w ~30 gr/kWh) są stałe niezależne od RCE,
# pomijamy w threshold check (porównujemy sam RCE × VAT vs threshold).
GROSS_MULTIPLIER: Final[float] = 1.23

# Calendar entity tracking Polish workdays/holidays (provided by HA's
# `workday` integration). Source of truth for `walk_back_workdays` —
# prev-workday consumption profiles + target SOC matrix walk back N
# entries from this set, not from a "skip weekends" heuristic.
WORKDAY_CALENDAR_ENTITY: Final = "calendar.workday_calendar"
