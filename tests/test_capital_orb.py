"""Tests for bots/capital_orb.py: the breakout rule, sizing and the API client, offline."""

import datetime
import importlib.util
import io
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "bots" / "capital_orb.py"
_spec = importlib.util.spec_from_file_location("capital_orb", _PATH)
orb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orb)

DAY = datetime.date(2026, 11, 3)  # a Tuesday; New York is UTC-5


def row(utc, o, h, l, c, spread=1.0):
    p = lambda v: {"bid": v - spread / 2, "ask": v + spread / 2}
    return {"snapshotTimeUTC": utc, "openPrice": p(o), "highPrice": p(h),
            "lowPrice": p(l), "closePrice": p(c)}


ROWS = [
    row("2026-11-03T14:30:00", 100, 110, 95, 105),   # 9:30 NY
    row("2026-11-03T14:35:00", 105, 112, 100, 108),  # 9:35
    row("2026-11-03T14:40:00", 108, 109, 98, 101),   # 9:40 -> range 112 / 95
    row("2026-11-03T14:45:00", 101, 115, 100, 111),  # wick above, closes below
    row("2026-11-03T14:50:00", 111, 116, 110, 114),  # first close above 112
    row("2026-11-03T14:55:00", 114, 120, 113, 119),
]


def test_parse_bars_uses_mid_prices_in_time_order():
    bars = orb.parse_bars(list(reversed(ROWS)))
    assert bars[0]["start"] == datetime.datetime(2026, 11, 3, 14, 30, tzinfo=datetime.timezone.utc)
    assert bars[0]["high"] == 110 and bars[0]["close"] == 105


def test_opening_range_needs_every_bar():
    bars = orb.parse_bars(ROWS)
    assert orb.opening_range(bars, DAY, 15, 5) == (112, 95)
    assert orb.opening_range(bars[:2], DAY, 15, 5) is None


def test_trigger_is_first_close_above_not_a_wick():
    bars = orb.parse_bars(ROWS)
    bar, direction = orb.find_trigger(bars, DAY, 15, 112, 95, {"BUY"})
    assert (bar["close"], direction) == (114, "BUY")
    assert orb.find_trigger(bars, DAY, 15, 200, 95, {"BUY"}) == (None, None)


def test_trigger_short_below_the_low_and_only_when_allowed():
    rows = ROWS[:3] + [row("2026-11-03T14:45:00", 100, 101, 90, 93)]
    bars = orb.parse_bars(rows)
    bar, direction = orb.find_trigger(bars, DAY, 15, 112, 95, {"BUY", "SELL"})
    assert (bar["close"], direction) == (93, "SELL")
    assert orb.find_trigger(bars, DAY, 15, 112, 95, {"BUY"}) == (None, None)


def test_trigger_buffer_needs_a_clear_close():
    bars = orb.parse_bars(ROWS)
    bar, _ = orb.find_trigger(bars, DAY, 15, 112, 95, {"BUY", "SELL"}, buffer=3)
    assert bar["close"] == 119


def test_closed_bars_drops_the_forming_bar():
    bars = orb.parse_bars(ROWS)
    now = datetime.datetime(2026, 11, 3, 14, 57, tzinfo=datetime.timezone.utc)
    assert len(orb.closed_bars(bars, now, 5)) == 5


def test_plan_trade_sizes_a_long_to_the_max_loss():
    plan = orb.plan_trade("BUY", 114.0, 112, 95, "mid", "r", 2, 1.5, 50, 1, 0.1, 0.1, 1)
    assert (plan["stop"], plan["target"], plan["size"]) == (103.5, 135.0, 4.7)
    assert plan["max_loss"] <= 50


def test_plan_trade_mirrors_a_short():
    plan = orb.plan_trade("SELL", 93.0, 112, 95, "mid", "r", 2, 1.5, 50, 1, 0.1, 0.1, 1)
    assert (plan["stop"], plan["target"], plan["size"]) == (103.5, 72.0, 4.7)
    edge = orb.plan_trade("SELL", 93.0, 112, 95, "edge", "r", 2, 1.5, 50, 1, 0.1, 0.1, 1)
    assert edge["stop"] == 112


