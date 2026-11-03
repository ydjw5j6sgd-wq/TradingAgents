# Capital.com Opening-Range-Bot (`capital_orb.py`)

Der Bot handelt einen Tag lang selbstständig den Ausbruch aus der Opening Range,
long oder short, je nach Marktlage. Standardmäßig handelt er den Nasdaq-100-CFD
(`US100`) bei Capital.com. Alle Kurse kommen direkt von Capital.com.

## So entscheidet der Bot

1. **Marktlage lesen** aus den Tageskerzen:
   - **Trend:** Liegt der letzte Schluss über einem steigenden 20-Tage-Durchschnitt,
     ist die Marktlage **long**. Liegt er unter einem fallenden, ist sie **short**.
     Sonst ist sie **neutral**.
   - **Stress:** Verglichen wird die Schwankungsbreite der letzten 5 Tage mit der
     der letzten 20 Tage (ATR 5/20).
     - unter 1,5: **normal**, volles Risiko
     - ab 1,5: **erhöht**, halbes Risiko
     - ab 2,0: **hoch**, heute kein Trade
2. **Opening Range** von 15:30 bis 15:45 Uhr deutscher Zeit (9:30 bis 9:45 Uhr New York).
   Ist sie breiter als die Hälfte der normalen Tagesschwankung (ATR), ist der Markt zu unruhig,
   und der Bot handelt heute nicht.
3. **Signal:** Die erste **geschlossene** Kerze außerhalb der Range löst den Trade aus.
   Ein Docht reicht nicht.
   - Bei Marktlage long nur **Kauf**, wenn eine Kerze über dem Hoch schließt.
   - Bei Marktlage short nur **Verkauf**, wenn eine Kerze unter dem Tief schließt.
   - Bei Marktlage neutral sind **beide Richtungen** erlaubt, aber der Schluss muss um
     10 % der Range-Breite jenseits der Grenze liegen.
4. **Chance prüfen:** Der Stop liegt auf der Mitte der Range, das Ziel bei 2R.
   Liegt das Ziel weiter als eine normale Tagesschwankung entfernt,
   lässt der Bot den Trade aus.
5. **Order:** Stop und Ziel gehen mit der Order an den Broker. Die Größe ergibt sich
   aus dem maximalen Verlust pro Trade.
6. Um 21:55 Uhr deutscher Zeit wird **glattgestellt**, wenn bis dahin weder Stop noch Ziel erreicht wurde.

Der Bot handelt höchstens einmal am Tag und eröffnet nach 21:30 Uhr keine neuen Positionen.
An Wochenenden und US-Feiertagen handelt er nicht, an verkürzten Handelstagen
verschiebt er die Zeiten nach vorn.

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
python3 bots/capital_orb.py regime           # Marktlage und heutige Range anzeigen, kein Handel
python3 bots/capital_orb.py run --dry-run    # nur beobachten und protokollieren, keine Order
python3 bots/capital_orb.py run              # handelt auf dem DEMOKONTO
python3 bots/capital_orb.py status           # Kontostand, offene Positionen, heutiger Stand
python3 bots/capital_orb.py flatten          # die Position des Bots sofort schließen
```

Mit diesen Einstellungen passt du das Verhalten an:

| Einstellung | Standard | Bedeutung |
| --- | --- | --- |
| `--side auto` | auto | `auto` folgt der Marktlage; `long`, `short` oder `both` erzwingen eine Richtung |
| `--risk 50` | 50 | max. Verlust pro Trade, in der Währung des Instruments (beim US100 USD) |
| `--stop mid` | mid | `mid` setzt den Stop auf die Mitte der Range, `edge` auf die gegenüberliegende Kante |
| `--rr 2` | 2 | Ziel als Vielfaches des Risikos |
| `--max-or-atr 0.5` | 0,5 | maximale Range-Breite im Verhältnis zur ATR |
| `--max-target-atr 1` | 1,0 | maximale Entfernung zum Ziel im Verhältnis zur ATR |
| `--stress-raised 1.5` / `--stress-high 2` | 1,5 / 2,0 | Schwellen für erhöhten und hohen Stress |

`run` startest du vor 15:45 Uhr deutscher Zeit. Der Bot wartet dann selbst,
handelt und beendet sich danach. Der Rechner muss die ganze Zeit laufen und darf nicht
in den Ruhezustand gehen, am besten ein kleiner Server oder ein Cloud-Dienst mit einem
täglichen Zeitplan, etwa montags bis freitags um 15:35 Uhr.

**Not-Aus:** Legst du die Datei `trader/STOP` an, gibt es ab sofort keine neuen
Einstiege mehr. Eine offene Position behält ihren Stop und ihr Ziel beim Broker.

## Echtgeld

Ohne weitere Angaben läuft der Bot immer auf dem Demokonto. Für das Echtgeldkonto
brauchst du **beides**: den Schalter `--live` und die Umgebungsvariable `CAPITAL_LIVE=yes`.
Lass den Bot vorher mehrere Wochen auf dem Demokonto laufen und vergleiche die Ergebnisse.

`--point-value` gibt an, wie viel Gewinn oder Verlust ein Punkt bei Größe 1 bringt.
Prüf diesen Wert bei Capital.com für dein Instrument, bevor du mit echtem Geld handelst.

## Risiken

- CFDs sind gehebelt. Bei Kurslücken oder Slippage kann der Verlust größer
  ausfallen als am Stop berechnet. Bei Short-Positionen gilt das genauso.
- Die Marktlage ist eine einfache Regel aus Trend und Schwankung, keine Vorhersage.
  Auch ein Signal in Trendrichtung kann verlieren.
- Verbindungsabbrüche oder API-Fehler werden protokolliert, und der Bot beendet sich.
  Eine offene Position bleibt mit Stop und Ziel beim Broker stehen.
- Das ist ein Werkzeug, keine Anlageberatung.
