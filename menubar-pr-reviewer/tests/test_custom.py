"""Review a PR…: reviewing a PR the menu doesn't list with a Review button."""
import pytest

import pr_reviewer as m
from conftest import NOW, groups, key, load, make_pr

OTHER = "other/lib#5"


def other_pr(**kwargs):
    pr = make_pr(5, **kwargs)
    pr["url"] = "https://github.com/other/lib/pull/5"
    return pr


@pytest.fixture
def started(app, monkeypatch):
    """The reviews the app starts, instead of starting them."""
    calls = []

    def start(k, auto=False):
        calls.append(k)
        app.state["reviews"][k] = {"status": "running", "auto": auto, "session": "s", "started": NOW, "pid": 1}

    monkeypatch.setattr(app, "_start_review", start)
    return calls


def test_parse_pr():
    assert m.parse_pr("https://github.com/acme/web/pull/12/files#diff-1") == "acme/web#12"
    assert m.parse_pr(" acme/web#7 ") == "acme/web#7"
    assert m.parse_pr("https://github.com/acme/web") is None
    assert m.parse_pr("https://github.com/acme/web/issues/3") is None


def test_another_repos_pr_is_listed_in_to_review(app, started):
    load(app)
    app._got_pr(OTHER, other_pr(), None)
    assert started == [OTHER]
    assert OTHER in app._groups()[0]
    assert app.state["custom"] == [OTHER]


def test_the_key_is_spelled_as_github_does(app, started):
    load(app)
    app._got_pr("OTHER/Lib#5", other_pr(), None)
    assert started == [OTHER]


@pytest.mark.parametrize("pr", [{"author": "me"}, {"author": "bob"}], ids=["my own", "author filtered out"])
def test_prs_the_menu_leaves_out(app, started, pr):
    app.config["block"] = ["bob"]
    load(app, make_pr(1, **pr))
    app._got_pr(key(1), make_pr(1, **pr), None)
    assert groups(app)["to review"] == [1]
    assert app.state["custom"] == [key(1)]


def test_a_listed_pr_is_just_reviewed(app, started):
    load(app, make_pr(1))
    app._got_pr(key(1), make_pr(1), None)
    assert started == [key(1)]
    assert app.state["custom"] == []


def test_a_running_review_isnt_started_again(app, started, monkeypatch):
    load(app, make_pr(1))
    app.state["reviews"][key(1)] = {"status": "running", "session": "s", "started": NOW, "pid": 1}
    dialogs = []
    monkeypatch.setattr(app, "_review_dialog", dialogs.append)
    app._got_pr(key(1), make_pr(1), None)
    assert started == [] and dialogs == [key(1)]


def test_stays_in_to_review_while_its_review_lasts(app, started):
    load(app)
    app._got_pr(OTHER, other_pr(), None)
    app._apply({"acme/web": {"prs": []}}, None, {OTHER: other_pr(review="COMMENTED", threads=(0, 1))})
    assert OTHER in app._groups()[0]  # the review I pushed by hand on GitHub, but Copilot's isn't done
    del app.state["reviews"][OTHER]
    assert OTHER in app._groups()[1]


def test_forgotten_once_closed(app, started, monkeypatch):
    dropped = []
    monkeypatch.setattr(app, "_drop_review", lambda k: dropped.append(app.state["reviews"].pop(k) and k))
    load(app)
    app._got_pr(OTHER, other_pr(), None)
    app._apply({"acme/web": {"prs": []}}, None, {OTHER: None})
    assert app.state["custom"] == [] and OTHER not in app._tracked()
    assert dropped == [OTHER]


def test_a_draft_in_a_tracked_repo_keeps_its_review(app, started):
    """The repo's listing leaves drafts out; the PR's own result says it's still open."""
    load(app)
    app._got_pr(key(1), make_pr(1), None)
    app._apply({"acme/web": {"prs": []}}, None, {key(1): make_pr(1)})
    assert key(1) in app.state["reviews"]


def test_untracking_a_repo_keeps_them(app, started):
    load(app)
    app._got_pr(OTHER, other_pr(), None)
    app.remove_repo("acme/web")
    assert app.state["custom"] == [OTHER] and OTHER in app.state["reviews"]


def test_remove_asks_when_there_is_a_review(app, started, monkeypatch):
    monkeypatch.setattr(app, "_drop_review", lambda k: app.state["reviews"].pop(k))
    load(app)
    app._got_pr(OTHER, other_pr(), None)
    assert app.remove_custom(OTHER) is not None  # the dialog, once the menu has closed
    assert app.state["custom"] == [OTHER]
    del app.state["reviews"][OTHER]
    assert app.remove_custom(OTHER) is None
    assert app.state["custom"] == [] and OTHER not in app._groups()[0]


def test_my_own_pr_isnt_approved_automatically(app, monkeypatch):
    load(app)
    app.custom_prs[key(1)] = make_pr(1, author="me")
    app.state["custom"] = [key(1)]
    app.state["reviews"][key(1)] = {"status": "done", "session": "s", "started": NOW, "comments": 0,
                                    "credits": {"total": 1.0, "agents": []}}
    acted = []
    monkeypatch.setattr(app, "_auto_act", lambda k, action: acted.append(k))
    app.config["automation"]["approve"] = True
    app._automate()
    assert acted == []
