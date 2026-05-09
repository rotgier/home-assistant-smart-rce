# Solcast self-calibration via Pattern (extrapolated_live_pattern)

## TL;DR

`sensor.rce_weather_adjusted_pv_live_extrapolated_pattern` projektuje na future
buckets dnia "współczynnik realizacji" obserwowany w bucketach przeszłych +
bieżącym. Najwartościowszy w **wczesnym oknie po Solcast refresh, gdy zakres
`[pv_estimate10, pv_estimate]` jest jeszcze szeroki**. Optymalne okno obserwacji
do decyzji o rozładowaniu baterii: **5-10 min po refresh Solcast** (zwykle ranne
godziny — typowe ~07:40 / 08:40, sprawdzić w logach).

## Algorytm — krótki opis

Pliki:
- `domain/pv_forecast_extrapolation.py::extrapolate_calibrated_pattern`
- `infrastructure/pv_forecast/realized_pv_loader.py` (ładuje realized PV per
  bucket dla today z utility meter `sensor.total_pv_generation_bi_hourly`)

Kroki:

1. Dla każdego daylight bucketu (past + current) oblicz **score realizacji**
   na 4-zone normalized scale używając trzech kwantyli Solcast (p10, estimate=p50, p90):
   ```
   S < 0      : realized < p10        S = realized/p10 − 1     (range −1..0)
   S in 0..1  : p10 ≤ real ≤ est      S = (real−p10)/(est−p10)
   S in 1..2  : est < real ≤ p90      S = 1 + (real−est)/(p90−est)
   S > 2      : real > p90            S = 2 + (real−p90)/p90  (unbounded)
   ```
   - S = −1 → zero PV; S = 0 → p10; S = 1 → estimate; S = 2 → p90
   - Score continuous across zones; below-p10 i above-p90 używają **ratio**
     (nie linear extrapolation) — zapobiega clamping do 0 lub unbounded growth
   - Edge: `est − p10 < 0.05 kWh/h` (collapsed zone) → fallback do ratio
     przeciw wider quantile. p10 < 0.05 → ratio przeciw estimate.

2. **Weighted average score** — current bucket waga 1.0, każdy krok wstecz
   `× PATTERN_DECAY = 0.7` (po 3 bucketach ≈ 0.34). Buckets z `pv_estimate / 2 <
   PATTERN_MIN_FORECAST_KWH = 0.05` kWh/30min są pomijane (pre-dawn / post-dusk noise).

3. Dla każdego future bucketa projekcja przez **inverse score mapping**
   (`_project_rate_from_score`):
   ```
   S < 0      → projected = p10 × (1 + S)               # ratio, never < 0 unless S=−1
   S in 0..1  → projected = p10 + S × (est − p10)
   S in 1..2  → projected = est + (S−1) × (p90 − est)
   S > 2      → projected = p90 × (1 + (S−2))           # ratio, may exceed p90
   ```

4. Wynik:
   - `adjusted` (chart attribute): full per-period day, current bucket =
     realized prorate (`pv_so_far × 60 / elapsed_min`), future buckets =
     projected, past buckets = forecast (niezmienione)
   - `remaining_kwh` (sensor state): kWh remaining today (current scaled +
     future projected, past wykluczone)
   - `target_soc` (SOC %): deficit calc 7-13 oparty o realized current bucket
     + projected future

Edge case: `elapsed_min < 3` → variant = unknown (utility meter just-reset
noise). Threshold zgodny z dashboard `extrapolate_current_bucket_js`.

### Why the 4-zone scale (vs simple linear factor)

**Original formula** `factor = (realized − p10) / (estimate − p10)` używała tylko
2 kwantyli. Dla bucketu z realized < p10 (np. early morning ramp-up gdzie PV ledwo
się rozkręcił), `factor < 0`. Linear extrapolation dla future bucketu z szerokim
zakresem `(estimate − p10)`:

```
factor = −0.6
Future bucket: p10=0.5, estimate=3.0
projected = 0.5 + (−0.6) × (3.0 − 0.5) = 0.5 − 1.5 = −1.0  → clamped to 0  ❌
```

