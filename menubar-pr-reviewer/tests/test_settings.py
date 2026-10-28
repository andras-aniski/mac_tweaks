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


def test_one_author_list_at_a_time(app):
    app.set_authors("allow", "alice")
    assert "Only PRs by" in app.set_authors("block", "renovate")
    assert m.load_config()["block"] == []
    assert app.set_authors("allow", "") is None and app.set_authors("block", "renovate") is None

    app.config.update(allow=["alice"], block=["renovate"])  # both, from a hand edit: clearing one still works
    assert app.set_authors("allow", "bob")
    assert app.set_authors("allow", "") is None
    assert m.load_config()["allow"] == []


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


def test_repo_admins(app):
    assert app.set_admins("Acme/Web", "@Alice, bob") is None
    config = m.load_config()
    assert config["repos"][0]["admins"] == ["Alice", "bob"]
    assert m.repo_admins(config, "ACME/web#7") == ["Alice", "bob"] and m.repo_admins(config, "acme/api#1") == []
    assert app.set_admins("acme/api", "carol") == "Not tracking acme/api"
    app.set_admins("acme/web", "")
    assert "admins" not in m.load_config()["repos"][0]


def test_reviews_get_the_repos_admins(app, monkeypatch):
    given = []
    monkeypatch.setattr(m.copilot_review, "prepare",
                        lambda key, workdir, clones, token, admins: given.append(admins) or {"base": "main"})
    monkeypatch.setattr(m.copilot_review, "start", lambda *a, **kw: None)
    monkeypatch.setattr(app, "_review_started", lambda *a: None)
    load(app, make_pr(1))
    app.set_admins("acme/web", "alice")
    app._start_review(key(1))
    assert given == [["alice"]]


def test_set_effort_and_context(app):
    app.set_review_option("copilot_effort", "xhigh")
    app.set_review_option("copilot_context", "default")
    config = m.load_config()
    assert (config["copilot_effort"], config["copilot_context"]) == ("xhigh", "default")


def test_reviews_start_with_the_chosen_model_effort_context_and_subagents(app, monkeypatch):
    started = []
    monkeypatch.setattr(m.copilot_review, "prepare", lambda key, workdir, clones, token, admins: {"base": "main"})
    monkeypatch.setattr(m.copilot_review, "start", lambda *a, **kw: started.append(kw) or None)
    monkeypatch.setattr(app, "_review_started", lambda *a: None)
    load(app, make_pr(1))
    app.set_model("claude-opus-5.5")
    app.set_review_option("copilot_effort", "high")
    app.set_agent("prr-conventions", False)
    app._start_review(key(1))
    assert {k: started[0][k] for k in ("model", "effort", "context", "agents")} == {
        "model": "claude-opus-5.5", "effort": "high", "context": "long_context",
        "agents": ["prr-acceptance", "prr-docs", "prr-code"]}
    job = app.state["reviews"][key(1)]
    assert (job["model"], job["effort"], job["context"]) == ("claude-opus-5.5", "high", "long_context")


def test_set_model(app):
    app.set_model("gpt-6.1-sol")
    assert m.load_config()["copilot_model"] == "gpt-6.1-sol"
    app.set_model(None)
    assert "copilot_model" not in m.load_config()


def test_subagents_can_be_turned_off_but_not_all(app):
    *rest, last = m.copilot_review.SUBAGENTS
    for agent in rest:
        assert app.set_agent(agent, False) is None
    assert m.review_agents(m.load_config()) == [last]
    assert app.set_agent(last, False) == "At least one subagent has to run."
    assert m.review_agents(m.load_config()) == [last]
    assert app.set_agent(rest[0], True) is None and app.set_agent(rest[0], True) is None
    assert m.load_config()["copilot_agents_off"] == rest[1:]
