Punkt 1

Wczoraj o 22:45 zauwazylem, ze Sell Power nie dziala tak jakbym chcial poniewaz nie bierze pod uwage "grid side load".

To stwarza problem w automatyzacji Inverter grid export to avoid NEGATIVE balance - to wlasnie bylo widoczne wczoraj o 22:45

Byl pobor w kuchni (zmywarka) i Sell Power przez to wcale nie ustawil na meter oddania na poziomie 1,5 kW tylko bylo w okolicach 100-200 W.


Punkt 2

Wczoraj obserwowalismy, ze nagłe blokowanie ladowania baterii przy duzym PV powoduje, że produkcja PV spada do zera bo zatrzymuja sie grzalki, bateria nie moze ladowac. Jest duza podac energii a popyt drastycznie spada.



Te 2 punkty sklaniaja mnie do    takiego rozwiazania.

Zamiast automatyzacji "Inverter grid export to avoid NEGATIVE balance" oraz zamiast blokowania ladowania w battery.py lepiej jest w grid export manager dodac logike, ktora w sytuacji NEGATIVE balance (analogicznej do tych zdefiniowanej w automatyzacji) wykona:
- dostosowanie ladowania baterii na podstawie house_consumption_minus_heaters_minus_pv (tylko pytanie czy avg 1 minute czy 2 minutes) na podobnej zasadzie jak teraz to robi grid export manager ... czyli jesli chcemy wyjsc z negative balance to po prostu mozemy ladowac baterie mniej niz to co wynika z house_consumption_minus_heaters_minus_pv
  - tylko wlasnie pytanie co z grzalkami? najlepiej byloby tam zwiekszyc "reserved", czyli:

          elif battery_charge_limit > 7:
              if grid_export_charge_active:
                  reserved = 3500
              else:
                  reserved = 3500 if battery_soc < 50 else 2500
          elif battery_charge_limit > 2:
              if grid_export_charge_active:
                  reserved = 2000
              else:
                  reserved = 1000
          elif battery_charge_limit == 2:
              if grid_export_charge_active:
                  reserved = 600
               else
                  reserved = 300
          elif battery_charge_limit == 1:
              reserved = 300
         else:
               reserved = 0
