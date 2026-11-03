#!/usr/bin/env python3
"""Market-aware opening-range breakout bot for Capital.com CFDs (default: US100, the Nasdaq 100).

Runs one trading day on its own, long or short:

1. Reads the market first, from Capital.com's daily bars: the trend (last
   close against a rising or falling 20-day average) sets the bias, and
   stress (the 5-day against the 20-day average true range) sets the risk:
   normal is full size, raised is half size, high is no trade today.
2. Builds the 9:30-9:45 New York opening range from 5-minute bars and skips
   the day when the range is too wide for the daily ATR.
3. Trades the first COMPLETE bar that closes clearly beyond the range, by
   at least a buffer (5% of the range width), in a direction the bias
   allows: above the high is a buy, below the low is a sell. A bar is
   complete once Capital.com has started the next one. A neutral bias
   allows both directions. New entries only until 11:30 New York (17:30
   in Germany).
4. Aims at the measured move (the range height from the broken level) and
   skips the trade when the reward:risk at the actual price is under 1.5,
   so a late entry far beyond the level is not chased. Sends the stop and
   the target with the order, skips a target too far for the day's ATR,
   and closes the position at the flat-by time.

With --agents it also needs today's call from bots/agent_desk.py (the
TradingAgents team): it trades only the direction both the market read and
the desk agree on, and not at all when the desk says Hold or has no call
for today.

One trade a day. Demo account by default; the live account needs both --live
and CAPITAL_LIVE=yes. --dry-run logs every decision and sends no order.

Credentials come from environment variables only, never from arguments:
  CAPITAL_API_KEY       the API key (Capital.com: Settings > API integrations)
  CAPITAL_IDENTIFIER    the login email of the account
  CAPITAL_API_PASSWORD  the custom password set for that API key

  python3 bots/capital_orb.py regime              # market read and today's range, no trading
  python3 bots/capital_orb.py run --dry-run       # watch and log, no orders
  python3 bots/capital_orb.py run                 # trade on the demo account
  python3 bots/capital_orb.py status              # account, positions, today's state
  python3 bots/capital_orb.py flatten             # close the bot's open position now

Create the file trader/STOP to stop new entries at once (the kill switch).
This is tooling, not financial advice. CFDs are leveraged: losses can exceed
what a stop suggests when the market gaps or slips.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "trader" / "state" / "capital_orb.json"
KILL_SWITCH = ROOT / "trader" / "STOP"
AGENT_VIEW = ROOT / "trader" / "state" / "agent_view.json"
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

DEMO_URL = "https://demo-api-capital.backend-capital.com"
LIVE_URL = "https://api-capital.backend-capital.com"
RESOLUTIONS = {5: "MINUTE_5", 1: "MINUTE", 1440: "DAY"}

OPEN = datetime.time(9, 30)
SIDES = {"long": {"BUY"}, "short": {"SELL"}, "both": {"BUY", "SELL"}}
WORDS = {"BUY": "KAUF (long)", "SELL": "VERKAUF (short)"}


def _load_calendar():
    """NYSE holidays and early closes from the trader-alerts skill, if present."""
    path = ROOT / ".claude" / "skills" / "trader-alerts" / "alerts.py"
    if not path.exists():
        return {}, {}
    spec = importlib.util.spec_from_file_location("trader_alerts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NYSE_HOLIDAYS, mod.NYSE_EARLY_CLOSE


HOLIDAYS, EARLY_CLOSE = _load_calendar()


class BotError(Exception):
    pass


# ---------------------------------------------------------------- pure logic

def session_times(day, no_entries_after, flat_by):
    """(entry cutoff, flat-by) as New York times, or None on a closed day.

    On a 1:00 p.m. early close a time after 1:00 p.m. moves up by three
    hours, keeping its gap to the close; an earlier one stays as it is.
    """
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    if day.isoformat() not in EARLY_CLOSE:
        return no_entries_after, flat_by
    early = datetime.time(13, 0)
    shift = datetime.timedelta(hours=3)
    move = lambda t: (datetime.datetime.combine(day, t) - shift).time() if t > early else t
    return move(no_entries_after), move(flat_by)


def parse_bars(prices):
    """Capital.com /prices rows -> [{start (UTC), open, high, low, close}] at mid prices."""
    mid = lambda p: (p["bid"] + p["ask"]) / 2
    bars = []
    for row in prices:
        start = datetime.datetime.fromisoformat(row["snapshotTimeUTC"]).replace(
            tzinfo=datetime.timezone.utc)
        bars.append({"start": start, "open": mid(row["openPrice"]), "high": mid(row["highPrice"]),
                     "low": mid(row["lowPrice"]), "close": mid(row["closePrice"])})
    return sorted(bars, key=lambda b: b["start"])


def closed_bars(bars, now, minutes):
    return [b for b in bars if b["start"] + datetime.timedelta(minutes=minutes) <= now]


def complete_bars(bars, now, minutes):
    """Closed bars the broker has moved past: a later bar exists, so this one is final."""
    return closed_bars(bars[:-1], now, minutes)


def market_regime(daily, stress_raised=1.5, stress_high=2.0):
    """Trend bias and stress from closed daily bars (oldest first).

    Bias: long when the last close is above a rising 20-day average, short
    when it is below a falling one, neutral otherwise. Stress: the 5-day
    average true range against the 20-day one.
    """
    if len(daily) < 26:
        raise BotError(f"Zu wenig Tageskerzen für die Marktlage ({len(daily)}, gebraucht 26)")
    avg = lambda xs: sum(xs) / len(xs)
    closes = [b["close"] for b in daily]
    sma20, sma20_before = avg(closes[-20:]), avg(closes[-25:-5])
    ranges = [max(b["high"] - b["low"], abs(b["high"] - prev["close"]), abs(b["low"] - prev["close"]))
              for prev, b in zip(daily, daily[1:])]
    atr20, atr5 = avg(ranges[-20:]), avg(ranges[-5:])
    last = closes[-1]
    if last > sma20 and sma20 > sma20_before:
        bias = "long"
    elif last < sma20 and sma20 < sma20_before:
        bias = "short"
    else:
        bias = "neutral"
    ratio = atr5 / atr20
    stress = "hoch" if ratio >= stress_high else "erhöht" if ratio >= stress_raised else "normal"
    return {"bias": bias, "stress": stress, "stress_ratio": round(ratio, 2), "atr": atr20,
            "sma20": sma20, "prev_close": last}


def allowed_sides(side, bias):
    """The directions to trade: the bias's own, both when it is neutral, or the override."""
    if side != "auto":
        return SIDES[side]
    return SIDES["both"] if bias == "neutral" else SIDES[bias]


