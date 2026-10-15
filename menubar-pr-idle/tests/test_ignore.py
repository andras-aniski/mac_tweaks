"""Ignoring a PR: no attention or notification for it until it's merged or closed."""
import json

import pr_idle as m
from conftest import key, load, make_pr


def saved():
    with open(m.CONFIG_PATH) as f:
        return json.load(f)


def test_ignored_pr_needs_no_attention(app, notes):
    load(app, make_pr(1), make_pr(2))
    assert app.attention == {key(1), key(2)}
    app.ignore(key(1))
    assert app.attention == {key(2)}
    assert saved()["ignored"] == [key(1)]
    assert notes == []  # the first poll after starting doesn't notify, and ignoring doesn't either


def test_ignored_pr_crossing_the_threshold_does_not_notify(app, notes):
    load(app, make_pr(1, idle_h=1))
    app.ignore(key(1))
    load(app, make_pr(1, idle_h=30))
    assert app.attention == set()
    assert notes == []


def test_put_back_without_a_notification(app, notes):
    load(app, make_pr(1))
    app.ignore(key(1))
    app.unignore(key(1), None)
    assert app.attention == {key(1)}
    assert saved()["ignored"] == []
    app._update_attention()
    assert notes == []


def test_merged_or_closed_prs_are_forgotten(app):
    load(app, make_pr(1), make_pr(2))
    app.ignore(key(1))
    app.ignore(key(2))
    load(app, make_pr(2))
    assert saved()["ignored"] == [key(2)]


def test_a_failed_fetch_keeps_the_ignored_prs(app):
    load(app, make_pr(1))
    app.ignore(key(1))
    app._apply({"acme/web": {"error": "Not found or no access"}}, None)
    app._apply({}, "HTTP 502 Bad Gateway")
    assert saved()["ignored"] == [key(1)]


def test_stop_tracking_forgets_the_repos_ignored_prs(app):
    load(app, make_pr(1))
    app.ignore(key(1))
    app.remove_repo("acme/web", None)
    assert saved()["ignored"] == []


def test_cli_remove_forgets_the_repos_ignored_prs(app):
    config = saved()
    config["ignored"] = [key(1), "acme/api#2"]
    m.save_config(config)
    m.cli(["remove", "acme/web"])
    assert saved()["ignored"] == ["acme/api#2"]


def test_an_ignore_made_outside_the_app_is_picked_up(app, notes):
    load(app, make_pr(1))
    m.save_config({**saved(), "ignored": [key(1)]})
    app._check_config()
    assert app.attention == set()


def test_invalid_ignored_list_is_reset(app):
    m.save_config({**saved(), "ignored": "acme/web#1"})
    assert m.load_config()["ignored"] == []
