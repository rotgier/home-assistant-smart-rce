# Obserwacja: lag między relay_power a house_consumption

**Data:** 2026-04-11
**Screenshot:** relay_power_vs_house_consumption_lag.png

## Obserwacja

Po włączeniu grzałki (big lub small relay):
- **house_consumption** reaguje po 2-4 sekundach (inwerter raportuje zmianę)
- **relay_power** reaguje po max 2 sekundach (OXT ZigBee relay raportuje natychmiast)
- Czasami relay_power reaguje **szybciej** niż house_consumption

## Implikacja dla EMS

Sensor `house_consumption_minus_heaters_minus_pv` bazuje na obu źródłach.
Krótki lag (2-4s) nie jest problemem — EMS pracuje na średnich 2-minutowych.
Ale przy przełączaniu grzałek może być chwilowy spike/drop w pv_available
zanim oba sensory się zsynchronizują.