def load_agent_view(day, path=None):
    """Today's call from bots/agent_desk.py. Raises BotError when there is none for today."""
    path = path or AGENT_VIEW
    try:
        view = json.loads(path.read_text())
    except (OSError, ValueError):
        raise BotError(f"Keine Einschätzung vom Agenten-Desk ({path}). "
                       "Erst bots/agent_desk.py laufen lassen. Kein Trade.")
    if view.get("date") != day.isoformat():
        raise BotError(f"Die Einschätzung vom Agenten-Desk ist vom {view.get('date')}, "
                       f"nicht von heute ({day}). Kein Trade.")
    return view


def combine_sides(sides, agent_bias):
    """The directions both the market read and the agent desk allow."""
    return sides & SIDES.get(agent_bias, set())


def opening_range(bars, day, or_minutes, minutes):
    """(high, low) of the bars from 9:30 New York, or None until all of them are in."""
    start = datetime.datetime.combine(day, OPEN, tzinfo=NY)
    end = start + datetime.timedelta(minutes=or_minutes)
    inside = [b for b in bars if start <= b["start"] < end]
    if len(inside) < or_minutes // minutes:
        return None
    return max(b["high"] for b in inside), min(b["low"] for b in inside)


def find_trigger(bars, day, or_minutes, high, low, sides, buffer=0.0):
    """(bar, "BUY" | "SELL") for the first close beyond the range, or (None, None)."""
    end = datetime.datetime.combine(day, OPEN, tzinfo=NY) + datetime.timedelta(minutes=or_minutes)
    for b in bars:
        if b["start"] < end:
            continue
        if "BUY" in sides and b["close"] > high + buffer:
            return b, "BUY"
        if "SELL" in sides and b["close"] < low - buffer:
            return b, "SELL"
    return None, None


