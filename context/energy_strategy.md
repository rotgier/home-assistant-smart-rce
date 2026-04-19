# Strategia zarządzania energią

## Taryfa Tauron G13

| Godziny       | Strefa | Cena |
|---------------|---|---|
| 22:00 - 07:00 | tania | niska |
| 07:00 - 13:00 | droga | wysoka |
| 13:00 - 19:00 | tania | niska |
| 19:00 - 22:00 | droga | wysoka (szczyt 19-22) |

## Kluczowy moment: 6:00 rano

Ostatnia szansa na doładowanie baterii z sieci po taniej taryfie (do 7:00).
Decyzja: ile doładować zależy od prognozy PV na dzisiaj.

## Prognoza PV — Solcast

Sensor `sensor.solcast_forecast_at_6` — snapshot prognozy Solcast z godziny 6:00.
- State: sumaryczna prognoza PV na dzień (kWh)
- Atrybut `forecast`: lista 30-minutowych okresów z `pv_estimate` (kWh)

### Dashboard card (do wklejenia w ApexCharts):
```yaml
- entity: sensor.solcast_forecast_at_6
  yaxis_id: energy
  attribute: forecast
  data_generator: |
    return entity.attributes.forecast.map(item => {
      return [new Date(item.period_start).getTime(), item.pv_estimate ];
    });
  type: area
  curve: stepline
  stroke_width: 1
  unit: kWh
  float_precision: 2
  show:
    datalabels: true
    hidden_by_default: true
    in_header: false
  group_by:
    duration: 30 minutes
    func: last
```

## Battery charge selector i godzina startu ładowania

### Obecny stan (częściowo ręczny)

Automatyzacja `_ Inverter ENABLE Battery Charge MORNING` (id: 1717069389765)
ustawia `input_number.battery_charge_current` > 0, co zezwala na ładowanie baterii.
Godzina startu jest **ustawiana ręcznie** w automatyzacji na podstawie:
- Ceny RCE (im taniej, tym wcześniej zaczynamy ładować)
- Prognozy PV (im gorzej, tym więcej trzeba naładować z sieci)

**Guard w automatyzacjach grzałek:** `input_number.battery_charge_current > 0`
— grzałki nie włączają się dopóki nie zezwolono na ładowanie baterii.
To zapobiega sytuacji, gdy grzałka włącza się o 7:00 a bateria jeszcze nie
zaczęła się ładować (bo czekamy na tańszą godzinę).

### Docelowo (smart_rce)

Smart_rce już liczy `start_charge_hour` i `end_charge_hour` na podstawie cen RCE.
Te wartości powinny sterować automatyzacją ładowania — ale jeszcze nie są
zintegrowane z pełną logiką (taryfa G13, prognoza PV, target SOC).

**TODO:** Połączyć logikę start_charge_hour z target SOC i prognozą PV,
żeby automatyzacja ładowania działała w pełni autonomicznie.

## Logika Battery CHARGE in the morning

Na bazie `sensor.sunny_hours_today_morning_forecast` (zakodowana wartość 3-cyfrowa):
- sunny × 100 + sunny_and_variable × 10 + total_good_conditions

| sunny_hours | target SOC | Interpretacja |
|---|---|---|
| < 2 | 45% | Bardzo pochmurno — ładuj dużo |
| < 4 | 40% | Pochmurno |
| < 14 | 35% | Trochę słońca |
| < 24 | 30% | Umiarkowanie słonecznie |
| < 34 | 25% | Słonecznie |
| < 35 | 22% | Bardzo słonecznie |
| < 125 | 20% | Dużo słońca + variable |
| < 235 | 15% | Prawie pełne słońce |
| >= 235 | 10% | Pełne słońce — PV wystarczy |

## Rozliczenie energii — bilansowanie godzinowe

**Kluczowa zasada:** Rozliczenie z zakładem energetycznym odbywa się na podstawie
**sumarycznego bilansu energii w danej godzinie** (netto import/eksport).

Przykład: jeśli w godzinie 12:00-13:00 pobrałem 2 kWh a oddałem 3 kWh →
płacę 0 zł za pobór i dostaję kasę za 1 kWh netto eksportu.

