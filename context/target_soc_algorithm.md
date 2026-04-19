# Algorytm: target battery SOC o 7:00 (research + design)

**Status**: research + brainstorm, decyzje częściowo otwarte.
**Kontekst biznesowy**: patrz [energy_strategy.md](energy_strategy.md) (taryfa G13, CWU, RCE ceny).
**Stan kodu**: `custom_components/smart_rce/domain/pv_forecast.py:calculate_target_soc()`.

## Cel

Zapewnić wystarczający SOC baterii o 7:00 (start drogiej taryfy 7-13 w G13) żeby w oknie 7:00-13:00 **nie pobierać netto z sieci** (bilans godzinowy).

Po 13:00 zaczyna się tania taryfa → można doładować baterię.

## Inputy / outputs

**Inputy:**
- `AdjustedPvForecast` — lista 30-min okresów z `pv_estimate_adjusted` (kWh/h, hourly rate)
- `ConsumptionProfile | None` — opcjonalny profile consumption per 30-min bucket (Etap A — z prev workday LTS)
- `now: datetime | None` — opcjonalny current timestamp (dla _live variant — start od bieżącego 30-min okresu zamiast 7:00)

**Output:**
- `TargetSocResult(value: int, buckets: list[TargetSocBucket])`
- `value` — target SOC percent (MIN_SOC_PERCENT=10 lub wyższy)
- `buckets` — per 30-min trace: `(period, pv_kwh, cons_kwh, balance, cumulative, is_min)` dla obserwacji

## Stałe (pv_forecast.py:9-13)

- `CONSUMPTION_PER_30MIN = 0.45 kWh` — domyślny (fallback) consumption per 30-min
- `BATTERY_CAPACITY_KWH = 10.7` — pojemność Pylontech H2
- `MIN_SOC_PERCENT = 10` — minimum (ochrona BMS)
- `LOSS_FACTOR = 0.10` — 10% straty konwersji
- `BUFFER_PERCENT = 12` — safety buffer dodawany do target gdy jest dowolny deficit

## Aktualny algorytm (w kodzie po Etap A)

```python
# Per 30-min okres 7:00-13:00:
balance = pv_30min - cons_30min   # cons = profile[bucket] lub CONSUMPTION_PER_30MIN
cumulative_balance += balance
min_balance = min(min_balance, cumulative_balance)

# Po przejściu przez okno:
if min_balance >= 0:
    return MIN_SOC_PERCENT
deficit_kwh = abs(min_balance)
deficit_percent = deficit_kwh / (BATTERY_CAPACITY_KWH / 100)
target = MIN_SOC_PERCENT + deficit_percent * (1 + LOSS_FACTOR) + BUFFER_PERCENT
return max(round(target), MIN_SOC_PERCENT)
```

**Semantyka**: cumulative balance simuluje zmianę SOC baterii w czasie. `min_balance` = najgorszy moment (ile najbardziej "w dół" SOC pójdzie). Deficyt konwertowany na % SOC + straty + buffer.

## Założenia ekonomiczne — hourly billing

**Kluczowa zasada**: rozliczenie z zakładem energetycznym per **godzina** (netto import − export).

| Scenariusz w godzinie 9:00-10:00 | Netto | Koszt |
|---|---|---|
| Pobrałem 2 kWh, oddałem 3 kWh | +1 kWh (export) | Kasa za 1 kWh |
| Pobrałem 3 kWh, oddałem 2 kWh | -1 kWh (import) | Płacę za 1 kWh |
| Pobrałem 2 kWh, oddałem 2 kWh | 0 | 0 |

**Implikacja dla target SOC**:
- W obrębie godziny dodatni i ujemny balans **wzajemnie się kasują** w billingu
- Np. 1st half deficit -0.5 + 2nd half surplus +0.5 → hour balance = 0 → nie płacimy netto, nawet jeśli intra-hour bateria była używana

Czyli **60-min granulacja** dla target SOC mogłaby być bardziej trafna ekonomicznie niż obecne 30-min.

## Fizyka baterii — start_charge_hour gate

