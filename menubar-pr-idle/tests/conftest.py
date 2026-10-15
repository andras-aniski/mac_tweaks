"""Shared fixtures: the app on a scratch config, with no menu, no network and no background threads."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pr_idle as m  # noqa: E402

NOW = time.time()
HOUR = 3600


def make_pr(number, *, idle_h=50, age_h=100):
    """A PR as summarize() returns it: idle for idle_h hours, ready for review for age_h hours."""
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/acme/web/pull/{number}",
        "author": "ann",
        "ready": NOW - age_h * HOUR,
        "active": NOW - idle_h * HOUR,
    }


def key(number):
    return f"acme/web#{number}"


@pytest.fixture
def notes(monkeypatch):
    """The notifications the app shows, by title."""
    sent = []
    monkeypatch.setattr(m, "notify", lambda title, message: sent.append(title))
    return sent


@pytest.fixture
def app(tmp_path, monkeypatch, notes):
    """The app tracking acme/web with a 24h threshold, on a scratch config, without a menu or GitHub."""
    monkeypatch.setattr(m, "CONFIG_PATH", str(tmp_path / "config.json"))
    m.save_config({"repos": [{"owner": "acme", "repo": "web"}], "attention_hours": 24})
    instance = m.PRIdleApp()
    instance._render = lambda: None
    return instance


def load(app, *prs):
    """A successful poll that found these open PRs."""
    app._apply({"acme/web": {"prs": list(prs)}}, None)
