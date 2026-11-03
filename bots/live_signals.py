#!/usr/bin/env python3
"""Live BUY/SELL signals for NAS100 day trading, in German, under the rulebook in bots/REGELWERK.md.

Two setups on 5-minute bars (bots/vwap_setup.py): the VWAP pullback and the
failed breakout (a sweep of the last hour's high or low on heavy volume that
closes back inside). Every signal is scored 0-100 on the rulebook's
multi-timeframe check (4H, 2H, 1H, 15M, 5M; the higher timeframe weighs
more), volume and the daily trend; under 40 it is NO TRADE and nothing is
sent. Open signals are followed on 1-minute bars, and the loop looks every
20 seconds, so a push goes out within about half a minute of a bar's close
(Yahoo's data is free and can lag).

`next` watches today's ^NDX bars and returns at the next event, printing it
and keeping the day in trader/state/live_signals.json:

  SIGNAL   a complete bar gave a BUY or SELL, in the rulebook's format
  MOVE     the open signal is +0.5 R: move the stop to the entry and trail it
  CLOSED   the open signal reached its stop, its trailed stop or its target,
           or the 15:55 New York flat-by
  PAUSE    two losses in a row: no new signals for --pause-minutes
  END      no more entries today (time, daily loss limit, max trades, closed day)
  WAIT     --max-minutes ran out with nothing new: just run it again

Exit code 0 for every event, 3 for WAIT. A bar only counts once Yahoo's data
reaches past its end, so a half-finished bar never signals. One signal is open
at a time. No new signals --blackout-minutes around a --blackout news time.
Nothing is ever placed.

  python3 bots/live_signals.py next                               # both directions (default)
  python3 bots/live_signals.py next --trend-filter                # only the daily trend's side
  python3 bots/live_signals.py next --blackout 14:30 --blackout 20:00
  python3 bots/live_signals.py next --account 2000 --risk-pct 1 --eur-per-point 0.86
  python3 bots/live_signals.py status
"""

import argparse
import datetime
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "trader" / "state" / "live_signals.json"
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

_spec = importlib.util.spec_from_file_location("vwap_setup", ROOT / "bots" / "vwap_setup.py")
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)

RULES = {
    "trend_filter": False,    # both directions; True keeps the daily trend's side
    "rr": vs.TARGET_R,        # the order's target (TP2) in R
    "min_risk": 15.0,         # skip stops under this many points: the spread would eat them
    "min_score": vs.MIN_SCORE,
    "max_trades": 6,
    "pause_after": 2,         # losses in a row
    "pause_minutes": 60,
    "risk_pct": 1.0,          # account risk per trade (rulebook: 0.5 to 1)
    "day_loss_pct": 2.0,      # rulebook: stop for the day at -2% of the account
    "blackouts": [],          # German clock times of news, "HH:MM"
    "blackout_minutes": 15,
    "account": None,          # account in EUR, for the position size
    "eur_per_point": None,    # EUR per index point for size 1 of the CFD
}
TRENDS = {"long": "aufwärts", "short": "abwärts", "neutral": "seitwärts"}


def de_time(day, slot, end=False):
    """A slot's start (or end) as a German clock time."""
    t = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY) + datetime.timedelta(
        minutes=5 * (slot + (1 if end else 0)))
    return t.astimezone(BERLIN).strftime("%H:%M")


def num(x):
    return f"{x:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


def minute_de(ts):
    return datetime.datetime.fromtimestamp(ts + 60, BERLIN).strftime("%H:%M")


# ---------------------------------------------------------------- messages

def setup_text(sig):
    if sig.get("setup") == "sweep":
        broke = "Hoch" if sig["side"] == "SELL" else "Tief"
        who = "Käufer" if sig["side"] == "SELL" else "Verkäufer"
        return (f"Fehlausbruch (Liquidity Sweep): Stunden-{broke} {num(sig['level'])} kurz gebrochen, "
                f"Schluss zurück, Volumen {num(sig['volume'])}x üblich: die {who} verlieren den Boden")
    return f"VWAP-Pullback: Rücksetzer an den VWAP {num(sig['vwap'])} mit Schluss in Trendrichtung des VWAP"