def plan_trade(direction, price, or_high, or_low, stop_mode, target_mode, rr, min_rr, risk,
               point_value, min_size, step, decimals):
    """Stop, target and size for a trade at price. Raises BotError when it does not fit.

    The stop sits at the range midpoint or at the far edge of the range (the
    low for a buy, the high for a sell), rounded away from the price. The
    target is the measured move (the range height beyond the broken level)
    or rr times the risk; under min_rr reward:risk there is no trade.
    """
    factor = 10 ** decimals
    long = direction == "BUY"
    edge = or_low if long else or_high
    stop = (or_high + or_low) / 2 if stop_mode == "mid" else edge
    stop = math.floor(stop * factor) / factor if long else math.ceil(stop * factor) / factor
    dist = price - stop if long else stop - price
    if dist <= 0:
        raise BotError(f"Stop {stop} liegt auf der falschen Seite des Einstiegs {price}")
    if target_mode == "measured":
        width = or_high - or_low
        target = or_high + width if long else or_low - width
    else:
        target = price + rr * dist if long else price - rr * dist
    target = math.ceil(target * factor) / factor if long else math.floor(target * factor) / factor
    reward = target - price if long else price - target
    if reward / dist < min_rr:
        raise BotError(f"Chance zu Risiko {reward / dist:.2f} : 1 unter dem Minimum {min_rr:g} : 1 "
                       f"(Ziel {target}, Stop {stop}, Einstieg {price}). Zu spät oder zu wenig Luft. Kein Trade.")
    raw = risk / (dist * point_value)
    size = round(math.floor(raw / step + 1e-9) * step, 6)
    if size < min_size:
        raise BotError(f"Stop zu weit weg: {dist:.2f} Punkte bei max. Verlust {risk} ergibt "
                       f"Größe {raw:.4f}, das Minimum ist {min_size}. Kein Trade.")
    return {"direction": direction, "entry": price, "stop": stop, "target": target, "size": size,
            "rr": round(reward / dist, 2), "max_loss": round(size * dist * point_value, 2)}


def range_problem(rng, regime, max_or_atr):
    """Why the opening range is not worth trading today, or None."""
    width = rng[0] - rng[1]
    if width > max_or_atr * regime["atr"]:
        return (f"Opening Range {width:.1f} Punkte breit, mehr als {max_or_atr:g} x ATR "
                f"({regime['atr']:.1f}). Zu unruhig, heute kein Trade.")
    return None


def target_problem(plan, regime, max_target_atr):
    """Why the target is out of reach for a normal day, or None."""
    reach = abs(plan["target"] - plan["entry"])
    if reach > max_target_atr * regime["atr"]:
        return (f"Ziel {reach:.1f} Punkte entfernt, mehr als {max_target_atr:g} x ATR "
                f"({regime['atr']:.1f}). Unrealistisch, kein Trade.")
    return None


def risk_for(regime, risk):
    """Full risk on a normal day, half when stress is raised, none when it is high."""
    return {"normal": risk, "erhöht": risk / 2, "hoch": 0.0}[regime["stress"]]


# ---------------------------------------------------------------- API client

