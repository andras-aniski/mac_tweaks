"""The Approve button of a PR back from Reviewed, e.g. once my comments are resolved."""
import pytest

import pr_reviewer as m
from conftest import key, load, make_pr

ADDRESSED = {"review": "CHANGES_REQUESTED", "threads": (2, 2), "pushed_h": 1}


@pytest.fixture
def github(monkeypatch):
    """Stubbed approvals: the PRs approved and at which commit, or the error to raise."""
    calls = {"approve": [], "error": None}

    def approve(k, head, token):
        if calls["error"]:
            raise calls["error"]
        calls["approve"].append((k, head))

    monkeypatch.setattr(m.copilot_review, "approve", approve)
    return calls


class Alerts(list):
    answer = 1  # OK; 0: Cancel


@pytest.fixture
def alerts(monkeypatch):
    """The alerts the app shows, by title, each answered with alerts.answer."""
    shown = Alerts()
    monkeypatch.setattr(m.rumps, "alert", lambda title, *args, **kwargs: shown.append(title) or shown.answer)
    return shown


def press_approve(app, k):
    then = app.approve(k)
    if then:
        then()


def test_back_rows_have_approve_instead_of_ignore(app):
    load(app, make_pr(1), make_pr(2, **ADDRESSED))
    app._render_menu(*app._groups())
    titles = {k: [b.title() for b in row.buttons] for k, row in app.rows.items()}
    assert titles == {key(1): ["Review", "Ignore"], key(2): ["Review", "Approve"]}


def test_approves_the_latest_commit(app, github, alerts, notes):
    load(app, make_pr(1, **ADDRESSED))
    press_approve(app, key(1))
    assert alerts == [f"Approve {key(1)}?"]
    assert github["approve"] == [(key(1), None)]
    assert notes == [f"Approved {key(1)}"]
    assert app.approving == set() and app.next_poll_at == 0


def test_cancel_does_nothing(app, github, alerts):
    alerts.answer = 0
    load(app, make_pr(1, **ADDRESSED))
    press_approve(app, key(1))
    assert github["approve"] == [] and app.approving == set()


def test_a_failed_approval_says_why(app, github, alerts):
    github["error"] = RuntimeError("Can not approve your own pull request")
    load(app, make_pr(1, **ADDRESSED))
    press_approve(app, key(1))
    assert alerts == [f"Approve {key(1)}?", f"Couldn't approve {key(1)}"]
    assert app.approving == set()


def test_a_copilot_review_with_comments_stays(app, github, alerts):
    load(app, make_pr(1, **ADDRESSED), make_pr(2, **ADDRESSED))
    for number, comments in ((1, 3), (2, 0)):
        app.state["reviews"][key(number)] = {"status": "done", "session": "s", "started": 0, "comments": comments,
                                             "head": "abc", "credits": {"total": 1.0, "agents": []}}
    press_approve(app, key(1))
    press_approve(app, key(2))
    assert key(1) in app.state["reviews"] and key(2) not in app.state["reviews"]


def test_the_button_follows_the_approval(app):
    load(app, make_pr(1, **ADDRESSED))
    app.approving.add(key(1))
    app._render_menu(*app._groups())
    assert app.rows[key(1)].buttons[1].title() == "Approving…"
    app.approving.clear()
    app._render_menu(*app._groups())
    assert app.rows[key(1)].buttons[1].title() == "Approve"
