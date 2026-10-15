"""Shared fixtures: the app on a scratch config file, with no menu bar item, no network and no banners."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pr_watchlist as m  # noqa: E402

KEY = "acme/web#1"


def make_pr(*, state="OPEN", checks=None, mergeable="MERGEABLE", merge_state="BLOCKED", unresolved=0,
            decision=None, draft=False):
    """A PR as the GraphQL query returns it; checks is the status check rollup state, None for no checks."""
    return {
        "title": "Add login",
        "url": "https://github.com/acme/web/pull/1",
        "state": state,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": decision,
        "reviewThreads": {"nodes": [{"isResolved": False}] * unresolved},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": checks and {"state": checks}}}]},
    }


@pytest.fixture
def notes(monkeypatch):
    """The banners the app shows, by title."""
    sent = []
    monkeypatch.setattr(m, "notify", lambda title, message, url: sent.append(title))
    return sent


@pytest.fixture
def app(tmp_path, monkeypatch, notes):
    """The app watching acme/web#1 on a scratch config file, without rendering."""
    monkeypatch.setattr(m, "CONFIG_PATH", str(tmp_path / "config.json"))
    m.save_config({"prs": [{"owner": "acme", "repo": "web", "number": 1, "label": "login"}], "show_labels": True})
    instance = m.PRWatchlistApp()
    instance._render = instance._render_title = lambda: None
    return instance


def load(app, pr):
    """A successful poll that returned this PR."""
    app._apply(list(app.prs), [pr], None)
