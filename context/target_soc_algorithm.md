# Algorytm: target battery SOC o 7:00 (research + design)

**Status**: research + brainstorm, decyzje częściowo otwarte.
**Kontekst biznesowy**: patrz [energy_strategy.md](energy_strategy.md) (taryfa G13, CWU, RCE ceny).
**Stan kodu**: `custom_components/smart_rce/domain/pv_forecast.py:calculate_target_soc()`.

## Cel

Zapewnić wystarczający SOC baterii o 7:00 (start drogiej taryfy 7-13 w G13) żeby w oknie 7:00-13:00 **nie pobierać netto
z sieci** (bilans godzinowy).

Po 13:00 zaczyna się tania taryfa → można doładować baterię.

## Inputy / outputs

**Inputy:**

- `AdjustedPvForecast` — lista 30-min okresów z `pv_estimate_adjusted` (kWh/h, hourly rate)
- `ConsumptionProfile | None` — opcjonalny profile consumption per 30-min bucket (Etap A — z prev workday LTS)
- `now: datetime | None` — opcjonalny current timestamp (dla _live variant — start od bieżącego 30-min okresu zamiast 7:
  00)

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

**Semantyka**: cumulative balance simuluje zmianę SOC baterii w czasie. `min_balance` = najgorszy moment (ile
najbardziej "w dół" SOC pójdzie). Deficyt konwertowany na % SOC + straty + buffer.

## Założenia ekonomiczne — hourly billing

**Kluczowa zasada**: rozliczenie z zakładem energetycznym per **godzina** (netto import − export).

| Scenariusz w godzinie 9:00-10:00 | Netto           | Koszt          |
|----------------------------------|-----------------|----------------|
| Pobrałem 2 kWh, oddałem 3 kWh    | +1 kWh (export) | Kasa za 1 kWh  |
| Pobrałem 3 kWh, oddałem 2 kWh    | -1 kWh (import) | Płacę za 1 kWh |
| Pobrałem 2 kWh, oddałem 2 kWh    | 0               | 0              |

**Implikacja dla target SOC**:

- W obrębie godziny dodatni i ujemny balans **wzajemnie się kasują** w billingu
- Np. 1st half deficit -0.5 + 2nd half surplus +0.5 → hour balance = 0 → nie płacimy netto, nawet jeśli intra-hour
  bateria była używana

Czyli **60-min granulacja** dla target SOC mogłaby być bardziej trafna ekonomicznie niż obecne 30-min.

## Fizyka baterii — start_charge_hour gate

**Druga kluczowa obserwacja**: bateria **nie ładuje się automatycznie z PV surplus** — wymaga explicit
`battery_charge_current > 0` (set via `_ Inverter ENABLE Battery Charge MORNING` automation).

Smart_rce liczy `start_charge_hour_today` (wg RCE cen) — moment gdy bateria zaczyna się ładować. Przed tą godziną:

- Deficit (PV < cons): pokrywany z baterii (rozładowuje się) lub z sieci (import)
- **Surplus (PV > cons): NIE ładuje baterii — idzie do sieci**

Po `start_charge_hour`:

- Deficit: z baterii
- Surplus: ładuje baterię → akumuluje dla kolejnych godzin

**Implikacja dla algorytmu**: w oknie 7:00 ≤ hour < start_charge_hour, hour surplus nie akumuluje się pozytywnie w
cumulative. Tylko hour deficit się kumuluje.

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

| Scenariusz                                         | 30-min algo (obecny)                | 60-min + gate (propozycja)                      |
|----------------------------------------------------|-------------------------------------|-------------------------------------------------|
| Intra-hour: 1st half -0.5, 2nd half +0.5 (netto 0) | min_balance = -0.5 → target ~15%    | effective = 0 → nie wpływa, target może być 10% |
| Cała godzina netto -1.0                            | min_balance -= 1.0                  | effective -= 1.0 (tak samo)                     |
| Pre-charge hour netto +0.5 (eksport)               | cumulative += 0.5 (zaniżone target) | effective = 0 → target wyższe (safer)           |
| Post-charge hour netto +1.0                        | cumulative += 1.0                   | identycznie                                     |

**Net effect**: propozycja jest:

