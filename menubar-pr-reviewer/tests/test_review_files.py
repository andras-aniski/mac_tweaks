"""A review's folder is shared by nobody: one app instance per config folder, and a lost result turns the review failed."""
import pytest

import pr_reviewer as m
from conftest import NOW, key, load, make_pr


def test_one_instance_per_config_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CONFIG_PATH", str(tmp_path / "config.json"))
    first = m.lock_instance()
    assert first is not None
    assert m.lock_instance() is None
    first.close()  # the first one quit
    assert m.lock_instance() is not None


@pytest.fixture
def alerts(monkeypatch):
    """The alerts the app shows, by title; each is closed."""
    shown = []
    monkeypatch.setattr(m.rumps, "alert", lambda title, *args, **kwargs: shown.append(title) or 0)
    return shown


def finished(app, auto=False):
    load(app, make_pr(1, ready_h=5))
    app.state["reviews"][key(1)] = {"status": "done", "auto": auto, "session": "s", "head": "reviewed",
                                     "started": NOW, "comments": 3, "credits": {"total": 1.0, "agents": []}}


def test_a_review_whose_files_are_gone_turns_failed(app, alerts):
    finished(app)  # its folder was never written, e.g. deleted by another instance
    app._open_review(key(1))
    job = app.state["reviews"][key(1)]
    assert job["status"] == "failed" and job["error"].startswith("The review's files are gone")
    assert alerts == [f"The review of {key(1)} failed"]  # offering to retry it
    assert app._review_button(job) == ("Review failed", m.RED)


def test_an_automatic_push_whose_files_are_gone_turns_failed(app, alerts, notes):
    finished(app, auto=True)
    app.config["automation"].update(push=True, since=0)
    app._automate()
    assert app.state["reviews"][key(1)]["status"] == "failed"
    assert notes == [f"Couldn't push {key(1)} automatically"] and alerts == []
