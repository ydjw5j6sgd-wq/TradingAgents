#!/usr/bin/env python3
"""Morning brief for the Nasdaq 100 opening-range breakout, in German.

Reads 60 days of Yahoo 5-minute bars for ^NDX and the overnight Nasdaq 100
future (NQ=F), then prints:

- the market phase: trend against a rising or falling 20-day average,
  stress (5-day against 20-day ATR), where the price sits in its 20-day range
- the key levels: yesterday's high, low and close, the 5-day range, the ATR
  and the open the future points to
- a replay of the bot's rules (bots/capital_orb.py) on every past session:
  how a long-only and a short-only opening-range breakout did, in R
- a call: long, short, both or wait, and the reward:risk the rules allow,
  in points of a typical opening range

Data is free, delayed and unofficial. This is tooling, not financial advice.

  python3 bots/morning_brief.py
  python3 bots/morning_brief.py --json
"""

import argparse
import datetime
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

OR_BARS = 3          # 9:30, 9:35, 9:40
LAST_ENTRY_SLOT = 23  # the 11:25 bar, the last that closes by 11:30
LAST_SLOT = 76       # the 15:50 bar, closing at the 15:55 flat-by


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orb = _load("capital_orb", ROOT / "bots" / "capital_orb.py")


# ---------------------------------------------------------------- pure logic

def daily_bars(days):
    """{date: {slot: bar}} -> daily bars (oldest first) in capital_orb's shape."""
    out = []
    for day in sorted(days):
        bars = [days[day][s] for s in sorted(days[day])]
        out.append({"date": day, "open": bars[0]["o"], "high": max(b["h"] for b in bars),
                    "low": min(b["l"] for b in bars), "close": bars[-1]["c"]})
    return out


def opening_range(session):
    if not all(s in session for s in range(OR_BARS)):
        return None
    bars = [session[s] for s in range(OR_BARS)]
    return max(b["h"] for b in bars), min(b["l"] for b in bars)


def replay_session(session, side, buffer=0.05, min_rr=1.5, max_width=None):
    """One session under the bot's rules for one side.

    Returns None without a full opening range, else {"result": ...} where the
    result is "none" (no breakout), "wide" (range too wide), "skipped" (the
    first breakout was under min_rr), or a trade: "target", "stop" or "flat"
    with its R. A bar that touches both the stop and the target counts as the
    stop.
    """
    rng = opening_range(session)
    if rng is None:
        return None
    high, low = rng
    width = high - low
    if width <= 0:
        return None
    if max_width is not None and width > max_width:
        return {"result": "wide"}
    long = side == "long"
    mid = (high + low) / 2
    for slot in range(OR_BARS, LAST_ENTRY_SLOT + 1):
        bar = session.get(slot)
        if bar is None:
            continue
        if long and bar["c"] > high + buffer * width:
            entry, target = bar["c"], high + width
        elif not long and bar["c"] < low - buffer * width:
            entry, target = bar["c"], low - width
        else:
            continue
        risk = entry - mid if long else mid - entry
        rr = (target - entry) / risk if long else (entry - target) / risk
        if rr < min_rr:
            return {"result": "skipped", "rr": rr}
        last = entry
        for later in range(slot + 1, LAST_SLOT + 1):
            b = session.get(later)
            if b is None:
                continue
            if (long and b["l"] <= mid) or (not long and b["h"] >= mid):
                return {"result": "stop", "r": -1.0, "rr": rr}
            if (long and b["h"] >= target) or (not long and b["l"] <= target):
                return {"result": "target", "r": rr, "rr": rr}
            last = b["c"]
        r = (last - entry) / risk if long else (entry - last) / risk
        return {"result": "flat", "r": r, "rr": rr}
    return {"result": "none"}


def replay(days, daily, side, buffer=0.05, min_rr=1.5, max_or_atr=0.5):
    """The rules over every session with 20 earlier days for its ATR."""
    stats = {"days": 0, "none": 0, "wide": 0, "skipped": 0, "target": 0, "stop": 0, "flat": 0,
             "r": []}
    dates = [d["date"] for d in daily]
    for i, day in enumerate(dates):
        if i < 21:
            continue
        prior = daily[i - 21:i]
        ranges = [max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"]))
                  for p, b in zip(prior, prior[1:])]
        atr = sum(ranges) / len(ranges)
        out = replay_session(days[day], side, buffer, min_rr, max_or_atr * atr)
        if out is None:
            continue
        stats["days"] += 1
        stats[out["result"]] += 1
        if "r" in out:
            stats["r"].append(out["r"])
    trades = len(stats["r"])
    stats["trades"] = trades
    stats["win_rate"] = round(stats["target"] / trades, 2) if trades else None
    stats["avg_r"] = round(sum(stats["r"]) / trades, 2) if trades else None
    stats["total_r"] = round(sum(stats["r"]), 2)
    del stats["r"]
    return stats


