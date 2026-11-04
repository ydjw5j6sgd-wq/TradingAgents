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
        "15:30", "16:10", "21:00", "21:55")
    assert (p["target_pts"], p["size_per_10_eur"]) == (40.0, 0.5)
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


def b4(h, l, c=None):
    return {"h": h, "l": l, "c": (h + l) / 2 if c is None else c}


def test_ema_starts_at_the_first_value_and_follows():
    assert mb.ema([10, 10, 10], 45) == [10, 10, 10]
    line = mb.ema([0, 46], 45)
    assert line[1] == pytest.approx(2.0)


def test_swings_find_pivot_highs_and_lows():
    bars = [b4(10, 5), b4(11, 6), b4(12, 7), b4(20, 8), b4(13, 7), b4(12, 6), b4(11, 1), b4(12, 2),
            b4(13, 3), b4(14, 4)]
    highs, lows = mb.swings(bars)
    assert highs == [(3, 20)] and lows == [(6, 1)]


def test_structure_turns_bearish_on_a_close_under_the_last_swing_low():
    up = [b4(100 + i, 90 + i) for i in range(4)]
    bars = up + [b4(110, 100), b4(106, 96), b4(105, 95), b4(104, 85), b4(106, 94), b4(107, 95),
                 b4(108, 96), b4(103, 90), b4(99, 80, 81)]
    st = mb.structure(bars)
    assert st["bias"] == "short" and st["bos"] == 85 and st["choch"] == 110


def test_nearest_levels_and_point_of_control():
    bars = [b4(10, 5), b4(11, 6), b4(12, 7), b4(20, 8), b4(13, 7), b4(12, 6), b4(11, 1), b4(12, 2),
            b4(13, 3), b4(14, 4)]
    assert mb.nearest_levels(bars, 10) == (20, 1)
    session = {0: {"h": 101, "l": 99, "c": 100, "v": 10}, 1: {"h": 121, "l": 119, "c": 120, "v": 50}}
    assert mb.point_of_control(session) == 120


def test_chart_context_needs_enough_bars_and_applies_the_basis():
    assert mb.chart_context([b4(1, 0)] * 10, 0, None) is None
    bars = [{"h": 1010 + i, "l": 1000 + i, "c": 1005 + i} for i in range(80)]
    ctx = mb.chart_context(bars, 100.0, None)
    assert ctx["price"] == 984 and ctx["ema_rising"] and ctx["poc"] is None


def flat(close=100.0, vol=100, n=78):
    return {k: {"o": close, "h": close + 1, "l": close - 1, "c": close, "v": vol} for k in range(n)}


def test_timeframes_read_the_last_session_and_its_volume():
    sessions = {f"2026-10-{d:02d}": flat() for d in range(1, 21)}
    sessions["2026-10-21"] = {k: {"o": k, "h": k + 1, "l": k - 1, "c": float(k), "v": 200} for k in range(78)}
    tf = mb.timeframes(sessions)
    assert tf["5m"] == "long" and tf["15m"] == "long" and tf["volume"] == pytest.approx(2.0)
    assert mb.timeframes({}) is None


def test_quality_record_groups_by_grade():
    assert mb.quality_record({"2026-10-01": flat()}, {}) == {}
