#!/usr/bin/env python3
"""Live BUY/SELL signals for NAS100 day trading (bots/vwap_setup.py), in German.

Two setups on 5-minute bars: the VWAP pullback and the failed breakout
(a sweep of the last hour's high or low on heavy volume that closes back
inside). Both directions by default; --trend-filter keeps only the side of
the daily trend. Open signals are followed on 1-minute bars, and the loop
looks every 20 seconds, so a push goes out within about half a minute of
a bar's close (Yahoo's data is free and can lag).

`next` watches today's ^NDX 5-minute bars and returns at the next event,
printing one line and keeping the day in trader/state/live_signals.json:

  SIGNAL   a complete bar gave a BUY or SELL: entry, stop, target, reward:risk
  MOVE     the open signal is +0.5 R: move the stop to the entry and trail it
  CLOSED   the open signal reached its stop, its trailed stop or its target,
           or the 15:55 flat-by
  END      no more entries today (after 21:05 German time, max trades, 3 losses, closed day)
  WAIT     --max-minutes ran out with nothing new: just run it again

Exit code 0 for SIGNAL, MOVE, CLOSED and END, 3 for WAIT. A bar only counts once
Yahoo's data reaches past its end, so a half-finished bar never signals. One
signal is open at a time, at most --max-trades a day. Nothing is ever placed.

  python3 bots/live_signals.py next                  # both directions (default)
  python3 bots/live_signals.py next --trend-filter   # only the daily trend's side
  python3 bots/live_signals.py status
"""

import argparse
import datetime
import importlib.util
import json
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


def de_time(day, slot, end=False):
    """A slot's start (or end) as a German clock time."""
    t = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY) + datetime.timedelta(
        minutes=5 * (slot + (1 if end else 0)))
    return t.astimezone(BERLIN).strftime("%H:%M")


def num(x):
    return f"{x:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


TRENDS = {"long": "aufwärts", "short": "abwärts", "neutral": "seitwärts"}


def context_text(ctx):
    vol = lambda x: f"{num(x)}x" if x is not None else "?"
    note = {"hoch": "ruhiges Volumen, im Rückblick die besten Signale",
            "mittel": "teils ruhiges Volumen", "normal": "lebhaftes Volumen"}[ctx["quality"]]
    return (f" 15 Min {TRENDS[ctx['trend_15m']]}, 45 Min {TRENDS[ctx['trend_45m']]}. "
            f"Volumen Signalkerze {vol(ctx['rel_volume'])} üblich, Eröffnung {vol(ctx['open_volume'])}. "
            f"Qualität {ctx['quality'].upper()} ({note}).")


def setup_text(sig):
    if sig.get("setup") == "sweep":
        broke = "Hoch" if sig["side"] == "SELL" else "Tief"
        who = "Käufer" if sig["side"] == "SELL" else "Verkäufer"
        return (f"Fehlausbruch: Stundenhoch/-tief {num(sig['level'])} kurz gebrochen ({broke}), "
                f"Schluss zurück, Volumen {num(sig['volume'])}x üblich, die {who} verlieren den Boden.")
    return f"VWAP-Pullback am VWAP {num(sig['vwap'])}."


def clusters_text(sig):
    above, below = sig.get("clusters") or (None, None)
    parts = [f"darüber {num(above)}" if above else None, f"darunter {num(below)}" if below else None]
    parts = [x for x in parts if x]
    return f" Volumen-Cluster: {', '.join(parts)}." if parts else ""


def signal_line(day, sig, bias):
    word = "BUY (long)" if sig["side"] == "BUY" else "SELL (short)"
    with_trend = {"long": "BUY", "short": "SELL"}.get(bias)
    trend_note = ("" if with_trend is None else " (mit dem Trend)" if with_trend == sig["side"]
                  else " (gegen den Trend)")
    return (f"{word} NAS100 – Kerze bis {de_time(day, sig['slot'], end=True)} Uhr: Einstieg ~{num(sig['entry'])}, "
            f"Stop {num(sig['stop'])} (-{num(sig['risk'])} Pkt), "
            f"Ziel {num(sig['target'])} (+{num(sig['rr'] * sig['risk'])} Pkt), "
            f"CRV {num(sig['rr'])} : 1. Bei +{num(vs.BE_AT * sig['risk'])} Pkt kommt der Push zum Nachziehen. "
            f"{setup_text(sig)} Tagestrend {TRENDS[bias]}{trend_note}."
            + clusters_text(sig)
            + (context_text(sig["context"]) if sig.get("context") else "")
            + " Index-Kurse: beim CFD Stop und Ziel als Abstand übernehmen. Nichts platziert.")


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