- **Mniej agresywna** w scenariuszach intra-hour split (hourly billing amortyzuje) → niższy target
- **Bardziej konserwatywna** w pre-charge surplus (gate zeruje) → wyższy target

## Otwarte pytania / edge cases

### 1. Czy "nadrabianie intra-hour" jest bezpieczne?

Przed usunięciem 30-min granulacji: jeśli 1st half godz 9 ma deficit 0.6 kWh a bateria jest na MIN_SOC=10%, bateria nie
pokryje → import z sieci. 2nd half ma surplus +0.7 kWh → eksport → hour netto +0.1. Billing: dostajemy kasę za 0.1 kWh
netto. Ale intra-hour importowaliśmy.

**Czy to jest problem ekonomiczny?** Nie w G13 — płacimy netto. Ale:

- Bateria spadła do MIN_SOC (ochrona BMS)
- Po 2nd half bateria znowu rośnie (z surplus) tylko w post-charge godzinach
- Więc jeśli to pre-charge: bateria w 1st half na MIN, 2nd half nie ładuje się (gate) → kończymy godzinę niżej

**Czy to jest realny scenariusz?** Rano (7-9) PV jest słabe, unlikely że 2nd half ma mocny surplus. Bardziej typowe:
cała godzina deficit. Więc edge case rzadki.

### 2. Start_charge_hour W ŚRODKU okna 7-13 (codzienność, nie edge case)

**Empirycznie zweryfikowane** (historia `sensor.rce_start_charge_hour_today`, kwiecień 2026):

- Typowa wartość w dni robocze: **10-11** (najtańsza RCE godzina — bateria czeka na tanie ładowanie)
- Weekend: wartość niższa, ale wtedy cały dzień tania taryfa G13 → target SOC nieistotny

**Implikacje dla okna 7-13:**

- Godziny 7-10 (pre-charge, ~3h): bateria NIE ładuje, surplus → sieć, tylko deficit akumuluje
- Godziny 10-13 (post-charge, ~3h): bateria ładuje z PV surplus → normalna akumulacja

**Dlaczego gate jest fundamentalny, nie edge:**

- Większość okna 7-13 jest pre-charge → obecny algo bez gate może zaniżać target SOC w scenariuszach gdzie pre-charge hour ma netto dodatni balans (surplus do sieci, nie dla baterii)
- Pozytywne akumulowanie pre-charge surplus fałszuje cumulative dla godzin 10-13 (gdzie już nie ma deficit bo PV silne)

### 3. End_charge_hour_today

~13:00 lub później → poza oknem obliczeń target SOC. **Ignorujemy na razie** — do uwzględnienia gdy rozszerzymy algorytm o wieczorny charge cycle (po 13:00, tania taryfa).

### 4. Weather-adjusted forecast (AT6 vs LIVE)

- AT6 — pessimistic modifiers, cloudy cap at hour 7
- LIVE — optimistic, no special capping (Solcast już zoptymalizowany na bieżące warunki)

Algorytm target_soc jest agnostyczny — przyjmuje dowolny `AdjustedPvForecast`. Ale interpretacja "ile SOC jest
potrzebne" zależy od tego czy PV estimate jest optymistyczny czy pesymistyczny.

### 5. Consumption profile granularity

Prev_day profile ma granulację 30-min (z LTS 5-min slotów). Jeśli agregujemy do godziny dla target_soc, trace per 30-min
i tak jest wartościowy (pokazuje wewnątrzgodzinową strukturę).

### 6. Czy buffer 12% jest "w wartości" czy "w czasie"?

Obecny BUFFER_PERCENT=12 dodaje 12% SOC niezależnie od wielkości deficit. To trochę arbitralne. Alternatywy:

- Procent od deficit (np. 20% * deficit_percent)
- Absolutny % (obecnie)
- Zależny od forecast confidence (pessimistic=więcej)

### 7. Intra-hour bateria MIN_SOC protection

Propozycja 60-min może dopuścić do sytuacji gdzie intra-hour SOC spada poniżej MIN_SOC pomimo że hour netto jest OK. BMS
nie pozwoli rozładować < MIN_SOC → import z sieci.

Zabezpieczenie: zachować 30-min intra-hour min check jako **secondary constraint**:

