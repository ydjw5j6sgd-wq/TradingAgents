#!/usr/bin/env python3
"""Morning brief for day trading the Nasdaq 100 (NAS100), in German.

Reads a year of ^NDX daily bars, about 60 days of 5-minute bars and the
overnight Nasdaq 100 future (NQ=F), then prints:

- the call: BUY, SELL, BOTH or WAIT, from the daily trend and stress
- the market phase: trend against a rising or falling 20-day average,
  stress (5-day against 20-day ATR), where the price sits in its 20-day range
- the key levels: yesterday's high, low and close, the 5-day range, the ATR
  and the open the future points to
- the plan for the VWAP pullback (bots/vwap_setup.py): signal window, typical
  stop and target in points, reward:risk, and a sizing example
- a replay of the setup on the last sessions, with and without trend filter
- chart context on 4-hour bars of the Nasdaq 100 future, moved to the index
  level: the 45 EMA, the swing structure (break of structure, change of
  character) and the nearest swing resistance and support, plus yesterday's
  volume point of control. Context only: as filters they did not improve
  the replay.

Data is free, delayed and unofficial. This is tooling, not financial advice.

  python3 bots/morning_brief.py
  python3 bots/morning_brief.py --json
"""

import argparse
import datetime
import importlib.util
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

_spec = importlib.util.spec_from_file_location("vwap_setup", ROOT / "bots" / "vwap_setup.py")
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)
orb = vs.orb


# ---------------------------------------------------------------- pure logic

def phase_of(regime, daily):
    last20 = daily[-20:]
    hi, lo = max(b["high"] for b in last20), min(b["low"] for b in last20)
    where = (regime["prev_close"] - lo) / (hi - lo) if hi > lo else 0.5
    trend = {"long": "Aufwärtstrend", "short": "Abwärtstrend", "neutral": "Seitwärtsphase"}[regime["bias"]]
    spot = ("nahe dem 20-Tage-Hoch" if where > 0.8 else "nahe dem 20-Tage-Tief" if where < 0.2
            else "mitten in der 20-Tage-Spanne")
    return {"trend": trend, "position": spot, "position_pct": round(where * 100)}


def recommend(regime, filtered):
    """(call, reason). Calls: BUY, SELL, BEIDE, ABWARTEN.

    The daily trend picks the side; the replay only adds a warning when that
    side lost money lately, since a few dozen trades decide nothing.
    """
    if regime["stress"] == "hoch":
        return "ABWARTEN", "Stress hoch: Die Schwankungen sind doppelt so groß wie üblich."
    call, reason, side_r = {
        "long": ("BUY", "Aufwärtstrend: nur Kauf-Signale, Rücksetzer an den VWAP kaufen.", "buy_avg_r"),
        "short": ("SELL", "Abwärtstrend: nur Verkauf-Signale, Anstiege an den VWAP verkaufen.", "sell_avg_r"),
        "neutral": ("BEIDE", "Seitwärtsphase: BUY und SELL am VWAP, mit kleinerer Größe.", "avg_r"),
    }[regime["bias"]]
    if filtered.get(side_r) is not None and filtered[side_r] < 0:
        reason += " Achtung: Diese Richtung lag zuletzt im Minus."
    if regime["stress"] == "erhöht":
        reason += " Stress erhöht: halbe Größe."
    return call, reason


def plan(day, median_risk, rr=vs.TARGET_R):
    start = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY)
    at = lambda slot: (start + datetime.timedelta(minutes=5 * (slot + 1))).astimezone(BERLIN).strftime("%H:%M")
    return {"open_de": start.astimezone(BERLIN).strftime("%H:%M"), "first_signal_de": at(vs.FIRST_SLOT),
            "last_signal_de": at(vs.LAST_ENTRY_SLOT), "flat_de": at(vs.FLAT_SLOT),
            "stop_pts": median_risk, "target_pts": rr * median_risk, "rr": rr,
            "size_per_10_eur": 10 / median_risk}


