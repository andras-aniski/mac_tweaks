"""Shared fixtures: the app on a scratch config folder, with no menu, no network and no background threads."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pr_reviewer as m  # noqa: E402

NOW = time.time()
HOUR = 3600


def make_pr(number, *, author="ann", ready_h=50, pushed_h=None, review=None, review_h=10, requested=False,
            threads=(0, 0), replied_h=None):
    """A PR as summarize() returns it. The *_h arguments are hours ago."""
    def ago(hours):
        return NOW - hours * HOUR

    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/acme/web/pull/{number}",
        "author": author,
        "ready": ago(ready_h),
        "pushed": ago(ready_h if pushed_h is None else pushed_h),
        "review": {"state": review, "at": ago(review_h)} if review else None,
        "requested": requested,
        "threads": threads,
        "replied": ago(replied_h) if replied_h is not None else 0,
    }


def key(number):
    return f"acme/web#{number}"


class InlineThread:
    """threading.Thread that runs its target right away, so a test sees the result."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def notes(monkeypatch):
    """The notifications the app shows, by title."""
    sent = []
    monkeypatch.setattr(m, "notify", lambda title, message: sent.append(title))
    return sent


@pytest.fixture
def app(tmp_path, monkeypatch, notes):
    """The app tracking acme/web, on a scratch config folder, without a menu, threads or GitHub."""
    monkeypatch.setattr(m, "CONFIG_PATH", str(tmp_path / "config.json"))
    m.save_config({"repos": [{"owner": "acme", "repo": "web"}], "allow": [], "block": []})
    monkeypatch.setattr(m.AppHelper, "callAfter", lambda f, *args: f(*args))
    monkeypatch.setattr(m.threading, "Thread", InlineThread)
    instance = m.PRReviewerApp()
    instance._render = lambda: None
    instance.login, instance.token = "me", "token"
    instance.started_at = NOW - HOUR  # past the grace period after starting
    return instance


def load(app, *prs):
    """A successful poll that found these PRs."""
    app._apply({"acme/web": {"prs": list(prs)}}, None)


def groups(app):
    """{group: [PR numbers]}"""
    to_review, reviewed, ignored, approved = app._groups()
    return {name: [int(k.split("#")[1]) for k in group] for name, group in
            (("to review", to_review), ("reviewed", reviewed), ("ignored", ignored), ("approved", approved))}