def phase_of(regime, daily):
    last20 = daily[-20:]
    hi, lo = max(b["high"] for b in last20), min(b["low"] for b in last20)
    where = (regime["prev_close"] - lo) / (hi - lo) if hi > lo else 0.5
    trend = {"long": "Aufwärtstrend", "short": "Abwärtstrend", "neutral": "Seitwärtsphase"}[regime["bias"]]
    spot = "nahe dem 20-Tage-Hoch" if where > 0.8 else "nahe dem 20-Tage-Tief" if where < 0.2 else "mitten in der 20-Tage-Spanne"
    return {"trend": trend, "position": spot, "position_pct": round(where * 100)}


def recommend(regime, long_stats, short_stats):
    """(call, reason). Calls: LONG, SHORT, BEIDE, ABWARTEN."""
    good = lambda st: st["trades"] >= 3 and st["avg_r"] is not None and st["avg_r"] > 0
    if regime["stress"] == "hoch":
        return "ABWARTEN", "Stress hoch: Die Schwankungen sind doppelt so groß wie üblich."
    if regime["bias"] == "long":
        if good(long_stats):
            return "LONG", "Aufwärtstrend, und Long-Ausbrüche liefen zuletzt im Schnitt positiv."
        return "ABWARTEN", "Aufwärtstrend, aber Long-Ausbrüche liefen zuletzt nicht positiv."
    if regime["bias"] == "short":
        if good(short_stats):
            return "SHORT", "Abwärtstrend, und Short-Ausbrüche liefen zuletzt im Schnitt positiv."
        return "ABWARTEN", "Abwärtstrend, aber Short-Ausbrüche liefen zuletzt nicht positiv."
    if good(long_stats) and good(short_stats):
        return "BEIDE", "Seitwärtsphase: beide Richtungen, nur bei klarem Ausbruch."
    if good(long_stats):
        return "LONG", "Seitwärtsphase, aber nur Long-Ausbrüche liefen zuletzt positiv."
    if good(short_stats):
        return "SHORT", "Seitwärtsphase, aber nur Short-Ausbrüche liefen zuletzt positiv."
    return "ABWARTEN", "Seitwärtsphase, und keine Richtung lief zuletzt positiv."


def rr_window(width, buffer=0.05, min_rr=1.5):
    """Points for the earliest valid entry, and how far past the level an entry may still be."""
    risk, reward = (0.5 + buffer) * width, (1 - buffer) * width
    # reward:risk at x past the level is (1 - x) / (0.5 + x); solve for min_rr
    latest = (1 - 0.5 * min_rr) / (1 + min_rr) * width
    return {"risk": risk, "reward": reward, "rr": reward / risk,
            "buffer_pts": buffer * width, "latest_pts": latest}


def event_hints(day):
    """Recurring dates worth a look; the exact calendar comes from a search, not from here."""
    hints = []
    if day.weekday() == 4 and day.day <= 7:
        hints.append("Erster Freitag im Monat: meist US-Arbeitsmarktbericht (NFP) um 14:30 Uhr.")
    if day.weekday() == 3:
        hints.append("Donnerstag: wöchentliche Erstanträge auf Arbeitslosenhilfe um 14:30 Uhr.")
    return hints


# ---------------------------------------------------------------- data

def fetch(now_utc):
    tm = _load("timemachine", SKILLS / "trader-timemachine" / "timemachine.py")
    alerts = _load("alerts", SKILLS / "trader-alerts" / "alerts.py")
    result = tm.chart_result(tm.fetch_chart("^NDX"), "^NDX")
    _, bars, _ = tm.parse_bars(result, now_utc.replace(tzinfo=None))
    days = tm.split_sessions(bars)
    quotes, _ = alerts.get_quotes(["NQ=F"])
    return days, quotes.get("NQ=F")