def size_text(sig, rules):
    if not rules.get("account") or not rules.get("eur_per_point"):
        return "fehlt: Kontostand und Kontraktwert pro Punkt angeben, dann rechne ich sie aus"
    risk_eur = rules["account"] * rules["risk_pct"] / 100
    size = math.floor(risk_eur / (sig["risk"] * rules["eur_per_point"]) * 100) / 100
    return (f"{size:.2f}".replace(".", ",") + f" (Risiko {num(risk_eur)} € = {num(rules['risk_pct'])} % "
            f"bei {num(sig['risk'])} Pkt Stop)")


def signal_line(day, sig, bias, rules=None):
    """The signal in the rulebook's output format."""
    rules = {**RULES, **(rules or {})}
    long = sig["side"] == "BUY"
    sign = 1 if long else -1
    r = sig["risk"]
    ctx = sig.get("context") or {}
    trends = sig.get("trends") or {}
    want = "long" if long else "short"
    agree = sum(1 for t in trends.values() if t == want)
    oppose = sum(1 for t in trends.values() if t not in ("neutral", want))
    with_trend = {"long": "BUY", "short": "SELL"}.get(bias)
    trend_note = ("" if with_trend is None else " (mit dem Tagestrend)" if with_trend == sig["side"]
                  else " (gegen den Tagestrend)")
    worst = sig["entry"] + sign * 0.15 * r
    lo, hi = sorted((sig["entry"], worst))
    above, below = sig.get("clusters") or (None, None)
    if sig.get("setup") == "sweep":
        stop_why = f"{'über dem Hoch' if not long else 'unter dem Tief'} der Sweep-Kerze"
        invalid = (f"5M-Schluss wieder {'unter' if long else 'über'} {num(sig['level'])} "
                   f"zurück auf die Ausbruchsseite, oder Stop {num(sig['stop'])}")
    else:
        stop_why = f"knapp {'unter dem Tief' if long else 'über dem Hoch'} der Signalkerze"
        invalid = (f"5M-Schluss {'unter' if long else 'über'} dem VWAP {num(sig['vwap'])}, "
                   f"oder Stop {num(sig['stop'])}")
    vol = lambda x: f"{num(x)}x" if x is not None else "?"
    reasons = [setup_text(sig)]
    if trends:
        reasons.append(f"Zeitebenen: {agree} von 5 mit dem Signal, {oppose} dagegen{trend_note}")
    if ctx:
        reasons.append(f"Volumen: Signalkerze {vol(ctx.get('rel_volume'))} üblich, Eröffnung "
                       f"{vol(ctx.get('open_volume'))}, Stufe {ctx.get('quality', '?').upper()}")
    if above or below:
        parts = [f"darüber {num(above)}" if above else None, f"darunter {num(below)}" if below else None]
        reasons.append("Volumen-Cluster " + ", ".join(p for p in parts if p))
    reasons.append(f"Management: bei +{num(vs.BE_AT * r)} Pkt Stop auf Einstand, dann "
                   f"{num(vs.TRAIL * r)} Pkt nachziehen (Push kommt)"
                   + ("; Achtung: große Impulskerze (FOMO-Regel)" if sig.get("fomo") else ""))
    lines = [
        f"{'BUY (long)' if long else 'SELL (short)'} NAS100 – {vs.SETUP_NAMES.get(sig.get('setup'), 'Setup')}, "
        f"Qualität {sig.get('score', '?')}/100",
        f"MARKTRICHTUNG: {'Long' if long else 'Short'}",
        "TREND:",
        *[f"{name}: {TRENDS[trends[name]]}" for name in vs.TIMEFRAMES if name in trends],
        f"SETUP: Bestätigt (5M-Kerzenschluss {de_time(day, sig['slot'], end=True)} Uhr)",
        f"EINSTIEG: {num(lo)} bis {num(hi)} (Signal {num(sig['entry'])}; weiter weg: NO TRADE, auf Rücklauf warten)",
        f"STOP-LOSS: {num(sig['stop'])} ({'-' if long else '+'}{num(r)} Pkt), {stop_why}",
        "TAKE-PROFIT:",
        f"TP1: {num(sig['entry'] + sign * r)} (1R, +{num(r)} Pkt, optional Teilgewinn)",
        f"TP2: {num(sig['target'])} ({num(sig['rr'])}R, +{num(sig['rr'] * r)} Pkt, Ziel der Order)",
        f"TP3: {num(sig['entry'] + sign * 3 * r)} (3R, +{num(3 * r)} Pkt, nur mit nachgezogenem Stop)",
        f"CHANCE-RISIKO: 1:{num(sig['rr'])} (TP2)",
        f"INVALIDIERUNG: {invalid}",
        f"SETUP-QUALITÄT: {sig.get('score', '?')}/100",
        "BEGRÜNDUNG:",
        *[f"- {x}" for x in reasons[:5]],
        f"POSITIONSGRÖSSE: {size_text(sig, rules)}",
        "DATEN: Yahoo ^NDX (Index der US-Sitzung, kostenlos, kann verzögert sein), Trends berechnet "
        "(EMA 20 je Zeiteinheit); beim CFD Stop und Ziel als Abstand in Punkten übernehmen. Nichts platziert.",
    ]
    return "\n".join(lines)


