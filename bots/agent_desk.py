#!/usr/bin/env python3
"""Morning call from the TradingAgents desk for the Capital.com bot.

Runs the full TradingAgents team (analysts, bull and bear researchers,
trader, risk team, portfolio manager) on one ticker, QQQ by default as the
stand-in for the Nasdaq 100, and saves the portfolio manager's rating as
today's direction for bots/capital_orb.py --agents:

  Buy, Overweight      -> long only
  Underweight, Sell    -> short only
  Hold, or no rating   -> no trade today

The models and the provider come from the usual TRADINGAGENTS_* variables
(for example TRADINGAGENTS_LLM_PROVIDER=anthropic) and the provider's key.

  python3 bots/agent_desk.py                # today's call on QQQ
  python3 bots/agent_desk.py --ticker SPY
  python3 bots/agent_desk.py --show         # print the saved call, run nothing
"""

import argparse
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
VIEW_FILE = ROOT / "trader" / "state" / "agent_view.json"
NY = ZoneInfo("America/New_York")

RATING_BIAS = {"Buy": "long", "Overweight": "long", "Hold": "none",
               "Underweight": "short", "Sell": "short"}
WORDS = {"long": "nur LONG", "short": "nur SHORT", "none": "KEIN TRADE"}


def bias_for(rating):
    """The bot's direction for a portfolio-manager rating; anything unreadable is no trade."""
    return RATING_BIAS.get(rating, "none")


def build_view(ticker, day, rating, decision, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {"date": day, "ticker": ticker, "rating": rating, "bias": bias_for(rating),
            "created_utc": now.isoformat(timespec="seconds"), "decision": (decision or "")[:2000]}


def save_view(view, path=None):
    path = path or VIEW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(view, indent=2, ensure_ascii=False))


def describe(view):
    return (f"Agenten-Desk {view['ticker']} für {view['date']}: Rating {view['rating']} "
            f"-> {WORDS[view['bias']]}.")


def run_desk(ticker, day):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
    state, rating = graph.propagate(ticker, day)
    return rating, state.get("final_trade_decision", "")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ticker", default="QQQ", help="ticker the desk analyses (default QQQ)")
    p.add_argument("--date", help="run date YYYY-MM-DD (default: today in New York)")
    p.add_argument("--show", action="store_true", help="print the saved call and exit")
    args = p.parse_args(argv)

    if args.show:
        try:
            view = json.loads(VIEW_FILE.read_text())
        except (OSError, ValueError):
            print("Noch keine gespeicherte Einschätzung.", file=sys.stderr)
            return 1
        print(describe(view))
        print(view["decision"])
        return 0

    day = args.date or datetime.datetime.now(NY).date().isoformat()
    print(f"Agenten-Desk analysiert {args.ticker} für {day}. Das dauert einige Minuten ...", flush=True)
    rating, decision = run_desk(args.ticker, day)
    view = build_view(args.ticker, day, rating, decision)
    save_view(view)
    print(describe(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
