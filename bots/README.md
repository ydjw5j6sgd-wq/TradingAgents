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
3. **Signal:** Die erste **vollständige** Kerze, die **klar** außerhalb der Range schließt,
   löst den Trade aus. Ein Docht reicht nicht.
   - **Klar** heißt: mindestens 5 % der Range-Breite jenseits der Grenze. Ein Schluss
     1 Punkt über dem Hoch zählt nicht.
   - **Vollständig** heißt: Capital.com hat schon die nächste Kerze begonnen. So kann
     eine halb fertige Kerze kein Fehlsignal auslösen.
   - Bei Marktlage long nur **Kauf** über dem Hoch, bei Marktlage short nur **Verkauf**
     unter dem Tief, bei Marktlage neutral beide Richtungen.
   - Einstiege nur bis **17:30 Uhr** deutscher Zeit (11:30 Uhr New York). Danach pendelt
     der Markt oft nur noch hin und her.
4. **Chance prüfen:** Der Stop liegt auf der Mitte der Range. Das Ziel ist die Range-Höhe,
   abgetragen ab der gebrochenen Grenze. Liegt das Chance-Risiko-Verhältnis zum
   tatsächlichen Kurs unter **1,5 : 1**, war der Ausbruch schon zu weit gelaufen, und der
   Bot läuft nicht hinterher. Liegt das Ziel weiter als eine normale Tagesschwankung
   entfernt, lässt er den Trade ebenfalls aus.
5. **Order:** Stop und Ziel gehen mit der Order an den Broker. Die Größe ergibt sich
   aus dem maximalen Verlust pro Trade.
6. Um 21:55 Uhr deutscher Zeit wird **glattgestellt**, wenn bis dahin weder Stop noch Ziel erreicht wurde.

Der Bot handelt höchstens einmal am Tag.
An Wochenenden und US-Feiertagen handelt er nicht, an verkürzten Handelstagen
verschiebt er die Zeiten nach vorn.

## Mit Agenten-Desk (`--agents`)

`bots/agent_desk.py` lässt vor Börsenstart das ganze TradingAgents-Team über den
Nasdaq 100 beraten. Weil das Framework mit Aktien und ETFs arbeitet, nimmt es dafür
den ETF `QQQ`. Das Team besteht aus:

- **Analysten** für Fundamentaldaten, Stimmung (StockTwits, Reddit), Nachrichten und Charttechnik
- **Bull- und Bear-Researchern**, die gegeneinander argumentieren
- **Trader, Risiko-Team und Portfolio Manager**, die am Ende entscheiden

Die Entscheidung des Portfolio Managers wird gespeichert und gibt die Richtung des Tages vor:

| Rating | Richtung für den Bot |
| --- | --- |
| Buy, Overweight | nur long |
| Underweight, Sell | nur short |
| Hold oder kein lesbares Rating | kein Trade |

Mit `--agents` handelt der Bot nur in die Richtung, in die **Marktlage und Agenten-Desk
gemeinsam** zeigen. Widersprechen sie sich, steht Hold, oder fehlt die Einschätzung von heute,
handelt er nicht.

**Täglicher Ablauf** (deutsche Zeit, montags bis freitags):

```bash
# ca. 14:00 Uhr: Agenten-Desk (dauert einige Minuten, kostet je nach Modell etwa 1-2 USD)
python3 bots/agent_desk.py
# ca. 15:35 Uhr: Bot mit Agenten-Filter
python3 bots/capital_orb.py run --agents
```

`python3 bots/agent_desk.py --show` zeigt die gespeicherte Entscheidung samt Begründung.

Für den Desk musst du TradingAgents installieren (`pip install .` im Repo) und einen KI-Anbieter
einstellen, zum Beispiel Claude:

```
TRADINGAGENTS_LLM_PROVIDER=anthropic
TRADINGAGENTS_DEEP_THINK_LLM=claude-opus-5
TRADINGAGENTS_QUICK_THINK_LLM=claude-fable-5
ANTHROPIC_API_KEY=...
```