def event_hints(day):
    """Recurring dates worth a look; the exact calendar comes from a search, not from here."""
    hints = []
    if day.weekday() == 4 and day.day <= 7:
        hints.append("Erster Freitag im Monat: meist US-Arbeitsmarktbericht (NFP) um 14:30 Uhr.")
    if day.weekday() == 3:
        hints.append("Donnerstag: wöchentliche Erstanträge auf Arbeitslosenhilfe um 14:30 Uhr.")
    return hints


def ema(values, n=45):
    out, e = [], None
    for v in values:
        e = v if e is None else e + 2 / (n + 1) * (v - e)
        out.append(e)
    return out


def swings(bars, side=3):
    """Pivot highs and lows: (index, price) where a bar's high (low) tops (bottoms) side bars each way."""
    highs, lows = [], []
    for i in range(side, len(bars) - side):
        window = bars[i - side:i + side + 1]
        if bars[i]["h"] >= max(b["h"] for b in window):
            highs.append((i, bars[i]["h"]))
        if bars[i]["l"] <= min(b["l"] for b in window):
            lows.append((i, bars[i]["l"]))
    return highs, lows


def structure(bars, side=3):
    """Swing structure: bias from the last close beyond a confirmed swing, the level it broke
    (break of structure) and the opposite swing whose break would flip it (change of character)."""
    highs, lows = swings(bars, side)
    bias, broke = "neutral", None
    for i, b in enumerate(bars):
        h = [p for j, p in highs if j + side < i]
        l = [p for j, p in lows if j + side < i]
        # a new break: the first in a direction, or a further one beyond the last broken level
        if h and b["c"] > h[-1] and (bias != "long" or h[-1] > broke):
            bias, broke = "long", h[-1]
        if l and b["c"] < l[-1] and (bias != "short" or l[-1] < broke):
            bias, broke = "short", l[-1]
    flip = None
    if bias == "short" and highs:
        flip = highs[-1][1]
    elif bias == "long" and lows:
        flip = lows[-1][1]
    return {"bias": bias, "bos": broke, "choch": flip}


def nearest_levels(bars, price, side=3):
    """The closest swing high above the price and swing low below it."""
    highs, lows = swings(bars, side)
    above = [p for _, p in highs if p > price] + [p for _, p in lows if p > price]
    below = [p for _, p in lows if p < price] + [p for _, p in highs if p < price]
    return (min(above) if above else None), (max(below) if below else None)


def point_of_control(session, step=10):
    """The price bin (step points wide) with the most volume in a session of 5-minute bars."""
    profile = {}
    for b in session.values():
        level = round((b["h"] + b["l"] + b["c"]) / 3 / step) * step
        profile[level] = profile.get(level, 0) + (b.get("v") or 0)
    return max(profile, key=profile.get) if profile else None


def chart_context(h4, basis, prev_session):
    """4-hour EMA 45, structure and levels on the future, moved to the index by the basis."""
    if len(h4) < 60:
        return None
    shifted = [{k: (v - basis if k in "hlc" else v) for k, v in b.items()} for b in h4[-300:]]
    line = ema([b["c"] for b in shifted])
    price = shifted[-1]["c"]
    resist, support = nearest_levels(shifted[-120:], price)
    return {"price": price, "ema45": line[-1], "ema_rising": line[-1] > line[-4],
            **structure(shifted[-120:]), "resistance": resist, "support": support,
            "poc": point_of_control(prev_session) if prev_session else None}


def timeframes(sessions):
    """5-, 15- and 45-minute trend at the last close, and that session's volume against the
    average of the 20 before it."""
    if not sessions:
        return None
    order = sorted(sessions)
    last = sessions[order[-1]]
    total = lambda s: sum(b.get("v") or 0 for b in s.values())
    prior = [total(sessions[d]) for d in order[-21:-1]]
    avg = sum(prior) / len(prior) if prior else 0
    return {"5m": vs.trend_of([last[k]["c"] for k in sorted(last)]),
            "15m": vs.trend_of(vs.higher_bars(sessions, 3)),
            "45m": vs.trend_of(vs.higher_bars(sessions, 9)),
            "volume": total(last) / avg if avg else None}


