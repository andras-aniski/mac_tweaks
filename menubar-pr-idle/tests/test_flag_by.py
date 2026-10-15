"""Flagging PRs by idle time (hours without activity) or by age (days since ready for review)."""
import json

import pr_idle as m
from conftest import HOUR, key, load, make_pr

DAY = 24 * HOUR


def flag_by_age(app, days=5):
    app.config.update(attention_by="age", attention_days=days)
    app.attention = app._flagged_keys()


def test_defaults(app):
    assert (app.config["attention_by"], app.config["attention_hours"], app.config["attention_days"]) == ("idle", 24, 5)


def test_by_age_flags_old_prs_whatever_their_activity(app):
    flag_by_age(app)
    load(app, make_pr(1, idle_h=1, age_h=6 * 24), make_pr(2, idle_h=50, age_h=2 * 24))
    assert app.attention == {key(1)}


def test_by_age_sorts_oldest_first(app):
    load(app, make_pr(1, idle_h=1, age_h=200), make_pr(2, idle_h=90, age_h=100), make_pr(3, idle_h=30, age_h=300))
    assert list(app._tracked()) == [key(2), key(3), key(1)]  # most idle first
    flag_by_age(app)
    assert list(app._tracked()) == [key(3), key(1), key(2)]


def test_crossing_the_age_threshold_notifies(app, notes):
    flag_by_age(app)
    load(app, make_pr(1, idle_h=1, age_h=4 * 24))
    load(app, make_pr(1, idle_h=1, age_h=5 * 24 + 1))
    assert notes == [f"{key(1)}: ready for review for 5d 1h"]


def test_switching_does_not_notify(app, notes):
    load(app, make_pr(1, idle_h=1, age_h=10 * 24), make_pr(2, idle_h=30, age_h=40))
    assert app.attention == {key(2)}
    app.set_flag_by("age", None)
    assert app.attention == {key(1)}
    app._update_attention()
    assert notes == []
    with open(m.CONFIG_PATH) as f:
        assert json.load(f)["attention_by"] == "age"


def test_switching_outside_the_app_does_not_notify(app, notes):
    load(app, make_pr(1, idle_h=1, age_h=10 * 24))
    m.cli(["by", "age"])
    app._check_config()
    assert app.attention == {key(1)} and notes == []


def test_invalid_settings_get_the_defaults(app):
    m.save_config({"repos": [], "attention_by": "size", "attention_days": -1})
    config = m.load_config()
    assert (config["attention_by"], config["attention_days"]) == ("idle", 5)


def test_cli_days(app, capsys):
    m.cli(["days", "2.5"])
    m.cli(["days"])
    assert capsys.readouterr().out.splitlines() == ["PRs need attention after 2.5d old", "2.5"]


def node(created, ready_events=(), timeline=()):
    """A PR as the GraphQL query returns it."""
    return {
        "number": 1, "title": "t", "url": "u", "isDraft": False, "createdAt": created, "author": {"login": "ann"},
        "commits": {"nodes": []}, "comments": {"nodes": []}, "reviews": {"nodes": []},
        "timelineItems": {"nodes": [{"createdAt": t} for t in timeline]},
        "ready": {"nodes": [{"createdAt": t} for t in ready_events]},
    }


def test_age_counts_from_the_last_ready_for_review():
    pr = m.summarize(node("2026-10-01T10:00:00Z", ["2026-10-05T10:00:00Z"]))
    assert pr["ready"] == m.parse_time("2026-10-05T10:00:00Z")


def test_age_of_a_pr_opened_ready_counts_from_opening():
    pr = m.summarize(node("2026-10-01T10:00:00Z"))
    assert pr["ready"] == m.parse_time("2026-10-01T10:00:00Z")


def test_one_dialog_sets_both_thresholds(app, monkeypatch, notes):
    load(app, make_pr(1, idle_h=30, age_h=3 * 24))
    monkeypatch.setattr(m, "ask_thresholds", lambda hours, days: ("48", "2.5"))
    app.set_thresholds(None)
    with open(m.CONFIG_PATH) as f:
        config = json.load(f)
    assert (config["attention_hours"], config["attention_days"]) == (48, 2.5)
    assert app.attention == set() and notes == []


def test_a_bad_value_saves_neither(app, monkeypatch):
    shown = []
    monkeypatch.setattr(m, "ask_thresholds", lambda hours, days: ("48", "soon"))
    monkeypatch.setattr(m.rumps, "alert", lambda title, *args, **kwargs: shown.append(title))
    app.set_thresholds(None)
    assert shown == ["Not a number"]
    assert (m.load_config()["attention_hours"], m.load_config()["attention_days"]) == (24, 5)


def test_parse_threshold():
    assert [m.parse_threshold(t) for t in ("24", " 1.5 ", "0", "-2", "inf", "nan", "")] == [24, 1.5] + [None] * 5
