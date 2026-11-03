"""Tests for bots/vwap_setup.py and bots/live_signals.py: the NAS100 VWAP pullback, offline."""

import datetime
import importlib.util
from pathlib import Path

import pytest

_BOTS = Path(__file__).resolve().parent.parent / "bots"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _BOTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vs = _load("vwap_setup")
ls = _load("live_signals")
DAY = datetime.date(2026, 11, 3)


def bar(o, h, l, c, v=100):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def rising(n=8, start=100.0, step=10.0):
    """n bars climbing steadily, each closing well above the VWAP."""
    return {k: bar(start + k * step, start + k * step + 5, start + k * step - 5, start + k * step + 4)
            for k in range(n)}


def test_vwap_is_volume_weighted_typical_price():
    vw = vs.session_vwap({0: bar(0, 12, 6, 9, 100), 1: bar(0, 30, 24, 27, 300)})
    assert vw[0] == pytest.approx(9) and vw[1] == pytest.approx((9 * 100 + 27 * 300) / 400)


def test_buy_on_a_dip_to_a_rising_vwap_that_closes_back_above():
    s = rising()
    s[8] = bar(150, 185, 120, 182)  # dips through the VWAP, closes above it
    sig = vs.pullback(s, 8)
    assert sig["side"] == "BUY" and sig["entry"] == 182
    assert sig["stop"] == pytest.approx(120 - 6.5)
    assert sig["target"] == pytest.approx(182 + 1.5 * (182 - 113.5))


def test_no_signal_without_the_dip_or_with_a_tight_stop():
    s = rising()
    s[8] = bar(180, 190, 178, 188)  # never touches the VWAP
    assert vs.pullback(s, 8) is None
    s = rising()
    s[8] = bar(150, 160, 150, 159)
    assert vs.pullback(s, 8, min_risk=1000) is None


def test_sell_mirrors_on_a_falling_vwap():
    s = {k: bar(300 - k * 10, 305 - k * 10, 295 - k * 10, 296 - k * 10) for k in range(8)}
    s[8] = bar(240, 290, 210, 213)  # pokes up through the VWAP, closes under it
    sig = vs.pullback(s, 8)
    assert sig["side"] == "SELL" and sig["stop"] == pytest.approx(290 + 8)


def test_outcome_stop_first_target_and_flat():
    sig = {"slot": 8, "side": "BUY", "entry": 100, "stop": 90, "target": 115, "risk": 10, "rr": 1.5}
    assert vs.outcome({9: bar(100, 120, 85, 110)}, sig)[:2] == ("stop", -1.0)
    assert vs.outcome({9: bar(100, 116, 95, 110)}, sig)[:2] == ("target", 1.5)
    assert vs.outcome({9: bar(100, 105, 95, 104)}, sig, last_slot=9) == (None, None, None)
    flat = vs.outcome({9: bar(100, 105, 95, 104), 76: bar(104, 106, 101, 105)}, sig)
    assert flat == ("flat", 0.5, 76)


def test_backtest_charges_the_spread_per_trade():
    s = rising(70)
    s[8] = bar(150, 185, 120, 182)
    stats = vs.backtest({"2026-11-02": s}, {"2026-11-02": "long"}, cost=2.0)
    assert stats["trades"] >= 1 and stats["sessions"] == 1


def test_live_step_signals_only_on_a_fresh_complete_bar_then_closes():
    s = rising()
    s[8] = bar(150, 185, 120, 182)
    state = {"date": DAY.isoformat(), "bias": "long", "seen": None, "open": None, "trades": []}
    kind, line = ls.step(state, s, 8, DAY, True, 1.5, 15, 3)
    assert kind == "SIGNAL" and line.startswith("BUY (long) NAS100 – Kerze bis 16:15 Uhr")
    s[9] = bar(182, 300, 181, 290)
    kind, line = ls.step(state, s, 9, DAY, True, 1.5, 15, 3)
    assert kind == "CLOSED" and "ZIEL erreicht" in line and line.endswith("+1,50 R.")
    assert state["open"] is None and len(state["trades"]) == 1


def test_live_step_respects_the_trend_filter_and_the_daily_limit():
    s = rising()
    s[8] = bar(150, 185, 120, 182)
    state = {"date": DAY.isoformat(), "bias": "short", "seen": None, "open": None, "trades": []}
    assert ls.step(state, s, 8, DAY, True, 1.5, 15, 3) is None
    state = {"date": DAY.isoformat(), "bias": "short", "seen": None, "open": None, "trades": []}
    assert ls.step(state, s, 8, DAY, False, 1.5, 15, 3)[0] == "SIGNAL"
    full = {"date": DAY.isoformat(), "bias": "long", "seen": 7, "open": None, "trades": [{"r": 1.5}] * 3}
    assert ls.step(full, s, 8, DAY, True, 1.5, 15, 3)[0] == "END"
    stopped = {"date": DAY.isoformat(), "bias": "long", "seen": 7, "open": None,
               "trades": [{"r": -1.0}, {"r": 1.5}, {"r": -1.0}, {"r": -1.0}]}
    kind, line = ls.step(stopped, s, 8, DAY, True, 1.5, 15, 6)
    assert kind == "END" and line.startswith("3 Verluste heute: Pause")
    two = {"date": DAY.isoformat(), "bias": "long", "seen": 7, "open": None, "trades": [{"r": -1.0}] * 2}
    assert ls.step(two, s, 8, DAY, True, 1.5, 15, 6)[0] == "SIGNAL"
    late = {"date": DAY.isoformat(), "bias": "long", "seen": 66, "open": None, "trades": []}
    assert ls.step(late, s, 67, DAY, True, 1.5, 15, 3)[0] == "END"