def test_plan_trade_aims_at_the_measured_move():
    plan = orb.plan_trade("BUY", 113.0, 112, 95, "mid", "measured", 2, 1.5, 50, 1, 0.1, 0.1, 1)
    assert (plan["stop"], plan["target"], plan["rr"]) == (103.5, 129.0, 1.68)
    short = orb.plan_trade("SELL", 94.0, 112, 95, "mid", "measured", 2, 1.5, 50, 1, 0.1, 0.1, 1)
    assert (short["stop"], short["target"], short["rr"]) == (103.5, 78.0, 1.68)


def test_plan_trade_refuses_a_late_entry_under_the_minimum_reward_risk():
    with pytest.raises(orb.BotError, match="Zu spät"):
        orb.plan_trade("BUY", 116.0, 112, 95, "mid", "measured", 2, 1.5, 50, 1, 0.1, 0.1, 1)


def test_plan_trade_refuses_a_stop_too_far_for_the_risk():
    with pytest.raises(orb.BotError, match="Kein Trade"):
        orb.plan_trade("BUY", 114.0, 112, 95, "edge", "r", 2, 1.5, 5, 1, 1, 1, 1)


def test_complete_bars_waits_for_the_next_bar():
    bars = orb.parse_bars(ROWS)
    late = datetime.datetime(2026, 11, 3, 15, 30, tzinfo=datetime.timezone.utc)
    assert [b["close"] for b in orb.complete_bars(bars, late, 5)][-1] == 114
    assert len(orb.complete_bars(bars[:4], late, 5)) == 3


def daily(closes, spread):
    """Daily bars from closes, each with the given high-low range."""
    start = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    return [{"start": start + datetime.timedelta(days=i), "open": c, "high": c + r / 2,
             "low": c - r / 2, "close": c} for i, (c, r) in enumerate(zip(closes, spread))]


def test_regime_reads_trend_and_stress():
    up = orb.market_regime(daily(range(100, 130), [10] * 30))
    assert (up["bias"], up["stress"]) == ("long", "normal")
    down = orb.market_regime(daily(range(130, 100, -1), [10] * 25 + [20] * 5))
    assert down["bias"] == "short" and down["stress"] == "erhöht"
    calm_then_wild = orb.market_regime(daily([100] * 30, [10] * 25 + [40] * 5))
    assert calm_then_wild["bias"] == "neutral" and calm_then_wild["stress"] == "hoch"
    with pytest.raises(orb.BotError, match="Zu wenig"):
        orb.market_regime(daily([100] * 10, [10] * 10))


def test_sides_follow_the_bias_unless_overridden():
    assert orb.allowed_sides("auto", "long") == {"BUY"}
    assert orb.allowed_sides("auto", "short") == {"SELL"}
    assert orb.allowed_sides("auto", "neutral") == {"BUY", "SELL"}
    assert orb.allowed_sides("short", "long") == {"SELL"}


def test_stress_scales_the_risk():
    assert orb.risk_for({"stress": "normal"}, 50) == 50
    assert orb.risk_for({"stress": "erhöht"}, 50) == 25
    assert orb.risk_for({"stress": "hoch"}, 50) == 0


def test_range_and_target_filters():
    regime = {"atr": 40.0}
    assert orb.range_problem((112, 95), regime, 0.5) is None
    assert "zu unruhig" in orb.range_problem((130, 95), regime, 0.5).lower()
    near = {"entry": 114.0, "target": 135.0}
    far = {"entry": 114.0, "target": 160.0}
    assert orb.target_problem(near, regime, 1.0) is None
    assert "Unrealistisch" in orb.target_problem(far, regime, 1.0)