```
target = max(60min_target, 30min_target_with_less_buffer)
```

Gdzie 30min wariant ma mniejszy buffer bo już mamy hourly amortyzację.

## Historia zmian

- **Pre-Etap A**: 30-min, constant `CONSUMPTION_PER_30MIN=0.45`, weekend short-circuit do MIN_SOC
- **Etap A (2026-04-18)**: dodany `ConsumptionProfile` z prev_day LTS fetch (3 profile dla N=1,2,3 workdays). 8 nowych
  sensorów prev_day + max. 30-min granulacja zachowana.
- **2026-04-18 later**: usunięty weekend short-circuit (do obserwacji). Dodany per-bucket trace jako attribute
  `buckets`.
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

## Open follow-up topics — next session (post-observation)

Rozważane po wdrożeniu `should_block_battery_discharge` (pre/post-charge, 2026-04-20):

### 1. Inter-hour surplus transfer w pre-charge

**Problem**: W pre-charge `battery_charge_max_current_toggle=False` — bateria **nie ładuje** się (ani z sieci, ani z PV). Surplus PV w godzinie X **idzie do sieci** (nie akumuluje dla godziny X+1).

Obecny `calculate_target_soc` iteruje 30-min periods od 7:00 do 13:00 i kumuluje `balance = PV - cons`. Zakłada że surplus z godziny 8:00-9:00 pokrywa deficit w godzinie 9:00-10:00. **To nie jest prawda w pre-charge** bo bateria nie ładuje się — surplus idzie do sieci bezpowrotnie.

**Opcje rozwiązania**:

**A) Reset cumulative na granicy godzin (30-min bucket semantics)**:
```python
# In calculate_target_soc loop:
if crossing hour boundary (in pre-charge):
    cumulative_balance = min(cumulative_balance, 0)  # surplus stracony, deficit kumuluje się
```

**B) Agregacja do 60-min windows**:
Worse granularity, ale zgodne z hourly billing semantics. Tracimy informację o intra-hour variance.

**C) Hybrid per-hour max constraint**:
W ramach godziny dopuszczamy 30-min kumulacja (intra-hour surplus może pokryć intra-hour deficit dzięki hourly netto billing). Ale między godzinami cumulative nie wznosi się wyżej niż 0 w pre-charge.

User (z dyskusji): **skłania się do 30-min windows** (status quo) + jakiś mechanizm "nie pozwól cumulative rosnąć między godzinami". Pre-charge vs post-charge rozróżnienie (post-charge bateria ładuje → surplus przenosi się).

**TODO**: zaprojektować konkretną zmianę w `calculate_target_soc`. Obecny kod iteruje 30-min periods — dodać parameter `pre_charge_end: time | None` (wzięty z `input_datetime.rce_start_charge_hour_today_override`). Wewnątrz pre-charge (okres ≤ pre_charge_end) kumulatywny balance nie może być pozytywny (clamping do 0 gdy przekroczymy zero od góry na granicy godziny).

### 2. Ekstrapolacja current 30-min bucket (in-progress period)

**Problem**: obecna pętla w `calculate_target_soc` zaczyna od bieżącego 30-min okresu z `now` (jeśli podane). W trakcie trwającego okresu (np. 08:15), używa pełnej wartości bucket 08:00-08:30 — zakłada że będzie pełny 30-min. Ale w 08:15 minęło dopiero 15 min z 30.

**Rozwiązanie już istnieje w `rce_forecast.py` (dashboard)**: ekstrapolacja current bucket — `value_so_far / elapsed_fraction * multiplier`. Widać na wykresach aktualną ekstrapolację do końca bucket.

**TODO**: zaaplikować tę samą logikę do `calculate_target_soc`:
- Dla current 30-min period (gdzie `now` mieści się): ekstrapolować remaining value
- PV: `PV_now_instant × remaining_minutes / 30` (assumption: stała moc przez resztę okresu)
- Consumption: z profile lub constant × `remaining_minutes / 30`

**Use case**: o 08:30 decyzja czy oddawać <50% SOC będzie trafniejsza z ekstrapolowanym bieżącym oknem. Bez tego sensor pokazuje "wartość dla pełnego bucketu" co może być nieadekwatne w połowie okresu.

