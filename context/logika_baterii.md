

Ale przydaloby sie w sumie cos takiego, ze w dnie niepracujace jednak zaczynami sterowac block_discharge. Choc w sumie to sie tez tyczy dni pracujacych.

Bo o co mi tutaj chodzi. W dzien roboczy strategia jest taka, ze najczesciej o godzinie 20 mamy wysokie ceny (powyzej 74 gr) i wtedy warto oddac energie do sieci. Wiec o godz. 13:00 automatyzacja Set Min SOC to 100 Afternoon ustawia DoD=0 o 13:00 - bo wtedy zaczynaja sie tanie godziny. Dzieki temu wszystko to co sie zakumuluje w baterii zostanie w niej do 19 - Wtedy ustawiamy DoD=90 bo zaczyna sie drogi prad. Nastepnie o godz. 20:00 nastepuje oddanie energii dzieki automatyzacji "(DATE) Battery Discharge at 18".

Natomiast jesli wieczorem nie mamy wysokiej ceny (powyzej 74 gr) ... i kolejnego dnia rano tez nie mamy wysokiej ceny (powyzej 74 gr) - a to bardzo czesto sie dzieje w dni wolne od pracy, jak dzis - to wówczas nie ma sensu trzymać DoD=0 do 19:00 ... i ponzniej oddawac w godzinie wieczornej. Wtedy dużo lepiej byloby robic cos takiego, ze od godz. 13:00 algorytm zachowuje sie podobnie (ale nieco inaczej) jak w post_charge, mianowicie jesli bilans PV w oknie 5 minutowy jest dodatni lub exported > 0 to trzymamy (lub ustawiamy) DoD=0 ... ale jesli ten bilans zaczyna byc ujemny I DODATKOWO nie mamy zadnej energii exported to wtedy nalezy przelaczyc na DoD=90.

Natomiast istotny tu jest jeszcze taki niuans, ze przy przejsciu z dnia wolnego w ktorym mozna pracowac w domu (sobota) na dzien w ktorym sie nie pracuje (niedziela) ... ktory dodatkowo bedzie bardzo sloneczny (i bedzie mial niskie ceny) ... to warto sie zastanowic, czy nie oddac energii do sieci w sobote wieczorem lub niedziele rano (zasadniczo wybieramy godzine z najwyzsza cena poczawszy od sobotnie popoludnia - np. 16:00 az do niedzielnego poranka) ... zeby wyzerowac do 10% baterie przed kolejnym slonecznym dniem (bo jak nie puszczamy pralek i zadnych pradozernych urzadzen to nie mamy jak zakumulowac energii i kolejnego dnia w ktorym jest slonecznie i sa niskie ceny nie mamy co zrobic z energia ... bo bateria juz pelna a do sieci oddajemy za zero zlotych)

Czyli w sumie to co napisalem powyzej tyczy sie sterowania block_discharge w dni robocze po 13:00, gdy zaczynaja sie niskie ceny (bo teraz tego nie robimy) oraz w sumie przez caly dzien w dni wolne od pracy ... choc wydaje mi sie, ze dla uproszczenia moglibysmy tez przyjac ze to robimy po 13:00 w dni wolne od pracy.

Tylko pewnie oprócz zmian w kodzie należałoby też zmienić automatyzacje jakoś sprytnie.