def test_session_times_on_a_normal_day_a_holiday_and_an_early_close():
    t = lambda s: datetime.time.fromisoformat(s)
    assert orb.session_times(DAY, t("15:30"), t("15:55")) == (t("15:30"), t("15:55"))
    assert orb.session_times(datetime.date(2026, 11, 7), t("15:30"), t("15:55")) is None
    assert orb.session_times(datetime.date(2026, 11, 26), t("15:30"), t("15:55")) is None
    assert orb.session_times(datetime.date(2026, 11, 27), t("15:30"), t("15:55")) == (
        t("12:30"), t("12:55"))
    assert orb.session_times(datetime.date(2026, 11, 27), t("11:30"), t("15:55")) == (
        t("11:30"), t("12:55"))


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_logs_in_and_places_a_bracket_order(monkeypatch):
    monkeypatch.setattr(orb.time, "sleep", lambda s: None)
    sent = []

    def opener(req, timeout):
        sent.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.full_url.endswith("/session"):
            return FakeResponse({}, {"CST": "c", "X-SECURITY-TOKEN": "x"})
        if req.get_method() == "POST":
            return FakeResponse({"dealReference": "o_1"})
        return FakeResponse({"dealStatus": "ACCEPTED", "level": 114.2,
                             "affectedDeals": [{"dealId": "d_1", "status": "OPENED"}]})

    api = orb.Capital(orb.DEMO_URL, "key", "me@example.com", "pw", opener=opener)
    api.login()
    assert api.order("US100", "BUY", 4.7, 103.5, 135.0) == ("d_1", 114.2)
    method, url, headers, data = sent[1]
    assert (method, url) == ("POST", orb.DEMO_URL + "/api/v1/positions")
    assert headers["Cst"] == "c" and headers["X-security-token"] == "x"
    assert json.loads(data) == {"epic": "US100", "direction": "BUY", "size": 4.7,
                                "guaranteedStop": False, "stopLevel": 103.5, "profitLevel": 135.0}


def test_client_raises_on_a_rejected_order(monkeypatch):
    monkeypatch.setattr(orb.time, "sleep", lambda s: None)

    def opener(req, timeout):
        if req.get_method() == "POST":
            return FakeResponse({"dealReference": "o_1"})
        return FakeResponse({"dealStatus": "REJECTED", "reason": "RISK_CHECK"})

    api = orb.Capital(orb.DEMO_URL, "key", "me@example.com", "pw", opener=opener)
    with pytest.raises(orb.BotError, match="RISK_CHECK"):
        api.order("US100", "SELL", 1, 120, 100)


def test_live_needs_the_extra_environment_variable(monkeypatch, capsys):
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("CAPITAL_LIVE", raising=False)
    assert orb.main(["run", "--live"]) == 2
    assert "CAPITAL_LIVE=yes" in capsys.readouterr().err


class Clock:
    """A fake clock: time.sleep moves it forward, datetime.now reads it."""

    def __init__(self, start):
        self.now = start
        clock = self

        class FakeDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

        self.datetime = FakeDatetime

    def sleep(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)


class FakeBroker:
    """Serves daily and 5-minute bars up to the clock, takes orders, hits the stop at 16:30 UTC."""

    base = orb.DEMO_URL

    def __init__(self, clock, daily_bars, intraday):
        self.clock, self.daily, self.intraday = clock, daily_bars, intraday
        self.orders, self.open = [], set()

    def bars(self, epic, minutes, count=200):
        if minutes == 1440:
            return self.daily
        return [b for b in orb.parse_bars(self.intraday) if b["start"] <= self.clock.now]

    def market(self, epic):
        return {"snapshot": {"marketStatus": "TRADEABLE", "bid": 94.0, "offer": 95.0,
                             "decimalPlacesFactor": 1},
                "dealingRules": {"minDealSize": {"value": 0.1}, "minSizeIncrement": {"value": 0.1}},
                "instrument": {"currency": "USD"}}

    def order(self, epic, direction, size, stop, target):
        self.orders.append((direction, size, stop, target))
        self.open.add("d_1")
        return "d_1", 94.0

    def positions(self):
        if self.clock.now >= datetime.datetime(2026, 11, 3, 16, 30, tzinfo=datetime.timezone.utc):
            self.open.clear()
        return [{"position": {"dealId": d}, "market": {"epic": "US100"}} for d in self.open]