Die Modellnamen kannst du anpassen. Die Hauptanleitung des Repos (`README.md`) listet die übrigen Anbieter.

## NAS100 Day-Trading: Morgenanalyse und Live-Signale

Für den täglichen Handel im Nasdaq 100 gibt es ein eigenes Setup, den **VWAP-Pullback**
(`vwap_setup.py`). Grundlage ist der VWAP, der volumengewichtete Durchschnittskurs seit Börsenstart:

- **BUY:** Der VWAP steigt (höher als vor 30 Minuten), die vorige Kerze schloss über ihm,
  und die aktuelle Kerze taucht bis an den VWAP ab, schließt aber wieder darüber.
  Der Stop liegt knapp unter dem Tief dieser Kerze.
- **SELL:** spiegelbildlich unter einem fallenden VWAP.
- **Ziel:** 2-mal das Risiko. Stops unter 15 Punkten werden ausgelassen, die frisst der Spread.
- **Stop nachziehen:** Liegt der Trade 0,5R im Plus, kommt ein Push. Dann den Stop auf Einstand
  (+0,1R) setzen und danach mit 0,5R Abstand nachziehen, am einfachsten als Trailing-Stop bei Capital.com.
  So wird aus einem Trade, der erst steigt und dann dreht, ein kleiner Gewinn statt eines vollen Verlusts.
- Signale kommen von 16:10 bis 21:05 Uhr deutscher Zeit, immer nur eine offene Position,
  höchstens 6 pro Tag. Nach 3 Verlusten ist für den Tag Pause. Glattgestellt wird bis 21:55 Uhr.
- **Vor 16:10 Uhr gibt es keine Signale.** Auf dem Nasdaq-100-Future verlor der VWAP-Pullback in
  der europäischen Sitzung (9:00 bis 15:25 Uhr) in jeder getesteten Variante (Ø −0,17 bis −0,42 R).
  Eine Rückkehr zum VWAP und der Ausbruch aus der Nacht-Range lagen bei etwa null.
- **Zweites Setup: Fehlausbruch.** Eine Kerze sticht über das Hoch (unter das Tief) der letzten Stunde,
  bei mindestens 1,2-fachem Volumen für diese Uhrzeit, schließt aber wieder darunter (darüber) und gegen
  ihre Eröffnung. Die Käufer (Verkäufer), die den Ausbruch gekauft haben, verlieren den Boden: SELL (BUY),
  Stop knapp hinter der Kerze, gleiches Nachziehen.
- **Beide Richtungen (Standard).** Jedes Signal sagt, ob es mit oder gegen den Tagestrend läuft.
  Mit `--trend-filter` kommen nur Signale in Trendrichtung.
- **Schnell:** Ein offenes Signal wird auf 1-Minuten-Kerzen verfolgt, und alle 20 Sekunden wird geprüft.
  Der Push kommt so etwa eine halbe Minute nach Kerzenschluss. Yahoo-Daten sind kostenlos und können
  sich verzögern.
- **Volumen-Cluster:** Jedes Signal nennt den nächsten Cluster darüber und darunter (Volume Profile
  der letzten 3 Tage und des laufenden Tages). Dort treffen Käufer und Verkäufer aufeinander.

Im Rückblick über 59 Handelstage, mit 2 Punkten Spread pro Trade:

| | Trades/Tag | Treffer | Ø pro Trade | längste Verlustserie |
| --- | --- | --- | --- |
| **VWAP + Fehlausbruch, beide Richtungen (Standard)** | 1,9 | 66 % | +0,17 R | 4 |
| VWAP + Fehlausbruch, nur mit dem Trend | 1,1 | 74 % | +0,31 R | 2 |
| nur VWAP, mit Trendfilter, Stop nachziehen | 0,7 | 74 % | +0,29 R | 2 |
| nur VWAP, ohne Trendfilter, Stop nachziehen | 1,2 | 67 % | +0,23 R | 2 |
| zum Vergleich: Stop bleibt, Ziel 1,5R, mit Trendfilter | 0,6 | 53 % | +0,25 R | 5 |