**Implikacje dla strategii:**
- Eksport w danej godzinie nie jest "stracony" dopóki bilans godzinowy jest dodatni
- Włączenie grzałki w godzinie z dużym eksportem "konsumuje" nadwyżkę — nie płacimy za ten prąd
- Ale jeśli grzałka odwróci bilans na netto import → zaczynamy płacić (po cenie taryfy)
- `sensor.total_export_import_hourly` = skumulowany bilans netto w bieżącej godzinie (dodatni = netto eksport)

**Strategia grzałek powinna:**
1. Unikać odwracania bilansu godzinowego na import (nie grzej za prąd z sieci)
2. Wykorzystywać nadwyżkę eksportu (lepiej grzać wodę niż oddawać po niskiej cenie)
3. Uwzględniać że w połowie godziny bilans może się jeszcze zmienić

## Bateria

- Pylontech H2, 3 moduły
- Usable capacity: **10.7 kWh**
- 1% SOC ≈ 0.107 kWh
- Minimum SOC: 10% (ochrona)
- Straty konwersji: ~10%

### Battery charge limit

`sensor.battery_charge_limit` — maksymalny prąd ładowania z BMS (ampery).
Koreluje z SoC ale jest dokładniejszy — bezpośredni odczyt z BMS.

| battery_charge_limit | Max charging power | Typowy SoC |
|---|---|---|
| 18A | ~5200W | niski (< 90%) |
| 7A | ~2000W | średni (90-96%) |
| 2A | ~500W | wysoki (97-99%) |
| 0A | 0W | 100% |

**Implikacja:** Zamiast estymować ile bateria może przyjąć na podstawie SoC,
można użyć `battery_charge_limit * voltage (~290V)` jako bezpośredni limit.

## Grzałki CWU — sterowanie z EMS

Dwie grzałki elektryczne: BIG (3000W), SMALL (1500W).
Kombinacje: OFF (0W) → SMALL (1.5kW) → BIG (3kW) → BOTH (4.5kW).

### Mode: ASAP — super słoneczny dzień, niskie ceny oddawania

Agresywne grzanie. Włączamy grzałki jak najszybciej gdy jest wystarczająco PV.
Stałe progi niezależne od SoC/battery_charge_limit.

Step-up OFF → SMALL → BIG → BOTH:
| Target | turn_on (pv >) | Utrzymanie (pv >) |
|---|---|---|
| SMALL | 1800 | 1300 |
| BIG | 3300 | 2800 |
| BOTH | 4800 | 4300 |

### Mode: WASTED — niepewna pogoda, priorytet bateria

Konserwatywne. Bateria ładuje się na maksa. Grzałki włączane dopiero gdy
energia jest faktycznie marnowana (eksportowana do sieci):

1. Pozwól baterii ładować pełną mocą
2. Gdy bilans godzinowy staje się dodatni (netto eksport) → włącz BIG
3. Gdy mimo BIG nadal eksport rośnie → włącz też SMALL

Progi bazowane na `pv_surplus = pv_available - battery_max_charge_power`.

## Taryfa G13 — weekend i święta

**W weekendy i dni wolne od pracy cały dzień jest tani prąd** — nie ma stref.
Algorytm ładowania baterii dotyczy **tylko dni roboczych** (pon-pt).

## Algorytm: target SOC o 7:00 (dni robocze)

**Cel**: Zapewnić wystarczający SOC baterii o 7:00 żeby w oknie 7:00-13:00 (droga taryfa G13) **nie pobierać netto z sieci** — zgodnie z zasadą hourly billing (sekcja "Rozliczenie energii").

**Kluczowe implikacje hourly billing dla algorytmu:**
- W obrębie godziny dodatni i ujemny bilans wzajemnie się kasują — intra-hour surplus "dogoni" intra-hour deficit w rozliczeniu
- Przed `start_charge_hour_today` bateria NIE ładuje się → surplus PV w tych godzinach idzie do sieci, NIE akumuluje na kolejne godziny (tylko intra-hour kasowanie)
- Po `start_charge_hour_today` surplus ładuje baterię → akumuluje międzygodzinowo

**Pełny design + research + brainstorm wyniesiony do osobnego pliku**: [target_soc_algorithm.md](target_soc_algorithm.md) — aktualny algorytm (po Etapie A), otwarte pytania (60-min vs 30-min granulacja, start_charge gate, buffer tuning), edge cases i TODO decyzje.

**Stan kodu**: `custom_components/smart_rce/domain/pv_forecast.py:calculate_target_soc()`.