**Nierealistyczna prognoza "0 kWh przez resztę dnia"** bazując na jednym słabym
porannym bucket (incident 2026-05-09 07:10).

**4-zone scale** używa ratio dla S < 0:

```
S = −0.6 (taka sama observation)
Future bucket: p10=0.5, est=3.0
projected = 0.5 × (1 + (−0.6)) = 0.5 × 0.4 = 0.20 kWh/h  ✓ (40% z p10, pessimistic ale realistic)
```

Powyżej p90 podobnie — zamiast unbounded linear growth, ratio daje bounded
extrapolation: realized = 1.2× p90 → projection 1.2× future p90.

## Use case: decyzja o rozładowaniu baterii rano

**Problem**: bateria bywa pełna rano (~80-90% SOC po nocnym charge). Jeśli
spodziewamy się dużego PV w ciągu dnia, bateria nie ma miejsca na surplus —
wszystko leci do sieci po cenach RCE. Czasem **opłaca się rozładować** baterię
przed dniem (np. do 50%) żeby:
- zwolnić miejsce na nadchodzący PV
- wykorzystać energię w domu / sprzedać po wieczornej drogiej cenie

Decyzja zależy od **prognozowanego dziennego surplus PV**. Tu wchodzi Pattern:

- **Adj PV Live** (forecast weather-adjusted): pesymistyczny przy zachmurzonym
  weather forecast, nawet jeśli Solcast estimate jest wysoki
- **Adj PV Live Extrap** (realized prorate): bazuje na current bucket realized,
  ale future buckets to nadal weather-adjusted forecast → mało zmieniony vs Adj PV Live
- **Adj PV Live Extrap Pattern**: **projektuje obserwowaną realizację na future**.
  Jeśli rano PV idzie tak jak `pv_estimate` (factor ≈ 1), pattern projektuje
  że reszta dnia też pójdzie blisko estimate — niezależnie od weather forecast.

### Optymalne okno obserwacji

```
07:40   ← typowy poranny refresh Solcast (sprawdź logi)
07:43   ← elapsed_min=13 dla bucketu 07:30 → pattern stable
07:43-07:55  ← decyzja: porównaj Pattern vs Adj PV Live
08:00   ← bucket boundary; po 08:03 pattern znów stable
```

**Dlaczego krótko po refresh**: zaraz po refresh Solcast morning forecast ma
zwykle szeroki zakres `[p10, estimate]` (Solcast jest niepewny czy będzie
sunny/cloudy). W tym oknie factor mocno koryguje projection — Pattern niesie
najwięcej informacji ponad Adj PV Live.

**Późniejsze refreshe** (np. 09:40, 10:40): zakres `[p10, estimate]` się zwęża
gdy Solcast widzi że jest sunny (p10 idzie w górę bliżej estimate) — Pattern
projektuje wtedy minimalnie ponad forecast, użyteczność spada.

### Próg decyzyjny

Z `target_soc_live_extrapolated_pattern` można wnioskować podobnie do
`target_soc_live`:
- Pattern target_soc ≤ MIN_SOC_PERCENT (10%) → no deficit expected → bezpieczne
  rozładowanie baterii
- Pattern target_soc > 50% → znaczący deficit oczekiwany — NIE rozładowywać

Z `weather_adjusted_pv_live_extrapolated_pattern` (kWh remaining):
- Próg "warto rozładować" zależy od pojemności baterii (~10.7 kWh) i typowego
  zużycia (~0.9 kWh/h × pozostałe godziny dnia). Patrz
  `target_soc_algorithm.md` sekcja 3 "Daily surplus estimation".

## Case study — refresh 09:40 dnia 2026-05-08

Obserwacja **przed** refresh (09:29:30):

| Sensor | Wartość |
|---|---|
| Adj PV Live | 17.91 kWh |
| Extrap (realized prorate) | 15.57 kWh |
| Extrap 5min | 15.92 kWh |
| **Extrap Pattern** | **26.92 kWh** |

Solcast `[p10, estimate]` przed refresh:

| Bucket | estimate | p10 | range |
|---|---|---|---|
| 09:30 | 3.08 | 1.222 | 1.86 |
| 10:00 | 2.81 | 0.446 | 2.37 |
| 10:30 | 2.92 | 0.356 | 2.57 |
| 11:00 | 3.21 | 0.432 | 2.78 |