**Druga kluczowa obserwacja**: bateria **nie ładuje się automatycznie z PV surplus** — wymaga explicit `battery_charge_current > 0` (set via `_ Inverter ENABLE Battery Charge MORNING` automation).

Smart_rce liczy `start_charge_hour_today` (wg RCE cen) — moment gdy bateria zaczyna się ładować. Przed tą godziną:
- Deficit (PV < cons): pokrywany z baterii (rozładowuje się) lub z sieci (import)
- **Surplus (PV > cons): NIE ładuje baterii — idzie do sieci**

Po `start_charge_hour`:
- Deficit: z baterii
- Surplus: ładuje baterię → akumuluje dla kolejnych godzin

**Implikacja dla algorytmu**: w oknie 7:00 ≤ hour < start_charge_hour, hour surplus nie akumuluje się pozytywnie w cumulative. Tylko hour deficit się kumuluje.

## Propozycja: 60-min hour-aggregated algorytm z start_charge gate

```python
for hour in 7..12:
    hour_balance = sum(pv_30min - cons_30min for 30min slots in hour)
    if hour < start_charge_hour:
        effective = min(hour_balance, 0)   # surplus stracony (do sieci)
    else:
        effective = hour_balance           # surplus akumuluje baterię
    cumulative += effective
    min_balance = min(min_balance, cumulative)
```

**Różnice vs obecny 30-min algo:**

| Scenariusz | 30-min algo (obecny) | 60-min + gate (propozycja) |
|---|---|---|
| Intra-hour: 1st half -0.5, 2nd half +0.5 (netto 0) | min_balance = -0.5 → target ~15% | effective = 0 → nie wpływa, target może być 10% |
| Cała godzina netto -1.0 | min_balance -= 1.0 | effective -= 1.0 (tak samo) |
| Pre-charge hour netto +0.5 (eksport) | cumulative += 0.5 (zaniżone target) | effective = 0 → target wyższe (safer) |
| Post-charge hour netto +1.0 | cumulative += 1.0 | identycznie |

**Net effect**: propozycja jest:
- **Mniej agresywna** w scenariuszach intra-hour split (hourly billing amortyzuje) → niższy target
- **Bardziej konserwatywna** w pre-charge surplus (gate zeruje) → wyższy target

## Otwarte pytania / edge cases

### 1. Czy "nadrabianie intra-hour" jest bezpieczne?

Przed usunięciem 30-min granulacji: jeśli 1st half godz 9 ma deficit 0.6 kWh a bateria jest na MIN_SOC=10%, bateria nie pokryje → import z sieci. 2nd half ma surplus +0.7 kWh → eksport → hour netto +0.1. Billing: dostajemy kasę za 0.1 kWh netto. Ale intra-hour importowaliśmy.

**Czy to jest problem ekonomiczny?** Nie w G13 — płacimy netto. Ale:
- Bateria spadła do MIN_SOC (ochrona BMS)
- Po 2nd half bateria znowu rośnie (z surplus) tylko w post-charge godzinach
- Więc jeśli to pre-charge: bateria w 1st half na MIN, 2nd half nie ładuje się (gate) → kończymy godzinę niżej

**Czy to jest realny scenariusz?** Rano (7-9) PV jest słabe, unlikely że 2nd half ma mocny surplus. Bardziej typowe: cała godzina deficit. Więc edge case rzadki.

### 2. Start_charge_hour rano vs w ciągu dnia

Smart_rce liczy start_charge_hour na bazie RCE. Typowo poranne godziny w nocnej taryfie (np. 3-5) mają najtańsze ceny → start_charge_hour < 7. Wtedy w oknie 7-13 **zawsze** bateria może ładować → gate nie trigeruje.

Edge case: start_charge_hour > 7 (np. jeśli RCE w nocy jest drogi a rano tanie). Wtedy gate ma znaczenie.

### 3. End_charge_hour_today

Analogicznie: do kiedy bateria się ładuje? Czy w oknie 7-13 może być moment gdy ładowanie jest wyłączone?

