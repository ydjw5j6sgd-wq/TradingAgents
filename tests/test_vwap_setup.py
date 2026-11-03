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


def fresh(bias="long", **extra):
    return {"date": DAY.isoformat(), "bias": bias, "seen": None, "open": None, "trades": [], **extra}


def pullback_day():
    s = rising()
    s[8] = bar(150, 185, 120, 182)
    return s


ANY = {"min_score": 0}  # these tests check the flow, not the score


def test_live_step_signals_only_on_a_fresh_complete_bar_then_closes():
    s = pullback_day()
    state = fresh()
    kind, line = ls.step(state, s, 8, DAY, {**ANY, "rr": 1.5})
    assert kind == "SIGNAL" and line.startswith("BUY (long) NAS100 – VWAP-Pullback, Qualität")
    s[9] = bar(182, 300, 181, 290)
    kind, line = ls.step(state, s, 9, DAY, ANY)
    assert kind == "CLOSED" and "ZIEL erreicht" in line and line.endswith("+1,50 R.")
    assert state["open"] is None and len(state["trades"]) == 1


def test_live_step_respects_the_trend_filter_and_the_limits():
    s = pullback_day()
    assert ls.step(fresh("short"), s, 8, DAY, {**ANY, "trend_filter": True}) is None
    assert ls.step(fresh("short"), s, 8, DAY, ANY)[0] == "SIGNAL"
    full = fresh(seen=7, trades=[{"r": 1.5, "closed_slot": 7}] * 6)
    assert ls.step(full, s, 8, DAY, ANY)[0] == "END"
    late = fresh(seen=66)
    assert ls.step(late, s, 67, DAY, ANY)[0] == "END"


def test_two_losses_in_a_row_pause_for_an_hour_then_signals_resume():
    s = pullback_day()
    half = {**ANY, "risk_pct": 0.5}  # at 1 % a trade, two losses already hit the 2 % day limit
    state = fresh(seen=0, trades=[{"r": -1.0, "closed_slot": 2}, {"r": -1.0, "closed_slot": 5}])
    kind, line = ls.step(state, s, 6, DAY, half)
    assert kind == "PAUSE" and line.startswith("2 Verluste in Folge: Pause bis 17:00 Uhr")
    assert ls.step(state, s, 8, DAY, half) is None  # slot 8 is inside the pause
    later = rising(18)
    later[18] = bar(250, 285, 150, 282)  # dips through the rising VWAP, closes back above it
    assert ls.step(state, later, 18, DAY, half)[0] == "SIGNAL"


def test_the_daily_loss_limit_follows_the_risk_per_trade():
    s = pullback_day()
    trades = [{"r": -1.0, "closed_slot": 2}, {"r": 0.3, "closed_slot": 4}, {"r": -1.0, "closed_slot": 6},
              {"r": -0.4, "closed_slot": 7}]
    kind, line = ls.step(fresh(seen=7, trades=trades), s, 8, DAY, ANY)  # -2.1 R at 1 % = -2.1 %
    assert kind == "END" and line.startswith("Tagesverlustlimit erreicht")
    half = {**ANY, "risk_pct": 0.5, "pause_after": 9}  # -2.1 R at 0.5 % = -1.05 %: trading on
    assert ls.step(fresh(seen=7, trades=trades), s, 8, DAY, half)[0] == "SIGNAL"


def test_no_new_signal_around_news():
    s = pullback_day()
    state = fresh()
    assert ls.step(state, s, 8, DAY, {**ANY, "blackouts": ["16:20"]}) is None
    assert state["skipped"][0]["why"] == "Nachrichten 16:20 Uhr"
    assert ls.step(fresh(), s, 8, DAY, {**ANY, "blackouts": ["16:40"]})[0] == "SIGNAL"


def test_a_low_score_is_no_trade():
    s = pullback_day()
    state = fresh("short")  # against the trend, on an impulsive bar, without context: 50 - 8 - 15
    assert ls.step(state, s, 8, DAY) is None and state["skipped"][0]["why"] == "Score 27"


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


def test_signal_line_follows_the_rulebook_format():
    sig = {"slot": 8, "side": "SELL", "entry": 100.0, "stop": 110.0, "target": 80.0, "risk": 10.0,
           "rr": 2.0, "vwap": 101.0, "setup": "vwap", "score": 64,
           "trends": {"4H": "short", "2H": "short", "1H": "long", "15M": "neutral", "5M": "short"},
           "context": {"rel_volume": 0.74, "open_volume": None, "quality": "mittel"}}
    text = ls.signal_line(DAY, sig, "short", {"account": 2000, "risk_pct": 1, "eur_per_point": 1})
    lines = text.splitlines()
    assert lines[0] == "SELL (short) NAS100 – VWAP-Pullback, Qualität 64/100"
    for label in ("MARKTRICHTUNG: Short", "4H: abwärts", "1H: aufwärts", "15M: seitwärts",
                  "SETUP: Bestätigt (5M-Kerzenschluss 16:15 Uhr)", "EINSTIEG: 98,5 bis 100,0",
                  "STOP-LOSS: 110,0 (+10,0 Pkt)", "TP1: 90,0", "TP2: 80,0", "TP3: 70,0",
                  "CHANCE-RISIKO: 1:2,0", "INVALIDIERUNG: 5M-Schluss über dem VWAP 101,0",
                  "SETUP-QUALITÄT: 64/100", "- Zeitebenen: 3 von 5 mit dem Signal, 1 dagegen (mit dem Tagestrend)",
                  "- Volumen: Signalkerze 0,7x üblich, Eröffnung ?, Stufe MITTEL",
                  "POSITIONSGRÖSSE: 2,00 (Risiko 20,0 € = 1,0 % bei 10,0 Pkt Stop)"):
        assert any(line.startswith(label) for line in lines), label
    assert "Kontostand und Kontraktwert" in ls.signal_line(DAY, sig, "short")


