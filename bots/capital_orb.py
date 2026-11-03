#!/usr/bin/env python3
"""Opening-range breakout bot for Capital.com CFDs (default: US100, the Nasdaq 100).

Runs one trading day on its own: builds the 9:30-9:45 New York opening range
from Capital.com's own 5-minute bars, buys on the first CLOSED bar above the
range high, sends the stop and the target with the order, and closes the
position at the flat-by time if neither was hit. Long only, one trade a day.

Demo account by default. The live account needs both --live and the
environment variable CAPITAL_LIVE=yes. --dry-run logs every decision and
sends no order at all.

Credentials come from environment variables only, never from arguments:
  CAPITAL_API_KEY       the API key (Capital.com: Settings > API integrations)
  CAPITAL_IDENTIFIER    the login email of the account
  CAPITAL_API_PASSWORD  the custom password set for that API key

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
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

DEMO_URL = "https://demo-api-capital.backend-capital.com"
LIVE_URL = "https://api-capital.backend-capital.com"
RESOLUTIONS = {5: "MINUTE_5", 1: "MINUTE"}

OPEN = datetime.time(9, 30)


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

    On a 1:00 p.m. early close both move up by the same gap they keep to
    the regular 4:00 p.m. close.
    """
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    if day.isoformat() not in EARLY_CLOSE:
        return no_entries_after, flat_by
    shift = datetime.timedelta(hours=3)
    move = lambda t: (datetime.datetime.combine(day, t) - shift).time()
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


def opening_range(bars, day, or_minutes, minutes):
    """(high, low) of the bars from 9:30 New York, or None until all of them are in."""
    start = datetime.datetime.combine(day, OPEN, tzinfo=NY)
    end = start + datetime.timedelta(minutes=or_minutes)
    inside = [b for b in bars if start <= b["start"] < end]
    if len(inside) < or_minutes // minutes:
        return None
    return max(b["high"] for b in inside), min(b["low"] for b in inside)


def find_trigger(bars, day, or_minutes, level):
    """The first bar after the range whose close is above the level, or None."""
    end = datetime.datetime.combine(day, OPEN, tzinfo=NY) + datetime.timedelta(minutes=or_minutes)
    for b in bars:
        if b["start"] >= end and b["close"] > level:
            return b
    return None


