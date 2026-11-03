"""Tests for bots/morning_brief.py: the replay of the bot's rules and the morning call, offline."""

import datetime
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "bots" / "morning_brief.py"
_spec = importlib.util.spec_from_file_location("morning_brief", _PATH)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def session(*later):
    """Opening range 100-120 (mid 110, width 20), then the given bars from slot 3 on."""
    s = {0: bar(110, 120, 105, 115), 1: bar(115, 118, 100, 104), 2: bar(104, 112, 102, 111)}
    s.update({3 + i: b for i, b in enumerate(later)})
    return s


def test_long_breakout_reaches_the_measured_move():
    # entry 121.5 (clears 120 + 1), stop 110, target 140: rr 18.5 / 11.5 = 1.61
    out = mb.replay_session(session(bar(115, 122, 114, 121.5), bar(121, 141, 120, 140)), "long")
    assert out["result"] == "target" and out["r"] == pytest.approx(18.5 / 11.5)


def test_a_bar_touching_stop_and_target_counts_as_the_stop():
    out = mb.replay_session(session(bar(115, 122, 114, 121.5), bar(121, 141, 109, 130)), "long")
    assert out == {"result": "stop", "r": -1.0, "rr": pytest.approx(18.5 / 11.5)}


def test_a_late_breakout_is_skipped_and_a_thin_one_ignored():
    assert mb.replay_session(session(bar(115, 126, 114, 125)), "long")["result"] == "skipped"
    assert mb.replay_session(session(bar(115, 121, 114, 120.5)), "long") == {"result": "none"}


def test_short_mirrors_and_exits_flat_at_the_close():
    out = mb.replay_session(session(bar(105, 106, 97, 98.5), bar(98, 100, 96, 97)), "short")
    # entry 98.5, stop 110, target 80, never hit: flat at 97 -> (98.5 - 97) / 11.5
    assert out["result"] == "flat" and out["r"] == pytest.approx(1.5 / 11.5)


def test_a_wide_range_is_skipped_and_a_partial_one_ignored():
    assert mb.replay_session(session(), "long", max_width=10) == {"result": "wide"}
    assert mb.replay_session({0: bar(1, 2, 0, 1)}, "long") is None


STATS = lambda trades, avg: {"trades": trades, "avg_r": avg}


@pytest.mark.parametrize("bias, stress, long, short, call", [
    ("long", "normal", STATS(5, 0.4), STATS(5, -0.2), "LONG"),
    ("long", "normal", STATS(5, -0.1), STATS(5, 0.3), "ABWARTEN"),
    ("short", "erhöht", STATS(5, -0.1), STATS(4, 0.2), "SHORT"),
    ("neutral", "normal", STATS(5, 0.2), STATS(5, 0.1), "BEIDE"),
    ("neutral", "normal", STATS(2, 0.9), STATS(5, -0.1), "ABWARTEN"),
    ("long", "hoch", STATS(9, 0.9), STATS(9, 0.9), "ABWARTEN"),
])
def test_the_call_needs_trend_and_a_positive_record(bias, stress, long, short, call):
    assert mb.recommend({"bias": bias, "stress": stress}, long, short)[0] == call


def test_rr_window_in_points():
    w = mb.rr_window(100)
    assert (w["risk"], w["reward"], w["buffer_pts"]) == pytest.approx((55, 95, 5))
    assert w["latest_pts"] == pytest.approx(10)  # (1 - 0.1) / (0.5 + 0.1) = 1.5


def test_event_hints_on_the_first_friday_and_thursdays():
    assert "NFP" in mb.event_hints(datetime.date(2026, 11, 6))[0]
    assert "Erstanträge" in mb.event_hints(datetime.date(2026, 11, 5))[0]
    assert mb.event_hints(datetime.date(2026, 11, 3)) == []