class Capital:
    def __init__(self, base_url, api_key, identifier, password, opener=urllib.request.urlopen):
        self.base, self.key, self.ident, self.pw = base_url, api_key, identifier, password
        self.open = opener
        self.tokens = {}

    def _call(self, method, path, body=None, retry=True):
        headers = {"X-CAP-API-KEY": self.key, "Content-Type": "application/json", **self.tokens}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.open(req, timeout=20) as resp:
                raw = resp.read()
                if path == "/api/v1/session" and method == "POST":
                    self.tokens = {"CST": resp.headers["CST"],
                                   "X-SECURITY-TOKEN": resp.headers["X-SECURITY-TOKEN"]}
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401 and retry and path != "/api/v1/session":
                self.login()  # sessions end after 10 idle minutes
                return self._call(method, path, body, retry=False)
            raise BotError(f"Capital.com {method} {path}: HTTP {e.code} {detail}")
        except urllib.error.URLError as e:
            raise BotError(f"Capital.com nicht erreichbar: {e.reason}")

    def login(self):
        self._call("POST", "/api/v1/session",
                   {"identifier": self.ident, "password": self.pw, "encryptedPassword": False})

    def market(self, epic):
        return self._call("GET", f"/api/v1/markets/{epic}")

    def bars(self, epic, minutes, count=200):
        return parse_bars(self._call(
            "GET", f"/api/v1/prices/{epic}?resolution={RESOLUTIONS[minutes]}&max={count}")["prices"])

    def positions(self):
        return self._call("GET", "/api/v1/positions").get("positions", [])

    def accounts(self):
        return self._call("GET", "/api/v1/accounts").get("accounts", [])

    def order(self, epic, direction, size, stop, target):
        ref = self._call("POST", "/api/v1/positions", {
            "epic": epic, "direction": direction, "size": size, "guaranteedStop": False,
            "stopLevel": stop, "profitLevel": target})["dealReference"]
        time.sleep(1)
        confirm = self._call("GET", f"/api/v1/confirms/{ref}")
        if confirm.get("dealStatus") != "ACCEPTED":
            raise BotError(f"Order abgelehnt: {confirm.get('reason') or confirm}")
        deals = confirm.get("affectedDeals") or [{}]
        return deals[0].get("dealId") or confirm.get("dealId"), confirm.get("level")

    def close(self, deal_id):
        return self._call("DELETE", f"/api/v1/positions/{deal_id}")


# ---------------------------------------------------------------- the bot

def log(msg):
    now = datetime.datetime.now(BERLIN).strftime("%H:%M:%S")
    print(f"[{now} DE] {msg}", flush=True)


def load_state(day):
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("date") != day.isoformat():
        state = {"date": day.isoformat(), "trades": []}
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def de(t):
    """A UTC or New York datetime as a German clock time."""
    return t.astimezone(BERLIN).strftime("%H:%M")


def dealing_rules(market):
    rules = market.get("dealingRules", {})
    value = lambda name, default: (rules.get(name) or {}).get("value", default)
    snap = market.get("snapshot", {})
    return {"min_size": value("minDealSize", 0.01), "step": value("minSizeIncrement", 0.01),
            "decimals": snap.get("decimalPlacesFactor", 2), "ask": snap.get("offer"),
            "bid": snap.get("bid"), "status": snap.get("marketStatus"),
            "currency": market.get("instrument", {}).get("currency")}


def open_deal_ids(api, epic):
    return {p["position"]["dealId"] for p in api.positions() if p["market"]["epic"] == epic}


def sleep_until(t):
    while True:
        left = (t - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60))


def read_market(api, args, now):
    daily = closed_bars(api.bars(args.epic, 1440, 60), now, 1440)
    regime = market_regime(daily, args.stress_raised, args.stress_high)
    sides = allowed_sides(args.side, regime["bias"])
    if args.agents:
        view = load_agent_view(now.astimezone(NY).date())
        log(f"Agenten-Desk ({view['ticker']}): Rating {view['rating']} -> {view['bias']}.")
        sides = combine_sides(sides, view["bias"])
    log(f"Marktlage {args.epic}: Trend {regime['bias']}, Stress {regime['stress']} "
        f"(ATR 5/20 = {regime['stress_ratio']}), ATR {regime['atr']:.1f}, "
        f"Vortagesschluss {regime['prev_close']:.1f}. "
        f"Erlaubt: {', '.join(WORDS[s] for s in sorted(sides)) or 'nichts'}.")
    return regime, sides


