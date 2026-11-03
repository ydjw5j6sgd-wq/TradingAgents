# Capital.com Opening-Range-Bot (`capital_orb.py`)

Handelt einen Tag lang selbstständig den Ausbruch aus der Opening Range,
standardmäßig auf dem Nasdaq-100-CFD (`US100`) bei Capital.com:

1. Die **Opening Range** bildet sich aus den 5-Minuten-Kerzen von 15:30 bis 15:45 Uhr
   deutscher Zeit (9:30 bis 9:45 Uhr New York). Die Kurse kommen direkt von Capital.com.
2. Die erste **geschlossene** Kerze über dem Hoch der Range löst den Kauf aus.
   Ein Docht über dem Hoch zählt nicht.
3. **Stop und Ziel gehen mit der Order an den Broker.** Der Stop liegt auf der Mitte
   der Range (`--stop low`: auf dem Tief), das Ziel bei 2R. Die Größe ergibt sich
   aus dem maximalen Verlust pro Trade (`--risk`).
4. Um 21:55 Uhr deutscher Zeit (15:55 Uhr New York) wird glattgestellt,
   wenn bis dahin weder Stop noch Ziel erreicht wurden.

Der Bot handelt nur long und höchstens einmal am Tag. Nach 21:30 Uhr eröffnet er
keine neuen Positionen. An Wochenenden und US-Feiertagen handelt er nicht, an verkürzten
Handelstagen verschiebt er die Zeiten entsprechend nach vorn.

## Einrichtung

1. **Den API-Schlüssel erstellst du bei Capital.com** unter Einstellungen > API-Integrationen.
   Das geht nur mit aktivierter Zwei-Faktor-Anmeldung. Dabei legst du ein eigenes
   Passwort für den Schlüssel fest.
2. **Die Zugangsdaten hinterlegst du als Umgebungsvariablen**, nie im Chat und nie in Dateien im Repo:
   ```
   CAPITAL_API_KEY=...
   CAPITAL_IDENTIFIER=deine-login-email
   CAPITAL_API_PASSWORD=das-passwort-des-api-schluessels
   ```
3. Der Bot braucht **Python 3.9 oder neuer**, aber keine weiteren Pakete.

## Benutzung

```bash
python3 bots/capital_orb.py run --dry-run    # nur beobachten und protokollieren, keine Order
python3 bots/capital_orb.py run              # handelt auf dem DEMOKONTO
python3 bots/capital_orb.py run --risk 30    # max. 30 (Währung des Instruments, beim US100 USD) Verlust pro Trade
python3 bots/capital_orb.py status           # Kontostand, offene Positionen, heutiger Stand
python3 bots/capital_orb.py flatten          # die Position des Bots sofort schließen
```

`run` startest du vor 15:45 Uhr deutscher Zeit. Der Bot wartet dann selbst,
handelt und beendet sich danach. Der Rechner muss die ganze Zeit laufen und darf
nicht in den Ruhezustand gehen, am besten ein kleiner Server oder ein
Cloud-Dienst mit einem täglichen Zeitplan, etwa montags bis freitags um 15:35 Uhr.

**Not-Aus:** Legst du die Datei `trader/STOP` an, gibt es ab sofort keine
neuen Einstiege mehr. Eine offene Position behält ihren Stop und ihr Ziel beim Broker.

## Echtgeld

Ohne weitere Angaben läuft der Bot immer auf dem Demokonto. Für das Echtgeldkonto
brauchst du **beides**: den Schalter `--live` und die Umgebungsvariable `CAPITAL_LIVE=yes`.
Lass den Bot vorher mehrere Wochen auf dem Demokonto laufen und vergleiche die Ergebnisse.

`--point-value` gibt an, wie viel Gewinn oder Verlust ein Punkt bei Größe 1 bringt.
Prüf diesen Wert bei Capital.com für dein Instrument, bevor du mit echtem Geld handelst.

## Risiken

- CFDs sind gehebelt. Bei Kurslücken oder Slippage kann der Verlust größer
  ausfallen als am Stop berechnet.
- Der Bot führt feste Regeln aus, er denkt nicht mit. Ein Ausbruchssignal
  ist keine Gewinngarantie.
- Verbindungsabbrüche oder API-Fehler werden protokolliert, und der Bot beendet sich. Eine
  offene Position bleibt mit Stop und Ziel beim Broker stehen.
- Das ist ein Werkzeug, keine Anlageberatung.
