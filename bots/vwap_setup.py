"""The NAS100 day-trading setup: a pullback to the session VWAP in the direction it slopes.

A session is {slot: bar}, slot 0 being the 9:30 New York bar, each bar a dict
with o, h, l, c and v (5-minute bars). The rule, on a COMPLETE bar k:

- BUY: the VWAP rises (above its value six bars earlier), the bar before
  closed above the VWAP, and bar k dips to the VWAP (low at or under it) but
  closes back above it. Stop a tenth of the bar's range under its low.
- SELL: the mirror image under a falling VWAP.
- Target: rr times the risk (1.5 by default). Stops under min_risk points
  are skipped: the spread would eat them.
- Signals on the bars from 10:05 to 15:00 New York, so from 16:10 to 21:05
  German time when they close; one position at a time, at most max_trades
  a day, flat by 15:55 New York (21:55 in Germany).

With the trend filter a day takes only the side of the daily trend (rising
or falling 20-day average); a neutral day takes both.

Each signal also carries context: the 15- and 45-minute trend and the
volume against the usual for that time of day. Replayed as filters the
higher timeframes did not help; calm volume did a little better, so it
grades a signal's quality instead of dropping signals.
"""

import datetime
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
NY = ZoneInfo("America/New_York")

FIRST_SLOT = 7      # 10:05 New York
LAST_ENTRY_SLOT = 66  # 15:00 New York
FLAT_SLOT = 76      # the 15:50 bar closes at the 15:55 flat-by
LOOK = 6            # VWAP slope over six bars (30 minutes)
SIDE_OF_BIAS = {"long": {"BUY"}, "short": {"SELL"}, "neutral": {"BUY", "SELL"}}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orb = _load("capital_orb", ROOT / "bots" / "capital_orb.py")


def session_vwap(session):
    """{slot: VWAP} from the open, on the bars' typical price."""
    pv = vol = 0.0
    out = {}
    for k in sorted(session):
        b = session[k]
        v = b.get("v") or 1
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vol += v
        out[k] = pv / vol
    return out


def pullback(session, k, vw=None, rr=1.5, min_risk=15.0):
    """The signal on bar k, or None."""
    vw = vw or session_vwap(session)
    b, p = session.get(k), session.get(k - 1)
    if b is None or p is None or k - 1 not in vw or k - LOOK not in vw:
        return None
    span = b["h"] - b["l"]
    if vw[k] > vw[k - LOOK] and p["c"] > vw[k - 1] and b["l"] <= vw[k] < b["c"]:
        side, stop = "BUY", b["l"] - 0.1 * span
    elif vw[k] < vw[k - LOOK] and p["c"] < vw[k - 1] and b["h"] >= vw[k] > b["c"]:
        side, stop = "SELL", b["h"] + 0.1 * span
    else:
        return None
    risk = abs(b["c"] - stop)
    if risk < min_risk:
        return None
    target = b["c"] + rr * risk if side == "BUY" else b["c"] - rr * risk
    return {"slot": k, "side": side, "entry": b["c"], "stop": stop, "target": target,
            "risk": risk, "rr": rr, "vwap": vw[k]}


def outcome(session, sig, last_slot=FLAT_SLOT, flat=True):
    """(result, r, slot) after the signal bar, up to last_slot.

    result is "stop", "target", "flat" (at the close of the flat-by bar), or
    None while the trade is still open. A bar touching both counts as the stop.
    """
    long = sig["side"] == "BUY"
    last = None
    for k in range(sig["slot"] + 1, min(last_slot, FLAT_SLOT) + 1):
        b = session.get(k)
        if b is None:
            continue
        if (long and b["l"] <= sig["stop"]) or (not long and b["h"] >= sig["stop"]):
            return "stop", -1.0, k
        if (long and b["h"] >= sig["target"]) or (not long and b["l"] <= sig["target"]):
            return "target", sig["rr"], k
        last = (k, b["c"])
    if flat and last_slot >= FLAT_SLOT and last:
        move = last[1] - sig["entry"] if long else sig["entry"] - last[1]
        return "flat", move / sig["risk"], last[0]
    return None, None, None


def scan_session(session, bias="neutral", trend_filter=True, rr=1.5, min_risk=15.0, max_trades=3):
    """Every trade the rules take in one full session, one at a time."""
    vw = session_vwap(session)
    sides = SIDE_OF_BIAS[bias] if trend_filter else SIDE_OF_BIAS["neutral"]
    trades, busy = [], -1
    for k in range(FIRST_SLOT, LAST_ENTRY_SLOT + 1):
        if k <= busy or len(trades) >= max_trades:
            continue
        sig = pullback(session, k, vw, rr, min_risk)
        if sig is None or sig["side"] not in sides:
            continue
        result, r, end = outcome(session, sig)
        if result is None:
            continue
        trades.append({**sig, "result": result, "r": r})
        busy = end
    return trades


def bias_by_date(dates, yearly):
    """Each date's trend bias from the 30 daily bars before it."""
    out = {}
    for d in dates:
        prior = [b for b in yearly if b["date"] < d][-30:]
        out[d] = orb.market_regime(prior)["bias"] if len(prior) >= 26 else "neutral"
    return out


