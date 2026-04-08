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

## Bateria

- Pylontech H2, 3 moduły
- Usable capacity: **10.7 kWh**
- 1% SOC ≈ 0.107 kWh
- Minimum SOC: 10% (ochrona)
- Straty konwersji: ~10%

## Taryfa G13 — weekend i święta

**W weekendy i dni wolne od pracy cały dzień jest tani prąd** — nie ma stref.
Algorytm ładowania baterii dotyczy **tylko dni roboczych** (pon-pt).

## Algorytm: target SOC o 7:00 (dni robocze)

### Cel
Zapewnić wystarczający SOC baterii o 7:00 żeby przetrwać drogą taryfę 7:00-13:00
bez pobierania z sieci. O 13:00 zaczyna się tania taryfa i można doładować.

### Dane wejściowe (dostępne o 6:00):
1. Solcast forecast — pv_estimate, pv_estimate10, pv_estimate90 per 30min
2. Prognoza pogody WetterOnline — condition per godzinę
3. Aktualny SOC baterii
4. Estymowane zużycie domu (z historii lub stałe)

### Krok 1: Weather-adjusted PV estimate per 30min (7:00-13:00)

| Condition WetterOnline | Estimate Solcast | Mnożnik |
|---|---|---|
| sunny | pv_estimate | 1.0 |
| partlycloudy-variable | pv_estimate | 0.8 |
| partlycloudy | pv_estimate10 | 1.0 |
| cloudy / inne | pv_estimate10 | 0.7 |

### Krok 2: Symulacja kumulacji deficytu godzina po godzinie

```
cumulative_balance = 0
min_balance = 0

Dla każdego 30min okresu od 7:00 do 13:00:
  expected_pv = weather_adjusted_estimate (z kroku 1)
  expected_consumption = estymowane zużycie
  balance = expected_pv - expected_consumption
  cumulative_balance += balance
  min_balance = min(min_balance, cumulative_balance)
```

### Krok 3: Target SOC

```
deficit_kwh = abs(min_balance)
deficit_percent = deficit_kwh / 0.107  # kWh -> % SOC
losses = deficit_percent * 0.10  # 10% straty konwersji
target_soc = 10 + deficit_percent + losses + 5  # limit + deficyt + straty + bufor
```

### Weryfikacja na danych 2026-04-08 (pochmurny dzień)

- Pogoda: cloudy cały dzień
- Solcast est10 × 0.7 dla 7:00-13:00: ~4.1 kWh PV
- Zużycie 7:00-13:00: ~5.0 kWh
- Najniższy punkt kumulacji: -0.74 kWh o 11:00
- deficit_percent: 6.9%
- Target SOC: 10 + 6.9 + 0.7 + 5 = ~23%
- Rzeczywisty SOC 7:00: 24% → bateria spadła do 13% (na styk)
- Z targetem 27% (uwzględniając realne straty) byłoby bezpieczniej