def minute_de(ts):
    return datetime.datetime.fromtimestamp(ts + 60, BERLIN).strftime("%H:%M")


def load_state(day):
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("date") != day.isoformat():
        state = {"date": day.isoformat(), "bias": None, "seen": None, "open": None, "trades": []}
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def step(state, session, complete, day, trend_filter, rr, min_risk, max_trades, history=None,
         max_losses=3, minutes=None):
    """Advance the day by the bars up to slot `complete`. Returns (event, line) or None.

    history holds earlier sessions for the sweep's volume check and the context.
    minutes, if given, are the complete 1-minute bars as (start epoch, bar): an
    open signal is then followed minute by minute instead of by 5-minute bars.
    Pure: no clock and no network, so it can be tested on saved sessions.
    """
    if state["open"]:
        sig = state["open"]
        open_ts = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY).timestamp()
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
            state["trades"].append({**sig, "result": f["result"], "r": f["r"]})
            state["open"] = None
            state["seen"] = max(state["seen"] or -1, f["slot"])
            return "CLOSED", closed_line(day, sig, f["result"], f["r"], when)
        if f["moved"] and not sig.get("moved"):
            sig["moved"] = True
            return "MOVE", move_line(day, sig, f["stop"])
        return None
    losses = sum(1 for t in state["trades"] if t["r"] < 0)
    if losses >= max_losses:
        return "END", (f"{losses} Verluste heute: Pause bis morgen, keine weiteren Signale. "
                       "Nach mehreren Verlusten nachzulegen ist der teuerste Fehler im Day-Trading.")
    if len(state["trades"]) >= max_trades:
        return "END", f"{max_trades} Signale heute erreicht. Für heute keine weiteren."
    if complete > vs.LAST_ENTRY_SLOT:
        return "END", (f"{de_time(day, vs.LAST_ENTRY_SLOT, end=True)} Uhr vorbei: "
                       "keine neuen Einstiege mehr heute.")
    if state["seen"] is None:
        # Started late: only the newest complete bar is fresh enough to act on.
        state["seen"] = complete - 1
    sides = vs.SIDE_OF_BIAS[state["bias"]] if trend_filter else vs.SIDE_OF_BIAS["neutral"]
    vw = vs.session_vwap(session)
    for k in range(state["seen"] + 1, complete + 1):
        state["seen"] = k
        if k < vs.FIRST_SLOT:
            continue
        sig = vs.signal_at(session, k, vw, history, vs.SETUPS, rr, min_risk)
        if sig and sig["side"] in sides and k == complete:
            if history:
                sig["context"] = vs.signal_context(history, day.isoformat(), session, sig)
                recent = [history[d] for d in sorted(history)[-3:]]
                today = {j: b for j, b in session.items() if j <= k}
                sig["clusters"] = vs.nearest_clusters(vs.volume_levels(recent + [today]), sig["entry"])
            state["open"] = sig
            return "SIGNAL", signal_line(day, sig, state["bias"])
    return None


def complete_slot(session, data_time, now):
    """The last slot whose bar has ended and is covered by Yahoo's data, or -1."""
    done = [k for k, b in session.items() if b["t"] + 300 <= min(now, data_time)]
    return max(done) if done else -1


def cmd_next(args):
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
    except Exception:  # the context is optional; signals go out without it
        history = None
    while True:
        now = time.time()
        session, data_time = vs.fetch_today(day)
        complete = complete_slot(session, data_time, now)
        minutes = None
        if state["open"]:
            minutes = [(t, b) for t, b in vs.fetch_minutes(day) if t + 60 <= min(now, data_time)]
        if complete >= 0:
            event = step(state, session, complete, day, args.trend_filter, args.rr, args.min_risk,
                         args.max_trades, history, args.max_losses, minutes)
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


def cmd_status(args):
    day = datetime.datetime.now(NY).date()
    print(json.dumps(load_state(day), indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["next", "status"])
    p.add_argument("--trend-filter", action="store_true",
                   help="only signal the side of the daily trend (default: both)")
    p.add_argument("--rr", type=float, default=vs.TARGET_R, help="target in R (default 2)")
    p.add_argument("--min-risk", type=float, default=15.0, help="skip stops under this many points")
    p.add_argument("--max-trades", type=int, default=6, help="signals a day (default 6)")
    p.add_argument("--max-losses", type=int, default=3, help="pause for the day after this many losses")
    p.add_argument("--max-minutes", type=float, default=100, help="return WAIT after this long")
    args = p.parse_args(argv)
    return {"next": cmd_next, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
