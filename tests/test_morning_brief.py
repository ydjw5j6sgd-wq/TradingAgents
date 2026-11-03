"""Tests for bots/morning_brief.py: the call, the plan and the German text, offline."""

import datetime
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "bots" / "morning_brief.py"
_spec = importlib.util.spec_from_file_location("morning_brief", _PATH)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


@pytest.mark.parametrize("bias, stress, stats, call, warns", [
    ("long", "normal", {"buy_avg_r": 0.3}, "BUY", False),
    ("long", "normal", {"buy_avg_r": -0.2}, "BUY", True),
    ("short", "normal", {"sell_avg_r": 0.4}, "SELL", False),
    ("neutral", "normal", {"avg_r": 0.1}, "BEIDE", False),
    ("long", "hoch", {"buy_avg_r": 0.9}, "ABWARTEN", False),
])
def test_the_call_follows_trend_and_stress(bias, stress, stats, call, warns):
    got, reason = mb.recommend({"bias": bias, "stress": stress}, stats)
    assert got == call and ("Achtung" in reason) == warns


def test_raised_stress_halves_the_size():
    assert "halbe Größe" in mb.recommend({"bias": "long", "stress": "erhöht"}, {})[1]


def test_plan_times_in_german_time_and_points():
    p = mb.plan(datetime.date(2026, 11, 3), 20.0)
    assert (p["open_de"], p["first_signal_de"], p["last_signal_de"], p["flat_de"]) == (
        "15:30", "16:10", "21:05", "21:55")
    assert (p["target_pts"], p["size_per_10_eur"]) == (30.0, 0.5)
    # the weeks when only Europe has left summer time: the US opens at 14:30 German time
    assert mb.plan(datetime.date(2026, 10, 27), 20.0)["open_de"] == "14:30"


def test_event_hints_on_the_first_friday_and_thursdays():
    assert "NFP" in mb.event_hints(datetime.date(2026, 11, 6))[0]
    assert "Erstanträge" in mb.event_hints(datetime.date(2026, 11, 5))[0]
    assert mb.event_hints(datetime.date(2026, 11, 3)) == []


def test_german_text_has_the_call_first():
    stats = {"trades": 3, "sessions": 5, "per_day": 0.6, "win_rate": 0.67, "avg_r": 0.25,
             "worst_streak": 1}
    b = {"date": "2026-11-04", "call": "BUY", "reason": "Aufwärtstrend.",
         "regime": {"stress": "normal", "stress_ratio": 1.1, "atr": 300.0},
         "phase": {"trend": "Aufwärtstrend", "position": "nahe dem 20-Tage-Hoch", "position_pct": 90},
         "levels": {"prev_close": 28000.0, "prev_high": 28100.0, "prev_low": 27900.0,
                    "five_day_high": 28200.0, "five_day_low": 27500.0, "atr": 300.0,
                    "est_open": {"pct": -0.25, "level": 27930.0}},
         "plan": mb.plan(datetime.date(2026, 11, 4), 20.0),
         "replay": {"with_filter": stats, "without_filter": {**stats, "trades": 0}}, "events": []}
    text = mb.german(b)
    assert text.splitlines()[2] == "EMPFEHLUNG: BUY"
    assert "Future über Nacht -0,25 %" in text and "Ø +0,25 R" in text and "keine Trades" in text