def move_line(day, sig, stop):
    ahead = abs(stop - sig["entry"])
    return (f"STOP NACHZIEHEN – {sig['side']} von {de_time(day, sig['slot'], end=True)} Uhr war mindestens "
            f"{num(vs.BE_AT * sig['risk'])} Pkt im Plus. Stop jetzt auf {num(stop)} ({num(ahead)} Pkt über dem "
            f"Einstieg statt Verlust), dann mit {num(vs.TRAIL * sig['risk'])} Pkt Abstand nachziehen "
            f"(Trailing-Stop {num(vs.TRAIL * sig['risk'])} Pkt). Ist der Kurs schon am neuen Stop, "
            f"Position schließen. Ziel bleibt {num(sig['target'])}.")


def closed_line(day, sig, result, r, when):
    what = {"stop": "STOP erreicht", "trail": "nachgezogener Stop erreicht", "target": "ZIEL erreicht",
            "flat": f"glattgestellt ({de_time(day, vs.FLAT_SLOT, end=True)})"}[result]
    return (f"{sig['side']} von {de_time(day, sig['slot'], end=True)} Uhr: {what} um {when} Uhr, "
            + f"{r:+.2f}".replace(".", ",") + " R.")


# ---------------------------------------------------------------- the day

def load_state(day):
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("date") != day.isoformat():
        state = {"date": day.isoformat(), "bias": None, "seen": None, "open": None, "trades": [],
                 "skipped": [], "pause_until": None, "paused_at": None}
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def blocked(day, slot, rules):
    """The news time a bar's close falls near, or None."""
    end = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY) + datetime.timedelta(
        minutes=5 * (slot + 1))
    for hhmm in rules["blackouts"]:
        h, m = (int(x) for x in hhmm.split(":"))
        news = datetime.datetime.combine(day, datetime.time(h, m), tzinfo=BERLIN)
        if abs((end - news).total_seconds()) <= rules["blackout_minutes"] * 60:
            return hhmm
    return None


