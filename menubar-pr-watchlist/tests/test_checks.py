"""Running CI checks: they blink the PR's LED on top of whatever state it is in."""
import pytest

import pr_watchlist as m
from conftest import KEY, load, make_pr


@pytest.mark.parametrize("pr, running", [
    (make_pr(checks="PENDING"), True),
    (make_pr(checks="EXPECTED"), True),
    (make_pr(checks="PENDING", unresolved=1), True),
    (make_pr(checks="SUCCESS"), False),
    (make_pr(checks="FAILURE"), False),
    (make_pr(), False),
    (make_pr(state="MERGED", checks="PENDING"), False),
])
def test_checks_running(pr, running):
    assert m.checks_running(pr) is running


@pytest.mark.parametrize("pr, state", [
    (make_pr(checks="PENDING", merge_state="UNSTABLE"), "waiting"),
    (make_pr(checks="PENDING", unresolved=2), "comments"),
    (make_pr(checks="FAILURE", unresolved=2), "failing"),
    (make_pr(checks="SUCCESS", merge_state="CLEAN"), "ready"),
    (make_pr(merge_state="CLEAN"), "ready"),
])
def test_classify(pr, state):
    assert m.classify(pr) == state


def test_poll_remembers_running_checks(app):
    load(app, make_pr(checks="PENDING", unresolved=1))
    assert app.info[KEY]["state"] == "comments" and app.info[KEY]["running"]
    load(app, make_pr(checks="SUCCESS", unresolved=1))
    assert not app.info[KEY]["running"]


def test_led_blinks_only_while_checks_run(app):
    load(app, make_pr(checks="PENDING"))
    p = app.prs[0]
    images = set()
    for _ in range(4):
        app.blink(None)
        images.add(app._led(p))
    assert images == {m.led_image(m.BLUE), m.led_image(m.BLUE, dim=True)}

    load(app, make_pr(checks="SUCCESS", merge_state="CLEAN"))
    app.blink(None)
    assert app.blink_on and app._led(p) is m.led_image(m.GREEN)


def test_checks_finishing_sends_one_banner(app, notes):
    load(app, make_pr(checks="PENDING"))
    load(app, make_pr(checks="SUCCESS", merge_state="CLEAN"))
    assert notes == ["login: Ready to merge"]