def quality_record(sessions, biases, cost=2.0):
    """Average R (net of cost) of the live default's signals (both setups, no trend filter) by grade."""
    order = sorted(sessions)
    out = {}
    for i, d in enumerate(order):
        if len(sessions[d]) < 70:
            continue
        history = {x: sessions[x] for x in order[max(0, i - 25):i]}
        for t in vs.scan_session(sessions[d], biases.get(d, "neutral"), False, history=history):
            grade = vs.signal_context(history, d, sessions[d], t)["quality"]
            out.setdefault(grade, []).append(t["r"] - cost / t["risk"])
    return {g: {"trades": len(rs), "avg_r": round(sum(rs) / len(rs), 2)} for g, rs in out.items()}


def brief(days, yearly, future, today, h4=None, basis=0.0):
    past = [b for b in yearly if b["date"] < today.isoformat()]
    regime = orb.market_regime(past[-30:])
    sessions = {d: s for d, s in days.items() if d < today.isoformat()}
    biases = vs.bias_by_date(sorted(sessions), yearly)
    filtered = vs.backtest(sessions, biases, trend_filter=True)
    unfiltered = vs.backtest(sessions, biases, trend_filter=False)
    call, reason = recommend(regime, filtered)
    prev, five = past[-1], past[-5:]
    est_open = None
    if future and future.get("prev_close"):
        change = future["price"] / future["prev_close"] - 1
        est_open = {"pct": round(change * 100, 2), "level": round(prev["close"] * (1 + change), 1)}
    return {"date": today.isoformat(), "call": call, "reason": reason, "regime": regime,
            "phase": phase_of(regime, past),
            "levels": {"prev_close": prev["close"], "prev_high": prev["high"], "prev_low": prev["low"],
                       "five_day_high": max(b["high"] for b in five),
                       "five_day_low": min(b["low"] for b in five), "atr": regime["atr"], "est_open": est_open},
            "plan": plan(today, unfiltered["median_risk"] or filtered["median_risk"] or 25.0),
            "replay": {"with_filter": filtered, "without_filter": unfiltered},
            "events": event_hints(today),
            "timeframes": timeframes(sessions),
            "quality": quality_record(sessions, biases),
            "chart": chart_context(h4 or [], basis, sessions[max(sessions)] if sessions else None),
            "clusters": vs.volume_levels([sessions[d] for d in sorted(sessions)[-3:]]) if sessions else None}


