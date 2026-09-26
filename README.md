# H5 Gold-Signal (Silver Bullet, Ziel 0,5 × Stopabstand)

Schickt werktags Push-Nachrichten aufs Handy (über [ntfy](https://ntfy.sh)) mit Einstieg, Stop und Ziel des
Silver-Bullet-Setups auf Gold. Läuft kostenlos über GitHub Actions, kein eigener Rechner nötig.

**Nur für ein Demokonto gedacht.** Das ist eine Forschungshypothese in der Forward-Beobachtung, keine Anlageberatung.

## Ablauf (deutsche Zeit, Normalfall)

| Uhrzeit | Nachricht |
|---|---|
| 16:00 | Spanne 15–16 Uhr (= 09–10 New York) steht: Hoch / Tief |
| 16:00–17:00 | bei einem Setup: `BUY LIMIT … \| SL … \| TP …` → Pending-Order mit SL und TP anlegen |
| sobald ausgelöst | „Order ausgelöst“, ggf. „andere Order LÖSCHEN“ |
| 17:00 | „Order löschen“, falls nicht ausgelöst |
| bis 18:00 | „Ziel/Stop erreicht“ oder um 18:00 „JETZT schließen“ |

In den Wochen, in denen Europa und die USA die Uhr zu unterschiedlichen Zeiten umstellen, verschiebt sich alles um eine
Stunde. Die Nachrichten nennen immer die richtige deutsche Uhrzeit. Kommt bis 16:05 keine „Spanne“-Nachricht, ist der
Lauf an dem Tag ausgefallen.

## Risikostatus

`risk_status.json` kommt aus der Risk Engine des Forschungs-Repositorys (wöchentlich nach dem Eintragen der tatsächlichen Trades):
Stufe (DEMO, LIVE_MICRO, …), Lotgröße, frei/pausiert und Grund — ohne Kontozahlen. Ist H5 pausiert (z. B. 6 Verluste in Folge
oder Verlustbudget ausgeschöpft), kommen Setups nur noch als „nur Modell, kein Trade“ mit niedriger Priorität. Die 16-Uhr-Nachricht
nennt immer den aktuellen Risikostatus und warnt, wenn er älter als 14 Tage ist.

## Regel

Spanne = Hoch/Tief 09:00–09:59 New York. Im Fenster 10:00–10:59 NY: Kurs unterschreitet das Tief (Long) bzw.
überschreitet das Hoch (Short), danach das erste Fair Value Gap (Long: Tief[k] > Hoch[k−2]); Limit-Order an Hoch[k−2]
(Short: Tief[k−2]), Stop = Extrem seit dem Durchbruch, Ziel = Einstieg ± 0,5 × Stopabstand. Beide Seiten laufen
parallel; die zuerst ausgelöste Order gilt. Kurse: öffentlicher Swissquote-Feed (XAU/USD Geldkurs), alle 4 Sekunden
abgefragt und zu 1-Minuten-Kerzen zusammengefasst. Die Niveaus weichen von deinem Broker um einige Cent ab.

## Einrichtung

1. App **ntfy** installieren (iOS/Android) → „+“ → Kanal abonnieren → den geheimen Kanalnamen eintragen.
2. Dieses Repository auf GitHub unter *Settings → Secrets and variables → Actions → New repository secret* mit dem
   Namen `NTFY_TOPIC` und dem Kanalnamen als Wert versehen.
3. *Actions* → „H5 Gold Signal“ → *Run workflow* (Häkchen „Nur eine Testnachricht senden“ gesetzt) → die Testnachricht
   muss aufs Handy kommen.

Jeder Lauf speichert seine Kerzen und Signale unter `logs/JJJJ-MM-TT.json`. Die täglichen Log-Commits halten außerdem
den Zeitplan aktiv (GitHub pausiert Zeitpläne in Repositories ohne Aktivität nach 60 Tagen).