def test_end_of_entries_uses_the_day_s_german_time():
    late = {"date": "2026-10-28", "bias": "long", "seen": 66, "open": None, "trades": []}
    # in the week Europe has left summer time and the US has not, entries end at 20:05
    assert ls.step(late, rising(), 67, datetime.date(2026, 10, 28))[1].startswith("20:05 Uhr")


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
    s = pullback_day()
    state = fresh()
    assert ls.step(state, s, 8, DAY, ANY)[0] == "SIGNAL"
    risk = state["open"]["risk"]
    s[9] = bar(182, 182 + 0.6 * risk, 181, 182 + 0.5 * risk)
    kind, line = ls.step(state, s, 9, DAY, ANY)
    assert kind == "MOVE" and line.startswith("STOP NACHZIEHEN – BUY von 16:15 Uhr")
    assert ls.step(state, s, 9, DAY, ANY) is None
    s[10] = bar(200, 201, 150, 160)
    kind, line = ls.step(state, s, 10, DAY, ANY)
    assert kind == "CLOSED" and "nachgezogener Stop erreicht" in line and state["trades"][0]["r"] > 0


def quiet_history(vol=100, days=20):
    return {f"2026-10-{d:02d}": flat_session(vol=vol) for d in range(1, days + 1)}


def test_sweep_sells_a_failed_break_of_the_hour_high_on_heavy_volume():
    s = {k: bar(100, 110, 90, 100) for k in range(20)}
    s[20] = bar(105, 130, 100, 101, v=200)  # runs 110, closes back under it and under its open
    sig = vs.sweep(s, 20, quiet_history())
    assert sig["side"] == "SELL" and sig["level"] == 110 and sig["volume"] == pytest.approx(2.0)
    assert sig["stop"] == pytest.approx(133) and sig["setup"] == "sweep"


def test_sweep_needs_the_volume_and_the_close_back_inside():
    s = {k: bar(100, 110, 90, 100) for k in range(20)}
    s[20] = bar(105, 130, 100, 101, v=100)
    assert vs.sweep(s, 20, quiet_history()) is None  # usual volume
    s[20] = bar(105, 130, 100, 115, v=200)
    assert vs.sweep(s, 20, quiet_history()) is None  # closed above the old high: a breakout
    s[20] = bar(95, 100, 70, 96, v=200)
    assert vs.sweep(s, 20, quiet_history())["side"] == "BUY"
    assert vs.sweep(s, 20, None) is None


def test_volume_levels_find_poc_value_area_and_spaced_clusters():
    s1 = {0: bar(0, 101, 99, 100, 500), 1: bar(0, 111, 109, 110, 300), 2: bar(0, 201, 199, 200, 400),
          3: bar(0, 91, 89, 90, 50)}
    lv = vs.volume_levels([s1], share=0.5)
    assert (lv["poc"], lv["val"], lv["vah"]) == (100, 100, 110)
    assert lv["nodes"] == [100, 200]  # 110 is no peak of its own and sits too close to 100
    assert vs.nearest_clusters(lv, 150) == (200, 110)
    assert vs.volume_levels([]) is None and vs.nearest_clusters(None, 1) == (None, None)


def test_live_follows_an_open_signal_on_one_minute_bars():
    s = pullback_day()
    state = fresh()
    assert ls.step(state, s, 8, DAY, ANY, minutes=[])[0] == "SIGNAL"
    open_ts = datetime.datetime.combine(DAY, datetime.time(9, 30), tzinfo=ls.NY).timestamp()
    minutes = [(open_ts + 9 * 300, bar(182, 183, 100, 101))]  # the stop in the first minute after the entry
    kind, line = ls.step(state, s, 8, DAY, ANY, minutes=minutes)
    assert kind == "CLOSED" and "STOP erreicht um 16:16 Uhr" in line


def test_signal_line_describes_a_sweep_and_the_clusters():
    sig = {"slot": 20, "side": "SELL", "entry": 101.0, "stop": 133.0, "target": 37.0, "risk": 32.0,
           "rr": 2.0, "level": 110.0, "volume": 2.0, "setup": "sweep", "clusters": (150.0, None), "fomo": True}
    text = ls.signal_line(DAY, sig, "long")
    assert "Fehlausbruch (Liquidity Sweep)" in text and "die Käufer verlieren den Boden" in text
    assert "- Volumen-Cluster darüber 150,0" in text and "FOMO-Regel" in text
    assert "INVALIDIERUNG: 5M-Schluss wieder über 110,0" in text