def test_complete_slot_needs_yahoo_data_past_the_bar_end():
    s = {0: {"t": 1000}, 1: {"t": 1300}}
    assert ls.complete_slot(s, data_time=1650, now=2000) == 1
    assert ls.complete_slot(s, data_time=1500, now=2000) == 0
    assert ls.complete_slot(s, data_time=1200, now=2000) == -1


def flat_session(close=100.0, vol=100):
    return {k: bar(close, close + 1, close - 1, close, vol) for k in range(78)}


def test_higher_bars_stop_at_the_signal_and_skip_incomplete_groups():
    sessions = {"2026-11-02": flat_session(), "2026-11-03": {k: bar(0, 1, 0, k) for k in range(10)}}
    closes = vs.higher_bars(sessions, 3, upto=("2026-11-03", 7))
    assert closes[-2:] == [2, 5] and len(closes) == 26 + 2
    assert len(vs.higher_bars(sessions, 9)) == 8 + 1


def test_trend_of_needs_a_sloping_ema():
    assert vs.trend_of(list(range(40))) == "long"
    assert vs.trend_of(list(range(40, 0, -1))) == "short"
    assert vs.trend_of([5] * 40) == "neutral"
    assert vs.trend_of([1, 2, 3]) == "neutral"


def test_volume_is_compared_with_the_same_time_of_day():
    history = {f"2026-10-{d:02d}": flat_session(vol=100) for d in range(1, 21)}
    today = flat_session(vol=50)
    assert vs.relative_volume(history, today, 10) == pytest.approx(0.5)
    assert vs.opening_volume(history, today) == pytest.approx(0.5)
    ctx = vs.signal_context(history, "2026-10-21", today, {"slot": 10})
    assert ctx["quality"] == "hoch" and ctx["trend_15m"] == "neutral"
    busy = vs.signal_context(history, "2026-10-21", flat_session(vol=300), {"slot": 10})
    assert busy["quality"] == "normal"


def test_signal_line_shows_the_context():
    sig = {"slot": 8, "side": "SELL", "entry": 100.0, "stop": 110.0, "target": 85.0, "risk": 10.0,
           "rr": 1.5, "vwap": 101.0, "context": {"trend_15m": "long", "trend_45m": "short",
                                                  "rel_volume": 0.74, "open_volume": None, "quality": "mittel"}}
    line = ls.signal_line(DAY, sig, "short")
    assert "15 Min aufwärts, 45 Min abwärts" in line and "Signalkerze 0,7x" in line
    assert "Eröffnung ?" in line and "Qualität MITTEL" in line and "Tagestrend abwärts" in line


def test_end_of_entries_uses_the_day_s_german_time():
    late = {"date": "2026-10-28", "bias": "long", "seen": 66, "open": None, "trades": []}
    # in the week Europe has left summer time and the US has not, entries end at 20:05
    assert ls.step(late, rising(), 67, datetime.date(2026, 10, 28), True, 1.5, 15, 3)[1].startswith("20:05 Uhr")


SIG = {"slot": 8, "side": "BUY", "entry": 100.0, "stop": 90.0, "target": 120.0, "risk": 10.0, "rr": 2.0}


def test_follow_moves_the_stop_to_the_entry_at_half_r_then_trails():
    s = {9: bar(100, 106, 99, 105), 10: bar(105, 112, 104, 111), 11: bar(111, 111, 106, 107)}
    f = vs.follow(s, SIG, last_slot=9, be_at=0.5, trail=0.5)
    assert f["result"] is None and f["moved"] and f["stop"] == pytest.approx(101)  # entry + 0.1 R = 101, and 106 - 5 = 101 behind the best price
    f = vs.follow(s, SIG, last_slot=10, be_at=0.5, trail=0.5)
    assert f["stop"] == pytest.approx(107)  # trails 5 behind the best price 112
    f = vs.follow(s, SIG, last_slot=11, be_at=0.5, trail=0.5)
    assert (f["result"], f["r"]) == ("trail", pytest.approx(0.7))


def test_follow_without_the_move_is_the_plain_outcome():
    s = {9: bar(100, 106, 99, 105), 10: bar(105, 106, 89, 95)}
    assert vs.follow(s, SIG)["result"] == "stop" and vs.outcome(s, SIG)[:2] == ("stop", -1.0)
    moved = vs.follow(s, SIG, be_at=0.5)
    assert (moved["result"], moved["r"]) == ("trail", pytest.approx(0.1))


def test_live_sends_one_move_push_then_closes_on_the_trailed_stop():
    s = rising()
    s[8] = bar(150, 185, 120, 182)
    state = {"date": DAY.isoformat(), "bias": "long", "seen": None, "open": None, "trades": []}
    assert ls.step(state, s, 8, DAY, True, 2.0, 15, 6)[0] == "SIGNAL"
    risk = state["open"]["risk"]
    s[9] = bar(182, 182 + 0.6 * risk, 181, 182 + 0.5 * risk)
    kind, line = ls.step(state, s, 9, DAY, True, 2.0, 15, 6)
    assert kind == "MOVE" and line.startswith("STOP NACHZIEHEN – BUY von 16:15 Uhr")
    assert ls.step(state, s, 9, DAY, True, 2.0, 15, 6) is None
    s[10] = bar(200, 201, 150, 160)
    kind, line = ls.step(state, s, 10, DAY, True, 2.0, 15, 6)
    assert kind == "CLOSED" and "nachgezogener Stop erreicht" in line and state["trades"][0]["r"] > 0
