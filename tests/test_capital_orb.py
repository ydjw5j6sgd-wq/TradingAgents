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
    bar = orb.find_trigger(bars, DAY, 15, 112)
    assert bar["close"] == 114
    assert orb.find_trigger(bars, DAY, 15, 200) is None


def test_closed_bars_drops_the_forming_bar():
    bars = orb.parse_bars(ROWS)
    now = datetime.datetime(2026, 11, 3, 14, 57, tzinfo=datetime.timezone.utc)
    assert len(orb.closed_bars(bars, now, 5)) == 5


def test_plan_trade_sizes_to_the_max_loss():
    plan = orb.plan_trade(ask=114.0, or_high=112, or_low=95, stop_mode="mid", rr=2, risk=50,
                          point_value=1, min_size=0.1, step=0.1, decimals=1)
    assert plan["stop"] == 103.5
    assert plan["target"] == 135.0
    assert plan["size"] == 4.7
    assert plan["max_loss"] <= 50


def test_plan_trade_refuses_a_stop_too_far_for_the_risk():
    with pytest.raises(orb.BotError, match="Kein Trade"):
        orb.plan_trade(ask=114.0, or_high=112, or_low=95, stop_mode="low", rr=2, risk=5,
                       point_value=1, min_size=1, step=1, decimals=1)


def test_session_times_on_a_normal_day_a_holiday_and_an_early_close():
    t = lambda s: datetime.time.fromisoformat(s)
    assert orb.session_times(DAY, t("15:30"), t("15:55")) == (t("15:30"), t("15:55"))
    assert orb.session_times(datetime.date(2026, 11, 7), t("15:30"), t("15:55")) is None
    assert orb.session_times(datetime.date(2026, 11, 26), t("15:30"), t("15:55")) is None
    assert orb.session_times(datetime.date(2026, 11, 27), t("15:30"), t("15:55")) == (
        t("12:30"), t("12:55"))


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
    assert api.buy("US100", 4.7, 103.5, 135.0) == ("d_1", 114.2)
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
        api.buy("US100", 1, 100, 120)


def test_live_needs_the_extra_environment_variable(monkeypatch, capsys):
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("CAPITAL_LIVE", raising=False)
    assert orb.main(["run", "--live"]) == 2
    assert "CAPITAL_LIVE=yes" in capsys.readouterr().err
