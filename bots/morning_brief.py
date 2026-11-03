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


def plan(day, median_risk, rr=1.5):
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


def brief(days, yearly, future, today):
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
            "plan": plan(today, filtered["median_risk"] or unfiltered["median_risk"] or 25.0),
            "replay": {"with_filter": filtered, "without_filter": unfiltered},
            "events": event_hints(today)}


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
        "Plan: VWAP-Pullback",
        f"- US-Börse ab {pl['open_de']} Uhr, Signale von {pl['first_signal_de']} bis {pl['last_signal_de']} Uhr, "
        f"glattstellen bis {pl['flat_de']} Uhr, max. 3 Signale",
        f"- Typischer Stop ~{num(pl['stop_pts'], 0)} Punkte, Ziel ~{num(pl['target_pts'], 0)} Punkte, "
        f"CRV {num(pl['rr'])} : 1",
        f"- Größe: je 10 € Risiko etwa {num(pl['size_per_10_eur'], 2)} Kontrakte "
        "(bei 1 € pro Punkt; Hebel ändert das Risiko nicht)",
        "",
        "Rückblick (Spread 2 Punkte eingerechnet)",
    ]
    for key, name in (("with_filter", "Mit Trendfilter"), ("without_filter", "Ohne Trendfilter")):
        st = b["replay"][key]
        if st["trades"]:
            lines.append(f"- {name}: {st['trades']} Trades in {st['sessions']} Tagen ({num(st['per_day'])}/Tag), "
                         f"Treffer {round(st['win_rate'] * 100)} %, Ø {'+' if st['avg_r'] >= 0 else ''}"
                         f"{num(st['avg_r'], 2)} R, längste Verlustserie {st['worst_streak']}")
        else:
            lines.append(f"- {name}: keine Trades")
    if b["events"]:
        lines += ["", "Termine"] + [f"- {e}" for e in b["events"]]
    lines += ["", "Kostenlose, verzögerte Yahoo-Daten. Werkzeug, keine Anlageberatung."]
    return "\n".join(lines)


# ---------------------------------------------------------------- data

def fetch(now_utc):
    alerts = vs._load("alerts", vs.SKILLS / "trader-alerts" / "alerts.py")
    quotes, _ = alerts.get_quotes(["NQ=F"])
    return vs.fetch_60d(now_utc), vs.fetch_yearly(), quotes.get("NQ=F")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    now = datetime.datetime.now(datetime.timezone.utc)
    days, yearly, future = fetch(now)
    b = brief(days, yearly, future, now.astimezone(NY).date())
    print(json.dumps(b, indent=2, ensure_ascii=False) if args.json else german(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
