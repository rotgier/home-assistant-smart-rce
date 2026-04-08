# Symulacje algorytmu target SOC

## Dane referencyjne: 2026-04-08 (pochmurny dzień)

Solcast at 6: 27.9 kWh, actual PV: 9.5 kWh, pogoda: cloudy cały dzień
Bateria: start 24% o 7:00, minimum 13% o ~11:20, potrzeba ~23-25%

### Porównanie podejść (dzisiaj):

| Podejście | Target SOC | Actual potrzeba | Ocena |
|---|---|---|---|
| est10 × 0.7 (bez cap) | 16% | 23-25% | za mało! |
| est10 × 0.7 + cap rano | **30%** | 23-25% | OK, bezpieczny bufor |

### Symulacja AT6 z cap na PV rano (2026-04-08, cloudy):

| Okres | est10×0.7 | capped | act PV | cons | cumul |
|---|---|---|---|---|---|
| 07:00 | 0.380 | 0.100 | 0.100 | 0.45 | -0.350 |
| 07:30 | 0.701 | 0.100 | 0.100 | 0.45 | -0.700 |
| 08:00 | 0.950 | 0.200 | 0.300 | 0.45 | -0.950 |
| 08:30 | 1.124 | 0.200 | 0.400 | 0.45 | -1.200 |
| 09:00 | 1.213 | 0.350 | 0.500 | 0.45 | -1.300 |
| 09:30 | 1.275 | 0.350 | 0.300 | 0.45 | -1.400 |
| 10:00 | 1.310 | 0.400 | 0.400 | 0.45 | -1.450 |
| 10:30 | 1.303 | 0.400 | 0.400 | 0.45 | -1.500 |
| 11:00 | 1.316 | 1.316 | 0.500 | 0.45 | -0.634 |
| 11:30 | 1.358 | 1.358 | 1.000 | 0.45 | +0.274 |
| 12:00 | 1.248 | 1.248 | 1.000 | 0.45 | +1.072 |
| 12:30 | 1.000 | 1.000 | 0.800 | 0.45 | +1.622 |

Min cumul: -1.500 kWh → deficit 14.0% SOC → **target 30%**

## Symulacja na jutro: 2026-04-09 (środa, mix pogody)

Pogoda: cloudy 7-10, partlycloudy 11, partlycloudy-variable 12+
Solcast est total (7-13): 37.7 kWh

| Okres | pogoda | est | est10 | adj | cons | cumul |
|---|---|---|---|---|---|---|
| 07:00 | cloudy | 0.76 | 0.20 | 0.10 | 0.45 | -0.350 |
| 07:30 | cloudy | 1.36 | 0.35 | 0.10 | 0.45 | -0.700 |
| 08:00 | cloudy | 1.99 | 0.56 | 0.20 | 0.45 | -0.950 |
| 08:30 | cloudy | 2.69 | 0.81 | 0.20 | 0.45 | -1.200 |
| 09:00 | cloudy | 3.28 | 1.01 | 0.35 | 0.45 | -1.300 |
| 09:30 | cloudy | 3.73 | 1.15 | 0.35 | 0.45 | -1.400 |
| 10:00 | cloudy | 4.19 | 1.30 | 0.40 | 0.45 | -1.450 |
| 10:30 | cloudy | 4.63 | 1.46 | 0.40 | 0.45 | -1.500 |
| 11:00 | partlycloudy | 4.96 | 1.59 | 3.47 | 0.45 | +1.521 |
| 11:30 | partlycloudy | 5.13 | 1.67 | 3.59 | 0.45 | +4.659 |
| 12:00 | partly-var | 5.00 | 1.57 | 4.00 | 0.45 | +8.212 |
| 12:30 | partly-var | 4.64 | 1.32 | 3.71 | 0.45 | +11.472 |

Min cumul: -1.500 kWh → **target 30%**

## Porównanie scenariuszy pogodowych (dane Solcast jutro):

| Pogoda | Target SOC | Min cumul |
|---|---|---|
| Cały dzień cloudy | **30%** | -1.500 kWh |
| Cały dzień partlycloudy | **10%** | +0.000 kWh |
| Cały dzień sunny | **10%** | +0.000 kWh |
| Jutro (cloudy rano → partly od 11) | **30%** | -1.500 kWh |

## Wnioski

1. Cloudy rano (7-10:30) dominuje target SOC — niezależnie od pogody w południe
2. Capy na PV rano przy cloudy są kluczowe — bez nich algorytm zawyża PV 2-3x
3. Przy partlycloudy/sunny PV wystarcza od rana — target = 10% (minimum)
4. 30% przy cloudy daje ~5-7% bufor ponad potrzebę (23-25%) — bezpiecznie

## Cloudy cap values (kWh per 30min):

| Godzina | Max PV przy cloudy |
|---|---|
| 7 | 0.10 |
| 8 | 0.20 |
| 9 | 0.35 |
| 10 | 0.40 |
| 11+ | bez limitu (est10 × 0.7) |
