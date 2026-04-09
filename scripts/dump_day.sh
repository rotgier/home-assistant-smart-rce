#!/bin/bash
# Zrzuca dane z HA za dany dzień do katalogu context/YYYY-MM-DD/
#
# Użycie: ./scripts/dump_day.sh 2026-04-09
#
# Wymaga: SSH do HA (ssh hassio@192.168.0.200), python3
# Pobiera: Solcast at_6, Solcast live, adjusted at_6, adjusted live,
#          PV/consumption/battery history, pogoda history, weather forecast z rana

set -e

DATE="${1:?Podaj datę, np: ./scripts/dump_day.sh 2026-04-09}"
CTX="$(dirname "$0")/../context/${DATE}"
HA="hassio@192.168.0.200"

TOKEN_CMD='export SUPERVISOR_TOKEN=$(grep -h SUPERVISOR_TOKEN /etc/profile.d/*.sh 2>/dev/null | grep -o "\"[^\"]*\"" | tr -d "\"")'
API="http://supervisor/core/api"

mkdir -p "$CTX"
echo "Zapisuję dane z $DATE do $CTX/"

# --- Sensory (stany aktualne) ---

echo "  solcast_at_6.json"
ssh "$HA" "$TOKEN_CMD; curl -s $API/states/sensor.solcast_forecast_at_6 -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/solcast_at_6.json"

echo "  solcast_live.json"
ssh "$HA" "$TOKEN_CMD; curl -s $API/states/sensor.solcast_pv_forecast_prognoza_na_dzisiaj -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/solcast_live.json"

echo "  adjusted_at_6.json"
ssh "$HA" "$TOKEN_CMD; curl -s $API/states/sensor.rce_weather_adjusted_pv_at_6 -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/adjusted_at_6.json"

echo "  adjusted_live.json"
ssh "$HA" "$TOKEN_CMD; curl -s $API/states/sensor.rce_weather_adjusted_pv_live -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/adjusted_live.json"

# --- Historia (PV, consumption, battery) ---

echo "  history (PV, consumption, battery)..."
ssh "$HA" "$TOKEN_CMD; curl -s '$API/history/period/${DATE}T00:00:00+02:00?end_time=${DATE}T23:59:59%2B02:00&filter_entity_id=sensor.total_pv_generation_bi_hourly,sensor.total_consumption_bi_hourly,sensor.battery_state_of_charge&minimal_response&no_attributes&significant_changes_only=false' -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/history_raw.json"

# --- Pogoda historia (zmiany stanów weather.wetteronline) ---

echo "  weather_history.json"
ssh "$HA" "$TOKEN_CMD; curl -s '$API/history/period/${DATE}T00:00:00+02:00?end_time=${DATE}T23:59:59%2B02:00&filter_entity_id=weather.wetteronline&minimal_response' -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\"" \
  > "$CTX/weather_history.json"

# --- Weather forecast z rana (zapisany przez weather_listener) ---

echo "  weather_forecast (szukam pliku z ~6:00 rano)..."
FORECAST_FILE=$(ssh "$HA" "ls /config/smart_rce/hourly_forecast_${DATE}T06* 2>/dev/null | head -1")
if [ -n "$FORECAST_FILE" ]; then
  ssh "$HA" "cat $FORECAST_FILE" > "$CTX/weather_forecast_at_0600.json"
  echo "    -> $(basename "$FORECAST_FILE")"
else
  echo "    -> BRAK pliku forecast z 6:xx rano"
fi

# --- Formatowanie JSONów ---

echo "  Formatowanie..."
for f in solcast_at_6.json solcast_live.json adjusted_at_6.json adjusted_live.json weather_history.json; do
  python3 -c "
import json
with open('$CTX/$f') as fh:
    d = json.load(fh)
with open('$CTX/$f', 'w') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
    fh.write('\n')
" 2>/dev/null && echo "    $f OK" || echo "    $f SKIP"
done

# --- Przetworzenie history_raw -> history_clean ---

echo "  history_clean.json"
python3 << 'PYEOF'
import json
from datetime import datetime, timedelta, timezone

CTX = "$CTX"
OFFSET = timezone(timedelta(hours=2))

with open(f"{CTX}/history_raw.json") as f:
    raw = json.load(f)

def to_windows_last(entries):
    windows = {}
    for e in entries:
        try:
            val = float(e['state'])
        except:
            continue
        ts = datetime.fromisoformat(e['last_changed']).astimezone(OFFSET)
        window = ts.replace(minute=ts.minute // 30 * 30, second=0, microsecond=0)
        key = window.isoformat()
        windows[key] = val
    return windows

pv = to_windows_last(raw[0])
cons = to_windows_last(raw[1])

bat_raw = {}
for e in raw[2]:
    try:
        val = float(e['state'])
    except:
        continue
    ts = datetime.fromisoformat(e['last_changed']).astimezone(OFFSET)
    bat_raw[ts.isoformat()] = val

clean = {
    "date": "$DATE",
    "pv_bi_hourly": [{"time": k, "rate_x2": round(v * 2, 2)} for k, v in sorted(pv.items())],
    "consumption_bi_hourly": [{"time": k, "rate_x2": round(v * 2, 2)} for k, v in sorted(cons.items())],
    "battery_soc": [{"time": k, "soc": v} for k, v in sorted(bat_raw.items())],
}

with open(f"{CTX}/history_clean.json", 'w') as f:
    json.dump(clean, f, indent=2, ensure_ascii=False)
    f.write('\n')

print(f"    pv: {len(clean['pv_bi_hourly'])} entries, cons: {len(clean['consumption_bi_hourly'])}, bat: {len(clean['battery_soc'])}")
PYEOF

# Usunięcie surowego pliku (mamy clean)
rm -f "$CTX/history_raw.json"

echo ""
echo "Gotowe! Pliki w $CTX/:"
ls -1 "$CTX/"
