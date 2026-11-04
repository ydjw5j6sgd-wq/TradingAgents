"""Tests for bots/agent_desk.py: how a TradingAgents rating becomes the bot's direction."""

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "bots" / "agent_desk.py"
_spec = importlib.util.spec_from_file_location("agent_desk", _PATH)
desk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(desk)


@pytest.mark.parametrize("rating, bias", [
    ("Buy", "long"), ("Overweight", "long"), ("Hold", "none"),
    ("Underweight", "short"), ("Sell", "short"), ("REVIEW", "none"), (None, "none"),
])
def test_ratings_map_to_a_direction_and_anything_unclear_is_no_trade(rating, bias):
    assert desk.bias_for(rating) == bias


def test_the_saved_view_round_trips(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 11, 3, 13, 0, tzinfo=datetime.timezone.utc)
    view = desk.build_view("QQQ", "2026-11-03", "Overweight", "Rating: Overweight\n...", now)
    desk.save_view(view, tmp_path / "v.json")
    saved = json.loads((tmp_path / "v.json").read_text())
    assert saved["bias"] == "long" and saved["created_utc"] == "2026-11-03T13:00:00+00:00"
    assert "nur LONG" in desk.describe(saved)


def test_main_runs_the_desk_and_saves_todays_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(desk, "VIEW_FILE", tmp_path / "v.json")
    monkeypatch.setattr(desk, "run_desk", lambda ticker, day: ("Sell", "Rating: Sell"))
    assert desk.main(["--ticker", "QQQ", "--date", "2026-11-03"]) == 0
    assert json.loads((tmp_path / "v.json").read_text())["bias"] == "short"
    assert desk.main(["--show"]) == 0
    assert "nur SHORT" in capsys.readouterr().out