def regime_cmd(api, args):
    now = datetime.datetime.now(datetime.timezone.utc)
    day = now.astimezone(NY).date()
    regime, _ = read_market(api, args, now)
    risk = risk_for(regime, args.risk)
    log(f"Risiko heute: {risk:g} pro Trade" + (" (kein Trade bei hohem Stress)" if not risk else ""))
    rng = opening_range(closed_bars(api.bars(args.epic, args.interval), now, args.interval),
                        day, args.or_minutes, args.interval)
    if rng:
        log(f"Opening Range: Hoch {rng[0]:.2f}, Tief {rng[1]:.2f}.")
        problem = range_problem(rng, regime, args.max_or_atr)
        if problem:
            log(problem)
    else:
        log("Opening Range noch nicht vollständig.")
    return 0


def run(api, args):
    now = datetime.datetime.now(datetime.timezone.utc)
    day = now.astimezone(NY).date()
    times = session_times(day, args.no_entries_after, args.flat_by)
    if times is None:
        log(f"{day}: US-Börse geschlossen (Wochenende oder Feiertag). Kein Handel.")
        return 0
    cutoff, flat_by = (datetime.datetime.combine(day, t, tzinfo=NY) for t in times)
    or_end = datetime.datetime.combine(day, OPEN, tzinfo=NY) + datetime.timedelta(minutes=args.or_minutes)
    state = load_state(day)
    mode = "DRY-RUN" if args.dry_run else ("LIVE" if api.base == LIVE_URL else "DEMO")
    log(f"{mode} {args.epic}: Range bis {de(or_end)}, Einstiege bis {de(cutoff)}, "
        f"glattstellen um {de(flat_by)}, max. Verlust {args.risk:g} pro Trade.")

    deal = next((t for t in state["trades"] if t.get("open")), None)
    if deal is not None:
        log(f"Offene Position aus diesem Tag gefunden (Deal {deal['deal_id']}), wird weiter verwaltet.")
        return manage(api, args, deal, state, flat_by)
    if len(state["trades"]) >= args.max_trades:
        log("Heute schon gehandelt. Keine weiteren Einstiege.")
        return 0

    try:
        regime, sides = read_market(api, args, now)
    except BotError as e:
        log(str(e))
        return 0
    if not sides:
        log("Marktlage und Agenten-Desk erlauben heute keine gemeinsame Richtung. Kein Trade.")
        return 0
    risk = risk_for(regime, args.risk)
    if not risk:
        log("Stress hoch: Die Schwankungen sind doppelt so groß wie üblich. Heute kein Trade.")
        return 0
    if risk < args.risk:
        log(f"Stress erhöht: Risiko auf {risk:g} pro Trade halbiert.")

    sleep_until(or_end + datetime.timedelta(seconds=20))
    rng = None
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= cutoff:
            log("Einstiegsfenster vorbei, kein Ausbruch. Heute kein Trade.")
            return 0
        bars = complete_bars(api.bars(args.epic, args.interval), now, args.interval)
        if rng is None:
            rng = opening_range(bars, day, args.or_minutes, args.interval)
            if rng is None:
                if now > or_end + datetime.timedelta(minutes=15):
                    log("Keine vollständige Opening Range von Capital.com. Abbruch.")
                    return 1
            else:
                log(f"Opening Range: Hoch {rng[0]:.2f}, Tief {rng[1]:.2f}.")
                problem = range_problem(rng, regime, args.max_or_atr)
                if problem:
                    log(problem)
                    return 0
        if rng:
            buffer = args.buffer * (rng[0] - rng[1])
            bar, direction = find_trigger(bars, day, args.or_minutes, rng[0], rng[1], sides, buffer)
            if bar:
                bar_end = bar["start"] + datetime.timedelta(minutes=args.interval)
                if now - bar_end > datetime.timedelta(minutes=args.interval + 3):
                    log(f"Ausbruch schon um {de(bar_end)}, zu alt für einen Einstieg. Kein Trade.")
                    return 0
                side = "über dem Hoch" if direction == "BUY" else "unter dem Tief"
                log(f"AUSBRUCH {WORDS[direction]}: Kerze bis {de(bar_end)} schloss bei "
                    f"{bar['close']:.2f} {side}.")
                deal = enter(api, args, direction, rng, regime, risk, state)
                if deal is None:
                    return 0
                return manage(api, args, deal, state, flat_by)
            if bars:
                last = bars[-1]["close"]
                log(f"Wartet: letzter Schluss {last:.2f} (Range {rng[1]:.2f} - {rng[0]:.2f}"
                    f", Puffer {buffer:.1f}).")
        step = args.interval * 60
        wake = (int(now.timestamp()) // step + 1) * step + 15
        sleep_until(datetime.datetime.fromtimestamp(wake, datetime.timezone.utc))


def enter(api, args, direction, rng, regime, risk, state):
    if KILL_SWITCH.exists():
        log(f"Kill-Switch {KILL_SWITCH} ist da. Kein Einstieg.")
        return None
    rules = dealing_rules(api.market(args.epic))
    if rules["status"] != "TRADEABLE":
        log(f"Markt nicht handelbar ({rules['status']}). Kein Einstieg.")
        return None
    price = rules["ask"] if direction == "BUY" else rules["bid"]
    try:
        plan = plan_trade(direction, price, rng[0], rng[1], args.stop, args.target, args.rr,
                          args.min_rr, risk, args.point_value, rules["min_size"], rules["step"],
                          rules["decimals"])
    except BotError as e:
        log(str(e))
        return None
    problem = target_problem(plan, regime, args.max_target_atr)
    if problem:
        log(problem)
        return None
    log(f"Plan: {WORDS[direction]} {plan['size']} @ ~{plan['entry']}, Stop {plan['stop']}, "
        f"Ziel {plan['target']}, CRV {plan['rr']} : 1, max. Verlust ~{plan['max_loss']} {rules['currency'] or ''}.")
    if args.dry_run:
        log("DRY-RUN: keine Order gesendet.")
        state["trades"].append({**plan, "dry_run": True})
        save_state(state)
        return None
    deal_id, level = api.order(args.epic, direction, plan["size"], plan["stop"], plan["target"])
    trade = {**plan, "deal_id": deal_id, "fill": level, "open": True,
             "time": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    state["trades"].append(trade)
    save_state(state)
    log(f"AUSGEFÜHRT: {WORDS[direction]}, Deal {deal_id}, Kurs {level}.")
    return trade


def manage(api, args, deal, state, flat_by):
    while datetime.datetime.now(datetime.timezone.utc) < flat_by:
        if deal["deal_id"] not in open_deal_ids(api, args.epic):
            log("Position geschlossen (Stop oder Ziel erreicht).")
            deal["open"] = False
            save_state(state)
            return 0
        time.sleep(60)
    if deal["deal_id"] in open_deal_ids(api, args.epic):
        api.close(deal["deal_id"])
        log(f"Glattgestellt um {de(flat_by)}.")
    deal["open"] = False
    save_state(state)
    return 0


def status(api, args):
    day = datetime.datetime.now(NY).date()
    for acc in api.accounts():
        bal = acc.get("balance", {})
        print(f"Konto {acc.get('accountName')}: Saldo {bal.get('balance')} {acc.get('currency')}, "
              f"verfügbar {bal.get('available')}")
    for p in api.positions():
        pos, mkt = p["position"], p["market"]
        print(f"Position {mkt['epic']} {pos['direction']} {pos['size']} @ {pos['level']}, "
              f"Stop {pos.get('stopLevel')}, Ziel {pos.get('profitLevel')}, Deal {pos['dealId']}")
    print(json.dumps(load_state(day), indent=2))
    return 0


def flatten(api, args):
    state = load_state(datetime.datetime.now(NY).date())
    open_ids = open_deal_ids(api, args.epic)
    for t in state["trades"]:
        if t.get("open") and t["deal_id"] in open_ids:
            api.close(t["deal_id"])
            log(f"Deal {t['deal_id']} geschlossen.")
        t["open"] = False
    save_state(state)
    return 0


def hhmm(s):
    return datetime.datetime.strptime(s, "%H:%M").time()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["run", "regime", "status", "flatten"])
    p.add_argument("--epic", default="US100", help="Capital.com market (default US100)")
    p.add_argument("--side", choices=["auto", "long", "short", "both"], default="auto",
                   help="auto follows the trend bias (default); the others override it")
    p.add_argument("--agents", action="store_true",
                   help="also follow today's call from bots/agent_desk.py")
    p.add_argument("--risk", type=float, default=50.0,
                   help="max loss per trade in the instrument's currency (default 50)")
    p.add_argument("--point-value", type=float, default=1.0,
                   help="profit or loss per point for size 1 (default 1)")
    p.add_argument("--stop", choices=["mid", "edge"], default="mid",
                   help="stop at the range midpoint or its far edge (default mid)")
    p.add_argument("--target", choices=["measured", "r"], default="measured",
                   help="measured move from the broken level (default) or --rr times the risk")
    p.add_argument("--rr", type=float, default=2.0, help="target in R with --target r (default 2)")
    p.add_argument("--min-rr", type=float, default=1.5,
                   help="skip a trade under this reward:risk at the actual price (default 1.5)")
    p.add_argument("--buffer", type=float, default=0.05,
                   help="the close must clear the range by this share of its width (default 0.05)")
    p.add_argument("--max-or-atr", type=float, default=0.5,
                   help="skip the day when the range is wider than this times the ATR (default 0.5)")
    p.add_argument("--max-target-atr", type=float, default=1.0,
                   help="skip a target farther than this times the ATR (default 1.0)")
    p.add_argument("--stress-raised", type=float, default=1.5,
                   help="ATR 5/20 ratio that halves the risk (default 1.5)")
    p.add_argument("--stress-high", type=float, default=2.0,
                   help="ATR 5/20 ratio that stops trading for the day (default 2.0)")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--interval", type=int, choices=[1, 5], default=5)
    p.add_argument("--no-entries-after", type=hhmm, default=hhmm("11:30"),
                   help="New York time (default 11:30, 17:30 in Germany)")
    p.add_argument("--flat-by", type=hhmm, default=hhmm("15:55"), help="New York time")
    p.add_argument("--max-trades", type=int, default=1, help="trades per day (default 1)")
    p.add_argument("--dry-run", action="store_true", help="log decisions, send no orders")
    p.add_argument("--live", action="store_true", help="live account; also needs CAPITAL_LIVE=yes")
    args = p.parse_args(argv)

    missing = [n for n in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD")
               if not os.environ.get(n)]
    if missing:
        print("Fehlende Umgebungsvariablen: " + ", ".join(missing), file=sys.stderr)
        return 2
    if args.live and os.environ.get("CAPITAL_LIVE") != "yes":
        print("--live braucht zusätzlich CAPITAL_LIVE=yes.", file=sys.stderr)
        return 2
    if args.or_minutes % args.interval:
        print("--or-minutes muss ein Vielfaches von --interval sein.", file=sys.stderr)
        return 2

    api = Capital(LIVE_URL if args.live else DEMO_URL, os.environ["CAPITAL_API_KEY"],
                  os.environ["CAPITAL_IDENTIFIER"], os.environ["CAPITAL_API_PASSWORD"])
    commands = {"run": run, "regime": regime_cmd, "status": status, "flatten": flatten}
    try:
        api.login()
        return commands[args.command](api, args)
    except BotError as e:
        log(f"FEHLER: {e}")
        return 1
    except KeyboardInterrupt:
        log("Abgebrochen. Eine offene Position bleibt mit Stop und Ziel beim Broker stehen.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