def num(x, digits=1):
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def german(b):
    r, lv, pl = b["regime"], b["levels"], b["plan"]
    lines = [
        f"NASDAQ 100 – Morgenanalyse für {b['date']}",
        "",
        f"EMPFEHLUNG: {b['call']}",
        b["reason"],
        "",
        "Marktphase",
        f"- {b['phase']['trend']}, Kurs {b['phase']['position']} ({b['phase']['position_pct']} % der Spanne)",
        f"- Stress {r['stress']} (ATR 5/20 = {num(r['stress_ratio'], 2)}), "
        f"typische Tagesschwankung {num(r['atr'])} Punkte",
        "",
        "Wichtige Marken",
        f"- Vortag: Schluss {num(lv['prev_close'])}, Hoch {num(lv['prev_high'])}, Tief {num(lv['prev_low'])}",
        f"- 5-Tage-Spanne: {num(lv['five_day_low'])} bis {num(lv['five_day_high'])}",
    ]
    if lv["est_open"]:
        sign = "+" if lv["est_open"]["pct"] >= 0 else ""
        lines.append(f"- Future über Nacht {sign}{num(lv['est_open']['pct'], 2)} % -> erwartete Eröffnung "
                     f"um {num(lv['est_open']['level'])}")
    lines += [
        "",
        "Plan: VWAP-Pullback und Fehlausbruch, beide Richtungen",
        f"- US-Börse ab {pl['open_de']} Uhr, Signale von {pl['first_signal_de']} bis {pl['last_signal_de']} Uhr, "
        f"glattstellen bis {pl['flat_de']} Uhr, max. 4 Trades, Pause nach 2 Verlusten in Folge, "
        "Tageslimit -2 % (bei 0,5 % Risiko pro Trade)",
        f"- Typischer Stop ~{num(pl['stop_pts'], 0)} Punkte, Ziel ~{num(pl['target_pts'], 0)} Punkte, "
        f"CRV {num(pl['rr'])} : 1",
        f"- Bei +{num(vs.BE_AT * pl['stop_pts'], 0)} Punkten Stop auf Einstand, dann mit "
        f"~{num(vs.TRAIL * pl['stop_pts'], 0)} Punkten Abstand nachziehen (Push kommt)",
        f"- Größe: je 10 € Risiko etwa {num(pl['size_per_10_eur'], 2)} Kontrakte "
        "(bei 1 € pro Punkt; Hebel ändert das Risiko nicht)",
        "",
        "Rückblick (Spread 2 Punkte eingerechnet)",
    ]
    for key, name in (("without_filter", "Live-Standard, beide Richtungen"), ("with_filter", "Nur mit dem Trend")):
        st = b["replay"][key]
        if st["trades"]:
            lines.append(f"- {name}: {st['trades']} Trades in {st['sessions']} Tagen ({num(st['per_day'])}/Tag), "
                         f"Treffer {round(st['win_rate'] * 100)} %, Ø {'+' if st['avg_r'] >= 0 else ''}"
                         f"{num(st['avg_r'], 2)} R, längste Verlustserie {st['worst_streak']}")
        else:
            lines.append(f"- {name}: keine Trades")
    grades = b.get("quality") or {}
    if grades:
        parts = [f"{g} Ø {'+' if grades[g]['avg_r'] >= 0 else ''}{num(grades[g]['avg_r'], 2)} R "
                 f"({grades[g]['trades']})" for g in ("hoch", "mittel", "normal") if g in grades]
        lines.append("- Nach Qualität (Volumen ruhig = hoch): " + ", ".join(parts))
    tf = b.get("timeframes")
    if tf:
        words = {"long": "aufwärts", "short": "abwärts", "neutral": "seitwärts"}
        lines += ["", "Zeitebenen und Volumen (zum Vortagesschluss)",
                  f"- 5 Min {words[tf['5m']]}, 15 Min {words[tf['15m']]}, 45 Min {words[tf['45m']]}"]
        if tf["volume"] is not None:
            lines.append(f"- Vortagsvolumen {num(tf['volume'], 1)}x des 20-Tage-Schnitts"
                         + (" (lebhaft)" if tf["volume"] >= 1.2 else " (ruhig)" if tf["volume"] < 0.8 else ""))
        lines.append("- Live-Signale nennen 15/45 Min und Volumen mit; als Filter brachten die Zeitebenen "
                     "im Test keinen Vorteil, ruhiges Volumen schon.")
    ch = b.get("chart")
    if ch:
        trend = {"long": "bullisch", "short": "bärisch", "neutral": "ohne klare Richtung"}[ch["bias"]]
        lines += ["", "Chart-Kontext (4 Stunden, Future auf Index-Niveau umgerechnet)",
                  f"- EMA 45: {num(ch['ema45'])}, {'steigend' if ch['ema_rising'] else 'fallend'}; "
                  f"Kurs {num(ch['price'])} {'darüber' if ch['price'] > ch['ema45'] else 'darunter'}"]
        if ch["bos"] is not None:
            flip = (f"; Wechsel (CHoCH) erst bei Schluss {'über' if ch['bias'] == 'short' else 'unter'} "
                    f"{num(ch['choch'])}") if ch["choch"] is not None else ""
            lines.append(f"- Struktur {trend}: letzter Bruch (BOS) bei {num(ch['bos'])}{flip}")
        if ch["resistance"] is not None:
            lines.append(f"- Nächster Widerstand {num(ch['resistance'])}")
        if ch["support"] is not None:
            lines.append(f"- Nächste Unterstützung {num(ch['support'])}")
        if ch["poc"] is not None:
            lines.append(f"- Vortags-POC (meistes Volumen): {num(ch['poc'], 0)}")
    cl = b.get("clusters")
    if cl:
        lines += ["", "Volumen-Cluster der letzten 3 Tage (hier wurde am meisten gehandelt)",
                  f"- POC {num(cl['poc'], 0)}, Value Area {num(cl['val'], 0)} bis {num(cl['vah'], 0)}",
                  f"- Stärkste Cluster: {', '.join(num(n, 0) for n in cl['nodes'])}",
                  "- An Clustern treffen Käufer und Verkäufer aufeinander: oft Bremse für den Kurs. Ein Bruch "
                  "mit Volumen zeigt, wo eine Seite den Boden verliert; als eigenes Signal brachte das im Test "
                  "nichts, nutze die Cluster als Orientierung für Ziel und Stop."]
        lines.append("- Nur zur Orientierung: Als Filter brachten diese Werte im Test keinen Vorteil.")
    if b["events"]:
        lines += ["", "Termine"] + [f"- {e}" for e in b["events"]]
    lines += ["", "Kostenlose, verzögerte Yahoo-Daten. Werkzeug, keine Anlageberatung."]
    return "\n".join(lines)


