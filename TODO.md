# TODO / tech-debt

## refactor: STATE_UNKNOWN/STATE_UNAVAILABLE zamiast hardcoded stringów

Priorytet: niski (czytelność/spójność, bez zmiany zachowania).

W wielu miejscach porównujemy stan encji do hardcoded `"unknown"`/`"unavailable"`
zamiast importować `STATE_UNKNOWN` / `STATE_UNAVAILABLE` z `homeassistant.const`.
Wzorzec do naśladowania jest już w `infrastructure/weather_listener.py`
(`UNAVAILABLE_STATES = (STATE_UNKNOWN, STATE_UNAVAILABLE, "", None)`).

Miejsca (grep 2026-06-10):
- `infrastructure/state_mapper.py:57,68,137,144`
- `infrastructure/pv_forecast/live_rate_reader.py:89,106,115`
- `sensor/weather_table_sensor.py:228,236,251`
- `infrastructure/goodwe_ems_actuator.py:152,159-160`
- `infrastructure/dod_policy_actuator.py:124`
- `infrastructure/weather_history_loader.py:192`
- `domain/weather_table.py:438`

Pułapka: `state_mapper.py:57` używa `match/case "unavailable" | "unknown"` —
`STATE_*` jako `case` byłby **capture pattern**, nie wartością. Trzeba
`case x if x in (STATE_UNKNOWN, STATE_UNAVAILABLE)` albo świadomie zostawić
literały. Część miejsc dorzuca `""` / `"None"` — rozważyć wspólną stałą/tuple
(np. re-export `UNAVAILABLE_STATES`). `domain/` (np. `dod_policy.py` enum
`UNKNOWN`) zostawić — to wartości domenowe, nie HA states.