Die Regel zum Nachziehen wurde auf der ersten Hälfte des Zeitraums gewählt und auf der zweiten
geprüft: Die Trefferquote lag dort bei 68 % bzw. 82 %. Rund ein Drittel der Gewinner sind
allerdings Mini-Gewinne: Der Kurs drehte, nachdem der Stop schon auf Einstand stand.

Getestet und verworfen: der Bruch eines Volumen-Clusters mit Volumen (−0,03 R, instabil) und die
Rückkehr in die Value Area des Vortags (−0,48 R). Der Fehlausbruch allein lag bei +0,05 R; zusammen
mit dem VWAP-Pullback bringt er mehr Trades und mehr Summe (+18,7 R statt +15,9 R).

Zum Vergleich lag der Opening-Range-Ausbruch des Capital.com-Bots über denselben Zeitraum bei −0,26 R.
Beide VWAP-Varianten waren in der ersten Hälfte des Zeitraums bei null oder im Minus.
Garantiert ist also nichts.

```bash
python3 bots/morning_brief.py                       # Morgenanalyse: BUY, SELL, BEIDE oder ABWARTEN
python3 bots/live_signals.py next                   # wartet aufs nächste Ereignis (beide Richtungen)
python3 bots/live_signals.py next --trend-filter    # nur Signale in Trendrichtung
python3 bots/live_signals.py status                 # Signale von heute
```

`live_signals.py next` endet beim nächsten Ereignis: `SIGNAL` (BUY oder SELL mit Einstieg,
Stop, Ziel und CRV), `CLOSED` (Stop, Ziel oder Glattstellen) oder `END` (Schluss für heute).
Eine Kerze zählt erst, wenn Yahoos Daten über ihr Ende hinausreichen. Ein Fehlsignal
durch eine halb fertige Kerze ist so ausgeschlossen. Die Kurse sind die des Index:
Beim CFD übernimmst du Stop und Ziel als Abstand in Punkten.

**Zeitebenen und Volumen:** Jedes Signal nennt den Trend auf 15 und 45 Minuten sowie das
Volumen der Signalkerze und der ersten halben Stunde, verglichen mit dem Üblichen zur gleichen
Uhrzeit. Daraus ergibt sich eine **Qualitätsstufe**:

| Qualität | Bedeutung | im Rückblick |
| --- | --- | --- |
| HOCH | Signalkerze und Eröffnung auf ruhigem Volumen | Ø +0,52 R |
| MITTEL | eins von beiden ruhig | Ø +0,09 R |
| NORMAL | beides lebhaft | Ø +0,05 R |

Als Filter brachten 15 und 45 Minuten im Test keinen Vorteil (45 Minuten: Ø +0,06 R statt +0,25 R).
Deshalb stehen sie nur als Info im Signal. Die Qualitätsstufe filtert ebenfalls nicht, damit keine
Trades verloren gehen. Bei MITTEL und NORMAL kann man aber kleiner handeln.

**Hebel und Größe:** Der Hebel (zum Beispiel 1:20) legt nur die Margin fest, nicht das Risiko.
Das Risiko bestimmt die Größe: Bei 1 % Risiko pro Trade und einem typischen Stop von 27 Punkten
sind es je 10 € Risiko etwa 0,37 Kontrakte, wenn ein Punkt bei Größe 1 einen Euro wert ist.
Im Rückblick gab es bis zu 7 Verluste in Folge.

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
| `--target measured` | measured | `measured`: Range-Höhe ab der Grenze; `r`: Vielfaches des Risikos (`--rr`) |
| `--min-rr 1.5` | 1,5 | Mindest-Chance-Risiko-Verhältnis zum tatsächlichen Kurs |
| `--buffer 0.05` | 0,05 | so weit (Anteil der Range-Breite) muss der Schluss jenseits der Grenze liegen |
| `--no-entries-after 11:30` | 11:30 | letzter Einstieg, New-York-Zeit (17:30 Uhr deutscher Zeit) |
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