def backtest(days, biases, trend_filter=True, rr=1.5, min_risk=15.0, max_trades=3, cost=2.0):
    """Stats over full sessions; each trade's R net of cost points."""
    rs, risks, per_side = [], [], {"BUY": [], "SELL": []}
    sessions = 0
    for d in sorted(days):
        if len(days[d]) < 70:
            continue
        sessions += 1
        for t in scan_session(days[d], biases.get(d, "neutral"), trend_filter, rr, min_risk, max_trades):
            r = t["r"] - cost / t["risk"]
            rs.append(r)
            risks.append(t["risk"])
            per_side[t["side"]].append(r)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    risks.sort()
    return {"sessions": sessions, "trades": len(rs), "per_day": round(len(rs) / sessions, 1) if sessions else 0,
            "win_rate": round(sum(r > 0 for r in rs) / len(rs), 2) if rs else None,
            "avg_r": avg(rs), "total_r": round(sum(rs), 1), "worst_streak": worst,
            "median_risk": risks[len(risks) // 2] if risks else None,
            "buy_avg_r": avg(per_side["BUY"]), "sell_avg_r": avg(per_side["SELL"]),
            "buys": len(per_side["BUY"]), "sells": len(per_side["SELL"])}


# ---------------------------------------------------------------- data

def tools():
    return (_load("timemachine", SKILLS / "trader-timemachine" / "timemachine.py"),
            _load("entry", SKILLS / "trader-entry" / "entry.py"))


def fetch_60d(now_utc):
    """{date: {slot: bar}} for about 60 sessions of ^NDX."""
    tm, _ = tools()
    result = tm.chart_result(tm.fetch_chart("^NDX"), "^NDX")
    _, bars, _ = tm.parse_bars(result, now_utc.replace(tzinfo=None))
    return {d: {s: {**b, "v": b.get("v") or 0} for s, b in sess.items()}
            for d, sess in tm.split_sessions(bars).items()}


def fetch_yearly():
    """A year of ^NDX daily bars, oldest first."""
    tm, _ = tools()
    raw = json.loads(tm._get("https://query2.finance.yahoo.com/v8/finance/chart/%5ENDX?range=1y&interval=1d"))
    res = raw["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["high"][i], q["low"][i], q["close"][i]):
            continue
        out.append({"date": datetime.datetime.fromtimestamp(t, NY).date().isoformat(),
                    "high": q["high"][i], "low": q["low"][i], "close": q["close"][i]})
    return out


def fetch_today(day):
    """(session {slot: bar}, data time as epoch seconds) for today's ^NDX 5-minute bars."""
    _, entry = tools()
    ch = entry.get_chart("^NDX", "5m")
    open_ts = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=NY).timestamp()
    session = {}
    for b in ch["bars"]:
        slot, rem = divmod(b["t"] - open_ts, 300)
        if rem == 0 and 0 <= slot <= 77:
            session[int(slot)] = {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                                  "v": b.get("v") or 0}
    return session, ch["data_time"]


# ---------------------------------------------------------------- context: higher timeframes and volume

def higher_bars(sessions, n, upto=None):
    """Closes of n x 5-minute bars, aligned to 9:30, over sessions (oldest first).

    upto=(date, slot) keeps only the bars complete by that 5-minute slot's close.
    """
    closes = []
    for d in sorted(sessions):
        s = sessions[d]
        for start in range(0, 78, n):
            last = start + n - 1
            if upto and (d > upto[0] or (d == upto[0] and last > upto[1])):
                return closes
            if all(k in s for k in range(start, start + n)):
                closes.append(s[last]["c"])
    return closes


def trend_of(closes, n=20):
    """long, short or neutral: the last close against a rising or falling EMA."""
    if len(closes) < n + 3:
        return "neutral"
    e, line = None, []
    for c in closes:
        e = c if e is None else e + 2 / (n + 1) * (c - e)
        line.append(e)
    if closes[-1] > line[-1] and line[-1] > line[-4]:
        return "long"
    if closes[-1] < line[-1] and line[-1] < line[-4]:
        return "short"
    return "neutral"


def relative_volume(history, session, slot, days=20):
    """A bar's volume against the same slot's average over the last days sessions."""
    same = [s[slot].get("v") or 0 for s in [history[d] for d in sorted(history)][-days:] if slot in s]
    avg = sum(same) / len(same) if same else 0
    return (session[slot].get("v") or 0) / avg if avg and slot in session else None


def opening_volume(history, session, bars=6, days=20):
    """The first half hour's volume against its average over the last days sessions."""
    first = lambda s: sum(s[k].get("v") or 0 for k in range(bars) if k in s)
    prior = [first(history[d]) for d in sorted(history)[-days:]]
    avg = sum(prior) / len(prior) if prior else 0
    return first(session) / avg if avg and all(k in session for k in range(bars)) else None


def signal_context(history, day, session, sig):
    """15- and 45-minute trend, relative volume and a quality grade for a signal.

    Quality is from the replay: signals on a calm bar (volume under the usual
    for that time) and on a calm open did better. It grades, it does not filter.
    """
    sessions = {**history, day: session}
    upto = (day, sig["slot"])
    t15 = trend_of(higher_bars(sessions, 3, upto))
    t45 = trend_of(higher_bars(sessions, 9, upto))
    rv = relative_volume(history, session, sig["slot"])
    ov = opening_volume(history, session)
    calm = sum(1 for x in (rv, ov) if x is not None and x < 1.0)
    quality = "hoch" if calm == 2 else "mittel" if calm == 1 else "normal"
    return {"trend_15m": t15, "trend_45m": t45, "rel_volume": rv, "open_volume": ov, "quality": quality}