def brief(days, future, today):
    full = {d: s for d, s in days.items() if d < today.isoformat()}
    daily = daily_bars(full)
    regime = orb.market_regime([{k: b[k] for k in ("high", "low", "close")} for b in daily])
    phase = phase_of(regime, daily)
    long_stats = replay(full, daily, "long")
    short_stats = replay(full, daily, "short")
    call, reason = recommend(regime, long_stats, short_stats)
    widths = [h - l for h, l in filter(None, (opening_range(full[d]) for d in sorted(full)[-20:]))]
    width = statistics.median(widths)
    prev = daily[-1]
    five = daily[-5:]
    est_open = None
    if future and future.get("prev_close"):
        change = future["price"] / future["prev_close"] - 1
        est_open = {"pct": round(change * 100, 2), "level": round(prev["close"] * (1 + change), 1)}
    open_de = datetime.datetime.combine(today, datetime.time(9, 30), tzinfo=NY).astimezone(BERLIN)
    return {"date": today.isoformat(), "or_window_de": f"{open_de:%H:%M}-{open_de + datetime.timedelta(minutes=15):%H:%M}",
            "last_entry_de": f"{open_de + datetime.timedelta(hours=2):%H:%M}",
            "regime": regime, "phase": phase, "call": call, "reason": reason,
            "levels": {"prev_close": prev["close"], "prev_high": prev["high"], "prev_low": prev["low"],
                       "five_day_high": max(b["high"] for b in five), "five_day_low": min(b["low"] for b in five),
                       "atr": regime["atr"], "est_open": est_open},
            "typical_or_width": width, "rr": rr_window(width),
            "replay": {"long": long_stats, "short": short_stats, "sessions": len(daily)},
            "events": event_hints(today)}


def german(b):
    f = lambda x: f"{x:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    r, lv, rr = b["regime"], b["levels"], b["rr"]
    lines = [
        f"NASDAQ 100 – Morgenanalyse für {b['date']}",
        "",
        f"EMPFEHLUNG: {b['call']} – {b['reason']}",
        "",
        "Marktphase",
        f"- {b['phase']['trend']}, Kurs {b['phase']['position']} ({b['phase']['position_pct']} % der Spanne)",
        f"- Stress {r['stress']} (ATR 5/20 = {str(r['stress_ratio']).replace('.', ',')}), "
        f"typische Tagesschwankung (ATR) {f(r['atr'])} Punkte",
        "",
        "Wichtige Marken",
        f"- Vortag: Schluss {f(lv['prev_close'])}, Hoch {f(lv['prev_high'])}, Tief {f(lv['prev_low'])}",
        f"- 5-Tage-Spanne: {f(lv['five_day_low'])} bis {f(lv['five_day_high'])}",
    ]
    if lv["est_open"]:
        lines.append(f"- Future über Nacht {lv['est_open']['pct']:+.2f} %".replace(".", ",")
                     + f" -> erwartete Eröffnung um {f(lv['est_open']['level'])}")
    lines += [
        "",
        f"Plan (Opening Range {b['or_window_de']} Uhr, Einstiege bis {b['last_entry_de']} Uhr)",
        f"- Typische Range-Breite: {f(b['typical_or_width'])} Punkte",
        f"- Gültiger Ausbruch: Schluss mindestens {f(rr['buffer_pts'])} Punkte jenseits der Range",
        f"- Stop Mitte der Range, Ziel Range-Höhe ab der Grenze: Risiko ~{f(rr['risk'])}, "
        f"Chance ~{f(rr['reward'])} Punkte, CRV {rr['rr']:.2f} : 1".replace(".", ","),
        f"- Liegt der Schluss mehr als {f(rr['latest_pts'])} Punkte jenseits der Grenze, "
        "fällt das CRV unter 1,5 : 1: dann nicht hinterherlaufen",
        "",
        f"Rückblick: die Regeln auf den letzten {b['replay']['sessions']} Handelstagen",
    ]
    for side, name in (("long", "Long"), ("short", "Short")):
        st = b["replay"][side]
        if st["trades"]:
            lines.append(f"- {name}: {st['trades']} Trades, {st['target']} Ziel, {st['stop']} Stop, "
                         f"{st['flat']} glattgestellt, Ø {st['avg_r']:+.2f} R".replace(".", ",")
                         + f" (zu spät/CRV zu klein: {st['skipped']}, Range zu breit: {st['wide']})")
        else:
            lines.append(f"- {name}: keine Trades")
    if b["events"]:
        lines += ["", "Termine"] + [f"- {e}" for e in b["events"]]
    lines += ["", "Kostenlose, verzögerte Yahoo-Daten. Werkzeug, keine Anlageberatung."]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    now = datetime.datetime.now(datetime.timezone.utc)
    days, future = fetch(now)
    b = brief(days, future, now.astimezone(NY).date())
    print(json.dumps(b, indent=2, ensure_ascii=False) if args.json else german(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