Realized current bucket (09:00): 1.327 kWh przy elapsed=29 min → projected
full bucket = 1.373 kWh. estimate=1.619, p10=1.111 → factor ≈ 0.515.

Z past buckets (sunny morning, factor blisko 1.0) weighted average ≈ 0.96.
Future projection: rate ≈ p10 + 0.96 × (est - p10) ≈ blisko estimate. Total
remaining ≈ 27 kWh (znacznie powyżej weather-adjusted 17.91).

**Po refresh** (09:45):

| Sensor | Wartość |
|---|---|
| Adj PV Live | 19.91 kWh ↑ (+2.0) |
| Extrap | 16.08 kWh ↑ (+0.5) |
| Extrap 5min | 16.00 kWh ↑ |
| **Extrap Pattern** | **16.49 kWh ↓ (-10.4)** |

Solcast `[p10, estimate]` po refresh — p10 mocno wzrosły (Solcast pewniejszy
że dzień sunny):

| Bucket | estimate | p10 (NEW) | range |
|---|---|---|---|
| 09:30 | 3.54 | **2.94** | **0.60** |
| 10:00 | 2.74 | **1.39** | 1.36 |
| 10:30 | 2.48 | **0.95** | 1.53 |
| 11:00 | 2.78 | **0.79** | 1.99 |

**Wniosek**: Adj PV Live wzrósł bo weather-adjusted używa `pv_estimate10` dla
cloudy buckets — gdy p10 wzrosło, adjusted też. Pattern spadł dramatycznie bo
zakres `[p10, estimate]` się zwęził → factor projektuje minimalnie ponad forecast.

**Pattern teraz blisko innych wariantów** (16.49 vs 16.08 vs 16.00) — ale w
chwili gdy zakres był szeroki (przed refresh) sygnalizował wyraźnie więcej PV
niż weather-adjusted. **To był moment obserwacji wartościowej** — gdyby user
patrzył 09:00-09:30 (przed refresh, w okresie szerokiego zakresu), Pattern
mówił "dziś będzie 27 kWh PV — rozładowanie baterii uzasadnione".

## Limitations / caveats

1. **Pattern wymaga zamkniętych past buckets** w today (utility meter history
   via recorder). Po restart smart_rce w połowie dnia: cache `_realized_pv_today`
   re-fetch przez async loader (bucket boundary co 30 min, +30s offset).
   Initial fetch przy startup.

2. **Past bucket data via 5-min recorder statistics** — wartości to ostatni
   5-min slot przed reset utility meter (:25 dla bucketu (h, 0), :55 dla
   bucketu (h, 30)). Patrz `consumption_profile_loader.py` dla wzorca.

3. **Factor decay**: PATTERN_DECAY=0.7 (current weight 1.0, prev 0.7, ...).
   Po 3 bucketach waga ≈ 0.34. Tunable via `domain/pv_forecast_extrapolation.py`.

4. **Threshold pomijania**: `PATTERN_MIN_FORECAST_KWH = 0.05` kWh/30min.
   Buckets z `pv_estimate / 2 < 0.05` (pre-dawn / post-dusk) są skipped —
   inaczej (estimate − p10) bliskie 0 → factor noisy.

5. **Brak asymetrii up/down**: factor > 1 jest dozwolone (sunnier than estimate).
   Niektóre dni mogą zaskakująco produkować >estimate, Pattern to projektuje.
   Future projection nie jest cap'owany na estimate — może wskazywać iż
   reszta dnia będzie znacząco powyżej forecast.

## Powiązane

- `target_soc_algorithm.md` sekcja 2 "Ekstrapolacja current 30-min bucket" —
  oryginalna idea, zaimplementowana ostatecznie jako 3 warianty extrapolated.
- `target_soc_algorithm.md` sekcja 3 "Daily surplus estimation" — break-even
  dla decyzji rozładowania.
- Dashboard `dashboards/views/rce_forecast.py`: serie **Adj PV Live Extrap
  Pattern** (crimson) na 30-min + hourly chartach.