def follow_open(state, session, complete, day, minutes):
    """Advance the open signal. Returns an event or None."""
    sig = state["open"]
    open_ts = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY).timestamp()
    when = None
    if minutes is not None:
        start = open_ts + (sig["slot"] + 1) * 300
        flat_ts = open_ts + (vs.FLAT_SLOT + 1) * 300
        f = vs.walk(((t, b) for t, b in minutes if start <= t < flat_ts), sig, vs.BE_AT, vs.TRAIL)
        if f["result"]:
            f["slot"], when = int((f["key"] - open_ts) // 300), minute_de(f["key"])
        elif complete >= vs.FLAT_SLOT and f["last"]:
            move = f["last"][1] - sig["entry"] if sig["side"] == "BUY" else sig["entry"] - f["last"][1]
            f.update(result="flat", r=move / sig["risk"], slot=vs.FLAT_SLOT)
            when = de_time(day, vs.FLAT_SLOT, end=True)
    else:
        f = vs.follow(session, sig, last_slot=complete, be_at=vs.BE_AT, trail=vs.TRAIL)
        when = de_time(day, f["slot"], end=True) if f["result"] else None
    if f["result"]:
        state["trades"].append({**sig, "result": f["result"], "r": f["r"], "closed_slot": f["slot"]})
        state["open"] = None
        state["seen"] = max(state["seen"] or -1, f["slot"])
        return "CLOSED", closed_line(day, sig, f["result"], f["r"], when)
    if f["moved"] and not sig.get("moved"):
        sig["moved"] = True
        return "MOVE", move_line(day, sig, f["stop"])
    return None


def step(state, session, complete, day, rules=None, history=None, minutes=None):
    """Advance the day by the bars up to slot `complete`. Returns (event, text) or None.

    history holds earlier sessions (oldest first) for the sweep's volume check,
    the timeframes and the context. minutes, if given, are the complete 1-minute
    bars as (start epoch, bar): an open signal is then followed minute by minute.
    Pure: no clock and no network, so it can be tested on saved sessions.
    """
    rules = {**RULES, **(rules or {})}
    state.setdefault("skipped", [])
    if state["open"]:
        return follow_open(state, session, complete, day, minutes)
    trades = state["trades"]
    day_r = sum(t["r"] for t in trades)
    if day_r <= -rules["day_loss_pct"] / rules["risk_pct"]:
        return "END", (f"Tagesverlustlimit erreicht ({num(day_r)} R = {num(-day_r * rules['risk_pct'])} % "
                       f"des Kontos bei {num(rules['risk_pct'])} % pro Trade). Heute keine weiteren Signale.")
    streak = 0
    for t in reversed(trades):
        if t["r"] >= 0:
            break
        streak += 1
    if streak >= rules["pause_after"] and state.get("paused_at") != len(trades):
        state["paused_at"] = len(trades)
        state["pause_until"] = trades[-1]["closed_slot"] + rules["pause_minutes"] // 5
        return "PAUSE", (f"{streak} Verluste in Folge: Pause bis {de_time(day, state['pause_until'], end=True)} "
                         "Uhr, keine neuen Signale. Nicht nachlegen.")
    if len(trades) >= rules["max_trades"]:
        return "END", f"{rules['max_trades']} Signale heute erreicht. Für heute keine weiteren."
    if complete > vs.LAST_ENTRY_SLOT:
        return "END", (f"{de_time(day, vs.LAST_ENTRY_SLOT, end=True)} Uhr vorbei: "
                       "keine neuen Einstiege mehr heute.")
    if state["seen"] is None:
        # Started late: only the newest complete bar is fresh enough to act on.
        state["seen"] = complete - 1
    sides = vs.SIDE_OF_BIAS[state["bias"]] if rules["trend_filter"] else vs.SIDE_OF_BIAS["neutral"]
    vw = vs.session_vwap(session)
    for k in range(state["seen"] + 1, complete + 1):
        state["seen"] = k
        if k < vs.FIRST_SLOT or k != complete:
            continue
        if state.get("pause_until") is not None and k <= state["pause_until"]:
            continue
        sig = vs.signal_at(session, k, vw, history, vs.SETUPS, rules["rr"], rules["min_risk"])
        if not sig or sig["side"] not in sides:
            continue
        news = blocked(day, k, rules)
        if news:
            state["skipped"].append({"slot": k, "side": sig["side"], "why": f"Nachrichten {news} Uhr"})
            continue
        if history:
            sessions = {**history, day.isoformat(): session}
            sig["context"] = vs.signal_context(history, day.isoformat(), session, sig)
            sig["trends"] = vs.mtf_trends(sessions, day.isoformat(), k)
            recent = [history[d] for d in sorted(history)[-3:]]
            today = {j: b for j, b in session.items() if j <= k}
            sig["clusters"] = vs.nearest_clusters(vs.volume_levels(recent + [today]), sig["entry"])
        sig["fomo"] = vs.impulsive(session, sig)
        sig["score"] = vs.setup_score(sig.get("trends", {}), sig["side"],
                                      sig.get("context", {}).get("quality"), state["bias"], sig["fomo"])
        if sig["score"] < rules["min_score"]:
            state["skipped"].append({"slot": k, "side": sig["side"], "why": f"Score {sig['score']}"})
            continue
        state["open"] = sig
        return "SIGNAL", signal_line(day, sig, state["bias"], rules)
    return None


def complete_slot(session, data_time, now):
    """The last slot whose bar has ended and is covered by Yahoo's data, or -1."""
    done = [k for k, b in session.items() if b["t"] + 300 <= min(now, data_time)]
    return max(done) if done else -1


def cmd_next(args, rules):
    started = time.time()
    day = datetime.datetime.now(NY).date()
    if vs.orb.session_times(day, datetime.time(15, 0), datetime.time(15, 55)) is None:
        print("END Heute ist an der US-Börse kein Handel.")
        return 0
    state = load_state(day)
    if state["bias"] is None:
        yearly = [b for b in vs.fetch_yearly() if b["date"] < day.isoformat()]
        state["bias"] = vs.orb.market_regime(yearly[-30:])["bias"]
        save_state(state)
    try:
        history = {d: s for d, s in vs.fetch_60d(datetime.datetime.now(datetime.timezone.utc)).items()
                   if d < day.isoformat()}
    except Exception:  # without history: no sweep, timeframes or context, so most signals score low
        history = None
    while True:
        now = time.time()
        session, data_time = vs.fetch_today(day)
        complete = complete_slot(session, data_time, now)
        minutes = None
        if state["open"]:
            minutes = [(t, b) for t, b in vs.fetch_minutes(day) if t + 60 <= min(now, data_time)]
        if complete >= 0:
            event = step(state, session, complete, day, rules, history, minutes)
            save_state(state)
            if event:
                print(f"{event[0]} {event[1]}")
                return 0
        flat_end = datetime.datetime.combine(day, datetime.time(15, 55), tzinfo=NY).timestamp()
        if now > flat_end + 600 and not state["open"]:
            print("END Handelstag vorbei.")
            return 0
        if time.time() - started > args.max_minutes * 60:
            print("WAIT Nichts Neues. Einfach erneut starten.")
            return 3
        # look every 20 seconds while a signal is open or a 5-minute bar is about to close
        to_close = 300 - now % 300
        time.sleep(20 if state["open"] or to_close < 40 else to_close - 25)


def cmd_status(args, rules):
    day = datetime.datetime.now(NY).date()
    print(json.dumps(load_state(day), indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["next", "status"])
    p.add_argument("--trend-filter", action="store_true",
                   help="only signal the side of the daily trend (default: both)")
    p.add_argument("--min-score", type=int, default=RULES["min_score"], help="NO TRADE under this score")
    p.add_argument("--risk-pct", type=float, default=RULES["risk_pct"], help="account risk per trade in %%")
    p.add_argument("--account", type=float, help="account in EUR, for the position size")
    p.add_argument("--eur-per-point", type=float, help="EUR per index point for size 1 of the CFD")
    p.add_argument("--blackout", action="append", default=[], help="news time HH:MM (German), repeatable")
    p.add_argument("--max-minutes", type=float, default=100, help="return WAIT after this long")
    args = p.parse_args(argv)
    rules = {**RULES, "trend_filter": args.trend_filter, "min_score": args.min_score,
             "risk_pct": args.risk_pct, "account": args.account, "eur_per_point": args.eur_per_point,
             "blackouts": args.blackout}
    return {"next": cmd_next, "status": cmd_status}[args.command](args, rules)


if __name__ == "__main__":
    sys.exit(main())
