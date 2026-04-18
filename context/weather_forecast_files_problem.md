# Problem: nadmierne pliki prognozy pogody w /config/smart_rce/

## Stan

- **593 plików** w `/config/smart_rce/` → **9 MB**
- ~110-120 plików/dzień (co ~5 min nowy plik, każdy ~12 KB)
- Format: `hourly_forecast_<timestamp>.json` — pełna prognoza godzinowa
- Pliki nigdy nie są usuwane ani rotowane
- Narastają od 8 kwietnia (5 dni → 593 plików)
- Przy tym tempie: **~3600 plików/miesiąc → ~43 MB/miesiąc**

## Źródło

`weather_listener.py` — metoda `_save_forecast_to_file()` wywoływana przy **każdej zmianie** prognozy WetterOnline (~co 5 min). Zapisuje pełny JSON z 24-godzinną prognozą.

Zapis następuje gdy:
- Prognoza się zmieniła (inne dane niż ostatnia)
- Prognoza zmieniła długość
- Pierwszy zapis w sesji

## Pytania do rozwiązania

1. **Czy te pliki są w ogóle potrzebne?** `WeatherForecastHistorySensor` (RestoreSensor) persystuje historię pogody przez HA recorder. Pliki to duplikacja.
2. **Jeśli potrzebne do audytu/debugowania** — ile przechowywać? (np. ostatnie 24h? ostatni dzień?)
3. **Czy zmienić na rotację** — np. jeden plik per godzina zamiast per zmiana?
4. **Czy wyłączyć zapis** i polegać wyłącznie na RestoreSensor + HA recorder?
