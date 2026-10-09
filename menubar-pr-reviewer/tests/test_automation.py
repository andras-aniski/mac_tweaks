"""Settings → Automation: when a review starts by itself, and when it's pushed or approved."""
import pytest

import history
import pr_reviewer as m
from conftest import HOUR, NOW, key, load, make_pr


@pytest.fixture
def started(app, monkeypatch):
    """The reviews the app starts, instead of starting them."""
    calls = []

    def start(k, auto=False):  # what the real one leaves behind: a running review
        calls.append((k, auto))
        app.state["reviews"][k] = {"status": "running", "auto": auto, "session": "s", "started": NOW, "pid": 1}
        app.state["reviewed_activity"][k] = m.last_activity(app._tracked()[k])

    monkeypatch.setattr(app, "_start_review", start)
    return calls


def automate(app, **settings):
    app.config["automation"].update({"since": NOW - 48 * HOUR, **settings})
    app._automate()


def test_off_by_default(app, started):
    load(app, make_pr(1, ready_h=5))
    app._automate()
    assert started == []


def test_due_after_the_wait(app, started):
    load(app, make_pr(1, ready_h=40 / 60), make_pr(2, ready_h=10 / 60))
    automate(app, review=True, wait_minutes=30)
    assert started == [(key(1), True)]
    assert round(app._auto_review_in(key(2), app._tracked()[key(2)], NOW) / 60) == 20


def test_nothing_automatic_right_after_the_app_starts(app, started, monkeypatch):
    """A grace period after starting, to see what's due before anything runs, is pushed or approved."""
    app.started_at = NOW - 60
    load(app, make_pr(1, ready_h=5))
    automate(app, review=True)
    assert started == []
    assert round(app._auto_review_in(key(1), app._tracked()[key(1)], NOW) / 60) == 4
    acted = []
    monkeypatch.setattr(app, "_auto_act", lambda k, action: acted.append((k, action)))
    app.state["reviews"][key(1)] = {"status": "done", "auto": True, "session": "s", "started": NOW - HOUR,
                                    "comments": 0, "credits": {"total": 1.0, "agents": []}}
    automate(app, approve=True)
    assert acted == []
    app.started_at = NOW - m.STARTUP_GRACE
    automate(app, approve=True)
    assert acted == [(key(1), "approve")]


def test_a_push_restarts_the_wait(app, started):
    load(app, make_pr(1, ready_h=5, pushed_h=10 / 60))
    automate(app, review=True, wait_minutes=30)
    assert started == []


def test_not_the_backlog(app, started):
    """Not PRs whose wait was over before auto-review was switched on."""
    load(app, make_pr(1, ready_h=60))
    automate(app, review=True, since=NOW - 2 * HOUR)
    assert started == []


def test_prs_still_waiting_when_switched_on_count(app, started):
    """Switching it on doesn't drop PRs that were in the middle of their wait."""
    load(app, make_pr(1, ready_h=20 / 60), make_pr(2, ready_h=40 / 60))
    automate(app, review=True, wait_minutes=30, since=NOW - 5 * 60)
    assert round(app._auto_review_in(key(1), app._tracked()[key(1)], NOW) / 60) == 10
    assert app._auto_review_in(key(2), app._tracked()[key(2)], NOW) is None  # due 10m before it was switched on


def test_one_automatic_review_at_a_time(app, started):
    load(app, make_pr(1, ready_h=5), make_pr(2, ready_h=5))
    app.state["reviews"][key(2)] = {"status": "running", "auto": True, "session": "s", "started": NOW, "pid": 1}
    automate(app, review=True)
    assert started == []


def test_my_own_running_review_doesnt_block_it(app, started):
    load(app, make_pr(1, ready_h=5), make_pr(2, ready_h=5))
    app.state["reviews"][key(2)] = {"status": "running", "auto": False, "session": "s", "started": NOW, "pid": 1}
    automate(app, review=True)
    assert started == [(key(1), True)]


