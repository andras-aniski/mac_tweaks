"""The Settings window's changes: each is saved to the config, or explains what's wrong with the input."""
import pytest

import pr_reviewer as m
from conftest import NOW, key, load, make_pr


@pytest.mark.parametrize("text, minimum, minutes", [
    ("15", 1, 15), (" 2.5 ", 1, 2.5), ("0", 0, 0), ("0", 1, None), ("", 1, None), ("soon", 1, None),
    ("nan", 0, None), ("inf", 1, None), ("-3", 0, None),
])
def test_parse_minutes(text, minimum, minutes):
    if minutes is None:
        with pytest.raises(ValueError):
            m.parse_minutes(text, minimum)
    else:
        assert m.parse_minutes(text, minimum) == minutes
        assert type(m.parse_minutes(text, minimum)) is type(minutes)


@pytest.mark.parametrize("text, budget", [("500", 500), ("12.5", 12.5), ("  ", None), ("", None)])
def test_parse_budget(text, budget):
    assert m.parse_budget(text) == budget


@pytest.mark.parametrize("text", ["0", "-1", "lots", "inf"])
def test_parse_budget_rejects(text):
    with pytest.raises(ValueError):
        m.parse_budget(text)


def test_track_and_remove_repos(app):
    assert app.track_repo("https://github.com/acme/api/pulls") is None
    assert [m.repo_key(r) for r in m.load_config()["repos"]] == ["acme/web", "acme/api"]
    assert app.track_repo("Acme/API") == "Already tracking acme/api"
    assert app.track_repo("not a repo").startswith("Not a repo")

    load(app, make_pr(1))
    app.state["reviews"][key(1)] = {"status": "failed", "error": "x"}
    app.remove_repo("acme/web")
    assert [m.repo_key(r) for r in m.load_config()["repos"]] == ["acme/api"]
    assert app.state["reviews"] == {} and "acme/web" not in app.state["known"]


def test_numbers_are_saved_or_explained(app):
    assert app.set_poll("5") is None and app.set_wait("0") is None and app.set_budget("250") is None
    config = m.load_config()
    assert (config["poll_minutes"], config["automation"]["wait_minutes"], config["monthly_budget"]) == (5, 0, 250)

    assert app.set_poll("0") and app.set_wait("-1") and app.set_budget("-5")
    assert m.load_config() == config  # nothing saved
    assert app.set_budget("") is None
    assert "monthly_budget" not in m.load_config()


def test_author_lists(app):
    app.set_authors("block", "@renovate[bot], dependabot")
    assert m.load_config()["block"] == ["renovate[bot]", "dependabot"]


def test_automation_switches(app, monkeypatch):
    app.set_automation("review", True)
    automation = m.load_config()["automation"]
    assert automation["review"] and automation["since"] >= NOW  # not the backlog

    answers = iter([0, 1])  # cancel, then turn on
    monkeypatch.setattr(m.rumps, "alert", lambda *a, **kw: next(answers))
    app.set_automation("approve", True)
    assert not m.load_config()["automation"]["approve"]
    app.set_automation("approve", True)
    assert m.load_config()["automation"]["approve"]
    app.set_automation("approve", False)  # no question to turn it off
    assert not m.load_config()["automation"]["approve"]


def test_budget_share_is_colored(app):
    app.month_spent = 10.0
    assert app._spending_summary() == [("Today 10.0 · this week 10.0 · this month 10.0 AIC", None)]
    app.config["monthly_budget"] = 100
    assert ("10%", m.GREEN) in app._spending_summary()
    app.month_spent = 85.0
    assert ("85%", m.AMBER) in app._spending_summary()
    app.month_spent = 100.0
    assert app._spending_summary()[0] == ("⚠︎ Over budget", m.RED)


def test_set_model(app):
    app.set_model("gpt-6.1-sol")
    assert m.load_config()["copilot_model"] == "gpt-6.1-sol"
    app.set_model(None)
    assert "copilot_model" not in m.load_config()