### 3. Daily surplus estimation — "czy warto discharge poniżej 50%"

**Problem**: Idea discharge <50% SOC przed rana → zrobienie miejsca na PV z dnia. Ale w dni o małym forecast PV + wysokie RCE ceny eksportu → **nie tracimy dużo** gdy bateria pełna (i tak eksportujemy cena RCE ~~ cena unikanego importu). Discharge <50% ma sens tylko gdy oczekujemy dużego surplus który nie zmieściłby się w baterii pełnej.

**Estymator break-even**:

```
Bateria 10.7 kWh. Typical usable discharge 50% → 30% = 20% × 0.107 = 2.14 kWh.
Koszt discharge teraz: 2.14 kWh × RCE_export_price_now (utrata eksportu)
Zysk: 2.14 kWh × (evening_drogi_price - evening_RCE_export_price)
       gdy bateria ma miejsce na PV i ładuje się z taniego RCE.

Break-even: discharge tylko gdy przewidywane PV surplus w ciągu dnia > 2.14 kWh.
```

**Dane potrzebne**:
- Solcast forecast today (Solcast: `sensor.solcast_pv_forecast_prognoza_na_dzisiaj`)
- Consumption estimate today (z prev_day profile)
- Delta = PV_forecast - expected_consumption — jeśli > threshold, discharge worth it

**TODO**: osobny sensor `sensor.rce_daily_surplus_estimate` (kWh). Obliczany z `adjusted_at_6.total_kwh` - estimated_consumption_today. Używany jako trigger condition w `(DATE) Battery Discharge Max at 8`:
```yaml
- condition: numeric_state
  entity_id: sensor.rce_daily_surplus_estimate
  above: 2.14  # kWh — minimum żeby miało sens
```

Alternatywnie: sensor binarny `binary_sensor.rce_discharge_makes_sense` wyliczany z cen RCE + surplus.

### 4. Skip `block_charge` w dni o niskiej prognozowanej energii

**Problem**: Obecnie `block_charge` (chronione `hourly_balance_negative`) wyłącza toggle ładowania gdy `exported_wh<0` w guard window (DoD=0 OR block_discharge=True). Logika: "nie ładuj z drogiej sieci w godzinach gdzie powinniśmy konsumować z PV surplus".

W **dni z małą prognozowaną energią** (chmury/zima) taka ochrona może być kontrproduktywna:
- PV nie dostarczy wystarczająco dużo energii żeby naładować baterię do `target SOC`
- Małe chwilowe importy (np. 50-200 Wh per godzina) są akceptowalną ceną za zapewnienie że bateria będzie naładowana na drogie wieczorne godziny
- Lepiej zapłacić teraz niż wieczorem, gdzie RCE bywa 2-3× droższe

**Propozycja**:
- Rozszerzyć semantykę `input_select.ems_water_heater_strategy` → przemianowane np. `ems_energy_strategy` (obejmuje szersze decyzje energetyczne, nie tylko grzałki)
- W trybie `BATTERY_FIRST` / `LOW_ENERGY_DAY`: `BatteryManager` **ignoruje** `block_charge` (zostawia toggle on nawet przy exported_wh<0)
- Pozostaje ochrona discharge (`block_discharge`) bez zmian — to oddzielny wymiar
- Optimum: automatyczne wykrycie low-energy-day z Solcast forecast (powiązane z punktem 3 — daily surplus estimation); gdy `rce_daily_surplus_estimate < threshold` → automatycznie tryb BATTERY_FIRST

**Ryzyko**: bez block_charge, bateria może ładować się z sieci w każdym momencie gdy toggle=on i PV<cons. Dla safety pozostawić override w drogich RCE godzinach (np. peak 7-10, 17-22).

**Use case dzisiaj (2026-04-20)**: poranek bez słońca, SOC 44%, pre-charge window z block_discharge hysteresis działał poprawnie, ale po post-charge (12:00+) toggle włączył się + przez chwilę była flapa `block_charge` True→False (12:17-12:18). Chociaż zadziałało jak zaprojektowane, jutro przy low-energy-day flag mogłoby pozwolić na kilka Wh importu żeby przyspieszyć ładowanie zamiast czekać aż chmury się rozejdą.