def plan_trade(ask, or_high, or_low, stop_mode, rr, risk, point_value, min_size, step, decimals):
    """Stop, target and size for a long at the ask. Raises BotError when it does not fit."""
    stop = (or_high + or_low) / 2 if stop_mode == "mid" else or_low
    factor = 10 ** decimals
    stop = math.floor(stop * factor) / factor
    if stop >= ask:
        raise BotError(f"Stop {stop} liegt nicht unter dem Einstieg {ask}")
    dist = ask - stop
    target = math.ceil((ask + rr * dist) * factor) / factor
    raw = risk / (dist * point_value)
    size = round(math.floor(raw / step + 1e-9) * step, 6)
    if size < min_size:
        raise BotError(f"Stop zu weit weg: {dist:.2f} Punkte bei max. Verlust {risk} ergibt "
                       f"Größe {raw:.4f}, das Minimum ist {min_size}. Kein Trade.")
    return {"entry": ask, "stop": stop, "target": target, "size": size,
            "max_loss": round(size * dist * point_value, 2)}


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

    def bars(self, epic, minutes):
        return parse_bars(self._call(
            "GET", f"/api/v1/prices/{epic}?resolution={RESOLUTIONS[minutes]}&max=200")["prices"])

    def positions(self):
        return self._call("GET", "/api/v1/positions").get("positions", [])

    def accounts(self):
        return self._call("GET", "/api/v1/accounts").get("accounts", [])

    def buy(self, epic, size, stop, target):
        ref = self._call("POST", "/api/v1/positions", {
            "epic": epic, "direction": "BUY", "size": size, "guaranteedStop": False,
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
            "status": snap.get("marketStatus"), "currency": market.get("instrument", {}).get("currency")}


def open_deal_ids(api, epic):
    return {p["position"]["dealId"] for p in api.positions() if p["market"]["epic"] == epic}


def sleep_until(t):
    while True:
        left = (t - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60))


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
        f"glattstellen um {de(flat_by)}, max. Verlust {args.risk} pro Trade.")

    deal = next((t for t in state["trades"] if t.get("open")), None)
    if deal is None and len(state["trades"]) >= args.max_trades:
        log("Heute schon gehandelt. Keine weiteren Einstiege.")
        return 0

    sleep_until(or_end + datetime.timedelta(seconds=20))
    rng = None
    while deal is None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= cutoff:
            log("Einstiegsfenster vorbei, kein Ausbruch. Heute kein Trade.")
            return 0
        bars = closed_bars(api.bars(args.epic, args.interval), now, args.interval)
        if rng is None:
            rng = opening_range(bars, day, args.or_minutes, args.interval)
            if rng is None:
                if now > or_end + datetime.timedelta(minutes=15):
                    log("Keine vollständige Opening Range von Capital.com. Abbruch.")
                    return 1
            else:
                log(f"Opening Range: Hoch {rng[0]:.2f}, Tief {rng[1]:.2f}.")
        if rng:
            bar = find_trigger(bars, day, args.or_minutes, rng[0])
            if bar:
                bar_end = bar["start"] + datetime.timedelta(minutes=args.interval)
                if now - bar_end > datetime.timedelta(minutes=args.interval + 2):
                    log(f"Ausbruch schon um {de(bar_end)} – zu alt für einen Einstieg. Kein Trade.")
                    return 0
                log(f"AUSBRUCH: Kerze bis {de(bar_end)} schloss bei {bar['close']:.2f} "
                    f"über dem Hoch {rng[0]:.2f}.")
                deal = enter(api, args, rng, state)
                if deal is None:
                    return 0
                break
            elif bars:
                last = bars[-1]
                log(f"Wartet: letzter Schluss {last['close']:.2f}, "
                    f"{rng[0] - last['close']:.2f} Punkte unter dem Hoch.")
        step = args.interval * 60
        wake = (int(now.timestamp()) // step + 1) * step + 15
        sleep_until(datetime.datetime.fromtimestamp(wake, datetime.timezone.utc))

    return manage(api, args, deal, state, flat_by)


def enter(api, args, rng, state):
    if KILL_SWITCH.exists():
        log(f"Kill-Switch {KILL_SWITCH} ist da. Kein Einstieg.")
        return None
    rules = dealing_rules(api.market(args.epic))
    if rules["status"] != "TRADEABLE":
        log(f"Markt nicht handelbar ({rules['status']}). Kein Einstieg.")
        return None
    try:
        plan = plan_trade(rules["ask"], rng[0], rng[1], args.stop, args.rr, args.risk,
                          args.point_value, rules["min_size"], rules["step"], rules["decimals"])
    except BotError as e:
        log(str(e))
        return None
    log(f"Plan: Kauf {plan['size']} @ ~{plan['entry']}, Stop {plan['stop']}, Ziel {plan['target']}, "
        f"max. Verlust ~{plan['max_loss']} {rules['currency'] or ''}.")
    if args.dry_run:
        log("DRY-RUN: keine Order gesendet.")
        state["trades"].append({**plan, "dry_run": True})
        save_state(state)
        return None
    deal_id, level = api.buy(args.epic, plan["size"], plan["stop"], plan["target"])
    trade = {**plan, "deal_id": deal_id, "fill": level, "open": True,
             "time": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    state["trades"].append(trade)
    save_state(state)
    log(f"GEKAUFT: Deal {deal_id}, Kurs {level}.")
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
    p.add_argument("command", choices=["run", "status", "flatten"])
    p.add_argument("--epic", default="US100", help="Capital.com market (default US100)")
    p.add_argument("--risk", type=float, default=50.0,
                   help="max loss per trade in the instrument's currency (default 50)")
    p.add_argument("--point-value", type=float, default=1.0,
                   help="profit or loss per point for size 1 (default 1)")
    p.add_argument("--stop", choices=["mid", "low"], default="mid",
                   help="stop at the range midpoint or the range low (default mid)")
    p.add_argument("--rr", type=float, default=2.0, help="target in R (default 2)")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--interval", type=int, choices=[1, 5], default=5)
    p.add_argument("--no-entries-after", type=hhmm, default=hhmm("15:30"), help="New York time")
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
    try:
        api.login()
        return {"run": run, "status": status, "flatten": flatten}[args.command](api, args)
    except BotError as e:
        log(f"FEHLER: {e}")
        return 1
    except KeyboardInterrupt:
        log("Abgebrochen. Eine offene Position bleibt mit Stop und Ziel beim Broker stehen.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