# ---------------------------------------------------------------- data

def fetch_future_4h(yearly):
    """Complete 4-hour bars of the Nasdaq 100 future on a UTC grid, and its basis to the index:
    the future's close in the last hour before the index's last close, minus that close."""
    tm, _ = vs.tools()
    raw = json.loads(tm._get("https://query2.finance.yahoo.com/v8/finance/chart/NQ%3DF?range=6mo&interval=1h"))
    res = raw["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    hourly = [(t, q["open"][i], q["high"][i], q["low"][i], q["close"][i])
              for i, t in enumerate(res["timestamp"]) if None not in (q["high"][i], q["low"][i], q["close"][i])]
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    bars = {}
    for t, o, h, l, c in hourly:
        k = t - t % 14400
        b = bars.setdefault(k, {"t": k, "o": o, "h": h, "l": l, "c": c})
        b["h"], b["l"], b["c"] = max(b["h"], h), min(b["l"], l), c
    h4 = [bars[k] for k in sorted(bars) if k + 14400 <= now]
    last = yearly[-1]
    close_ts = datetime.datetime.combine(datetime.date.fromisoformat(last["date"]), datetime.time(15, 0),
                                         tzinfo=NY).timestamp()
    near = [c for t, o, h, l, c in hourly if t == close_ts]
    basis = near[0] - last["close"] if near else 0.0
    return h4, basis


def fetch(now_utc):
    alerts = vs._load("alerts", vs.SKILLS / "trader-alerts" / "alerts.py")
    quotes, _ = alerts.get_quotes(["NQ=F"])
    yearly = vs.fetch_yearly()
    past = [b for b in yearly if b["date"] < now_utc.astimezone(NY).date().isoformat()]
    try:
        h4, basis = fetch_future_4h(past)
    except Exception:  # the chart context is optional; the brief stands without it
        h4, basis = [], 0.0
    return vs.fetch_60d(now_utc), yearly, quotes.get("NQ=F"), h4, basis


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    now = datetime.datetime.now(datetime.timezone.utc)
    days, yearly, future, h4, basis = fetch(now)
    b = brief(days, yearly, future, now.astimezone(NY).date(), h4, basis)
    print(json.dumps(b, indent=2, ensure_ascii=False) if args.json else german(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