Zwykle ładowanie trwa do 100% SOC lub do pewnej godziny. Do zbadania w automations.yaml.

### 4. Weather-adjusted forecast (AT6 vs LIVE)

- AT6 — pessimistic modifiers, cloudy cap at hour 7
- LIVE — optimistic, no special capping (Solcast już zoptymalizowany na bieżące warunki)

Algorytm target_soc jest agnostyczny — przyjmuje dowolny `AdjustedPvForecast`. Ale interpretacja "ile SOC jest potrzebne" zależy od tego czy PV estimate jest optymistyczny czy pesymistyczny.

### 5. Consumption profile granularity

Prev_day profile ma granulację 30-min (z LTS 5-min slotów). Jeśli agregujemy do godziny dla target_soc, trace per 30-min i tak jest wartościowy (pokazuje wewnątrzgodzinową strukturę).

### 6. Czy buffer 12% jest "w wartości" czy "w czasie"?

Obecny BUFFER_PERCENT=12 dodaje 12% SOC niezależnie od wielkości deficit. To trochę arbitralne. Alternatywy:
- Procent od deficit (np. 20% * deficit_percent)
- Absolutny % (obecnie)
- Zależny od forecast confidence (pessimistic=więcej)

### 7. Intra-hour bateria MIN_SOC protection

Propozycja 60-min może dopuścić do sytuacji gdzie intra-hour SOC spada poniżej MIN_SOC pomimo że hour netto jest OK. BMS nie pozwoli rozładować < MIN_SOC → import z sieci.

Zabezpieczenie: zachować 30-min intra-hour min check jako **secondary constraint**:
```
target = max(60min_target, 30min_target_with_less_buffer)
```
Gdzie 30min wariant ma mniejszy buffer bo już mamy hourly amortyzację.

## Historia zmian

- **Pre-Etap A**: 30-min, constant `CONSUMPTION_PER_30MIN=0.45`, weekend short-circuit do MIN_SOC
- **Etap A (2026-04-18)**: dodany `ConsumptionProfile` z prev_day LTS fetch (3 profile dla N=1,2,3 workdays). 8 nowych sensorów prev_day + max. 30-min granulacja zachowana.
- **2026-04-18 later**: usunięty weekend short-circuit (do obserwacji). Dodany per-bucket trace jako attribute `buckets`.
- **NOWY (po brainstorm 2026-04-18)**: refactor do 60-min z start_charge_hour gate — **TODO**

## TODO — decyzje do podjęcia

1. **Implementować hour-aggregation + gate?**
   - Jako nowy sensor "Target Battery SOC Hourly" (side-by-side comparison)?
   - Albo zmiana w istniejącym `calculate_target_soc`?
   - Lub dual-check (max ze 60-min i 30-min)?

2. **Jak obsłużyć brak `start_charge_hour`?**
   - Smart_rce ma `sensor.rce_start_charge_hour_today` — input do algorytmu
   - Domyślnie: brak = nie gate (naive akumulacja)
   - Fallback: hardcoded np. 6 (bezpieczny poranek)

3. **Buffer tuning** (punkt 6 powyżej) — odłóż, najpierw obserwacja Etapu A

4. **Intra-hour safety** (punkt 7) — odłóż, najpierw zdecydować o 60-min vs 30-min

5. **Weryfikacja empiryczna**:
   - Porównać `_live` (30-min, constant 0.45) vs `_prev_day_X` (30-min, profile) przez tydzień
   - Po tym zdecydować czy 60-min refactor jest wartościowy (jeśli 30-min daje sensowne wyniki, może nie trzeba)

## Powiązane pliki

- `custom_components/smart_rce/domain/pv_forecast.py` — implementacja
- `custom_components/smart_rce/pv_forecast_coordinator.py` — orkiestracja (call sites)
- `custom_components/smart_rce/sensor.py` — wystawiana wartość + trace
- `context/energy_strategy.md` — szerszy kontekst strategii (CWU, grzałki, taryfy)
- `/Users/mark/git/home-assistant-ops/research/TODO-observe-prev-day-target-soc.md` — tygodniowa obserwacja Etapu A