def test_not_again_without_new_activity(app, started):
    """After a review that was pushed, approved, discarded or failed: only once the author does something."""
    load(app, make_pr(1, ready_h=5))
    app.state["reviewed_activity"][key(1)] = app._tracked()[key(1)]["ready"]
    automate(app, review=True)
    assert started == []
    load(app, make_pr(1, ready_h=5, pushed_h=1))  # a push since
    app._automate()
    assert started == [(key(1), True)]


def test_not_over_budget(app, started):
    history.append(m.history_path(), {"event": "review", "key": key(9), "credits": 50.0})
    app.config["monthly_budget"] = 40
    load(app, make_pr(1, ready_h=5))
    automate(app, review=True)
    assert started == []


def test_prs_back_in_to_review_get_one(app, started):
    load(app, make_pr(1, ready_h=90, review="COMMENTED", threads=(2, 2), pushed_h=2))
    automate(app, review=True)
    assert started == [(key(1), True)]


@pytest.fixture
def github(monkeypatch, app):
    """Stubbed pushes and approvals, and the PR's head commit."""
    calls = {"push": [], "approve": [], "head": "reviewed"}
    cr = m.copilot_review
    monkeypatch.setattr(cr, "read_result", lambda workdir: {"summary": "Looks fine.", "comments": [
        {"severity": "SHOULD-FIX", "title": "t", "path": None, "line": None, "body": "b", "source": "code"}]})
    monkeypatch.setattr(cr, "to_push", lambda k, comments, token, summary=None: (comments, 0, summary))
    monkeypatch.setattr(cr, "push", lambda k, workdir, head, comments, token, summary=None:
                        calls["push"].append((k, len(comments), summary)))
    monkeypatch.setattr(cr, "approve", lambda k, head, token: calls["approve"].append((k, head)))
    monkeypatch.setattr(cr, "head_sha", lambda k, token: calls["head"])
    return calls


def finished(app, number, comments, auto, started_h=0.5):
    app.state["reviews"][key(number)] = {"status": "done", "auto": auto, "session": "s", "head": "reviewed",
                                          "started": NOW - started_h * HOUR, "comments": comments,
                                          "credits": {"total": 1.0, "agents": []}}


def test_pushes_automatic_reviews_only(app, github):
    load(app, make_pr(1, ready_h=5), make_pr(2, ready_h=5))
    finished(app, 1, comments=1, auto=True)
    finished(app, 2, comments=1, auto=False)
    automate(app, push=True)
    assert github["push"] == [(key(1), 1, "Looks fine.")]
    assert key(1) not in app.state["reviews"] and key(2) in app.state["reviews"]


def test_approves_reviews_that_found_nothing(app, github):
    load(app, make_pr(1, ready_h=5), make_pr(2, ready_h=5))
    finished(app, 1, comments=0, auto=False)
    finished(app, 2, comments=2, auto=True)
    automate(app, approve=True)
    assert github["approve"] == [(key(1), "reviewed")]


def test_nothing_automatic_when_the_pr_moved_on(app, github, notes):
    load(app, make_pr(1, ready_h=5, pushed_h=0.1))  # pushed after the review started
    finished(app, 1, comments=0, auto=True)
    automate(app, approve=True, push=True)
    assert github["approve"] == [] and github["push"] == []
    github["head"] = "newer"  # GitHub knows before the next poll does
    load(app, make_pr(2, ready_h=5))
    finished(app, 2, comments=0, auto=True)
    automate(app, approve=True)
    assert github["approve"] == []
    assert app.state["reviews"][key(2)]["auto_failed"] == "the PR has new commits since the review"
    app._automate()  # it doesn't try again
    assert notes.count(f"Couldn't approve {key(2)} automatically") == 1


def test_review_cap_follows_the_budget(app, monkeypatch):
    for budget, spent, cap, expected in [(None, 10, None, None), (100, 45, None, 56), (100, 85, None, 30),
                                         (100, 45, 40, 40), (None, 0, 60, 60)]:
        app.config.pop("monthly_budget", None)
        app.config.pop("copilot_max_ai_credits", None)
        if budget:
            app.config["monthly_budget"] = budget
        if cap:
            app.config["copilot_max_ai_credits"] = cap
        monkeypatch.setattr(app, "_month_spent", lambda spent=spent: spent)
        assert app._review_cap() == expected, (budget, spent, cap)