def test_a_full_day_goes_short_in_a_downtrend(monkeypatch, tmp_path):
    clock = Clock(datetime.datetime(2026, 11, 3, 14, 35, tzinfo=datetime.timezone.utc))
    monkeypatch.setattr(orb.datetime, "datetime", clock.datetime)
    monkeypatch.setattr(orb.time, "sleep", clock.sleep)
    monkeypatch.setattr(orb, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(orb, "KILL_SWITCH", tmp_path / "STOP")
    downtrend = daily(range(160, 120, -1), [40] * 40)
    intraday = ROWS[:3] + [
        row("2026-11-03T14:45:00", 101, 112, 96, 108),   # inside the range
        row("2026-11-03T14:50:00", 108, 111, 90, 93),    # closes below the low 95
        row("2026-11-03T14:55:00", 93, 95, 85, 88),
    ]
    broker = FakeBroker(clock, downtrend, intraday)
    parser_args = ["run", "--max-or-atr", "1", "--max-target-atr", "2"]
    monkeypatch.setattr(orb, "Capital", lambda *a: broker)
    broker.login = lambda: None
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD"):
        monkeypatch.setenv(name, "x")
    assert orb.main(parser_args) == 0
    assert broker.orders == [("SELL", 5.2, 103.5, 78.0)]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["trades"][0]["direction"] == "SELL" and state["trades"][0]["open"] is False


def run_short_day(monkeypatch, tmp_path, extra_args, view=None):
    """The downtrend day above, with an optional agent-desk call; returns the broker."""
    clock = Clock(datetime.datetime(2026, 11, 3, 14, 35, tzinfo=datetime.timezone.utc))
    monkeypatch.setattr(orb.datetime, "datetime", clock.datetime)
    monkeypatch.setattr(orb.time, "sleep", clock.sleep)
    monkeypatch.setattr(orb, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(orb, "KILL_SWITCH", tmp_path / "STOP")
    monkeypatch.setattr(orb, "AGENT_VIEW", tmp_path / "agent_view.json")
    if view is not None:
        (tmp_path / "agent_view.json").write_text(json.dumps(view))
    intraday = ROWS[:3] + [row("2026-11-03T14:45:00", 101, 112, 96, 108),
                           row("2026-11-03T14:50:00", 108, 111, 90, 93),
                           row("2026-11-03T14:55:00", 93, 95, 85, 88)]
    broker = FakeBroker(clock, daily(range(160, 120, -1), [40] * 40), intraday)
    broker.login = lambda: None
    monkeypatch.setattr(orb, "Capital", lambda *a: broker)
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD"):
        monkeypatch.setenv(name, "x")
    assert orb.main(["run", "--max-or-atr", "1", "--max-target-atr", "2", *extra_args]) == 0
    return broker


def test_agents_agreeing_with_the_market_let_the_trade_through(monkeypatch, tmp_path):
    view = {"date": "2026-11-03", "ticker": "QQQ", "rating": "Underweight", "bias": "short"}
    assert run_short_day(monkeypatch, tmp_path, ["--agents"], view).orders[0][0] == "SELL"


@pytest.mark.parametrize("view", [
    {"date": "2026-11-03", "ticker": "QQQ", "rating": "Buy", "bias": "long"},
    {"date": "2026-11-03", "ticker": "QQQ", "rating": "Hold", "bias": "none"},
    {"date": "2026-11-02", "ticker": "QQQ", "rating": "Sell", "bias": "short"},
    None,
])
def test_agents_disagreeing_holding_stale_or_missing_mean_no_trade(monkeypatch, tmp_path, view):
    assert run_short_day(monkeypatch, tmp_path, ["--agents"], view).orders == []


def test_combine_sides_keeps_only_the_shared_direction():
    assert orb.combine_sides({"BUY", "SELL"}, "short") == {"SELL"}
    assert orb.combine_sides({"BUY"}, "short") == set()
    assert orb.combine_sides({"BUY"}, "none") == set()
