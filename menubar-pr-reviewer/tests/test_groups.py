"""Which group a PR is in, and when it moves: the rules in the README's diagram."""
import pytest

import pr_reviewer as m
from conftest import HOUR, NOW, groups, key, load, make_pr

CASES = [
    # (what, the PR, its group)
    ("not reviewed", {}, "to review"),
    ("my own", {"author": "me"}, None),
    ("commented, one of my threads open", {"review": "COMMENTED", "threads": (1, 2), "pushed_h": 1}, "reviewed"),
    ("changes requested, all resolved, pushed since",
     {"review": "CHANGES_REQUESTED", "threads": (3, 3), "pushed_h": 1}, "to review"),
    ("all resolved, nothing since my review", {"review": "COMMENTED", "threads": (2, 2), "pushed_h": 20}, "reviewed"),
    ("all resolved, a reply since my review",
     {"review": "COMMENTED", "threads": (2, 2), "pushed_h": 20, "replied_h": 1}, "to review"),
    ("no threads, pushed since", {"review": "COMMENTED", "pushed_h": 1}, "reviewed"),
    ("my review requested again", {"review": "COMMENTED", "requested": True}, "to review"),
    ("approved", {"review": "APPROVED", "pushed_h": 20}, "approved"),
    ("approved, new commits since", {"review": "APPROVED", "pushed_h": 1}, "approved"),
    ("approved, my review requested again", {"review": "APPROVED", "requested": True}, "to review"),
    ("approval dismissed", {"review": "DISMISSED", "pushed_h": 1}, "to review"),
]


@pytest.mark.parametrize("what, pr, group", CASES, ids=[c[0] for c in CASES])
def test_group(app, what, pr, group):
    load(app, make_pr(1, **pr))
    assert [name for name, numbers in groups(app).items() if 1 in numbers] == ([group] if group else [])


def test_ignore_and_put_back(app):
    load(app, make_pr(1))
    app.ignore(key(1))
    assert groups(app)["ignored"] == [1]
    app.unignore(key(1), None)
    assert groups(app)["to review"] == [1]


def test_a_reviewed_pr_ignores_the_ignore_list(app):
    load(app, make_pr(1, review="COMMENTED", threads=(0, 1)))
    app.ignore(key(1))
    assert groups(app)["reviewed"] == [1]


def test_author_lists(app):
    app.config.update(allow=["Ann", "bob"], block=["bob", "renovate[bot]"])
    load(app, make_pr(1, author="ann"), make_pr(2, author="bob"), make_pr(3, author="carol"),
         make_pr(4, author="bob", review="COMMENTED", threads=(0, 1)))
    assert groups(app)["to review"] == [1]  # carol isn't allowed, bob is blocked
    assert groups(app)["reviewed"] == [4]  # the lists don't apply to PRs I reviewed


def test_bot_logins_match_with_or_without_the_suffix():
    assert m.author_ok("renovate", {"allow": [], "block": ["renovate[bot]"]}) is False
    assert m.author_ok("dependabot[bot]", {"allow": ["dependabot"], "block": []}) is True


def test_to_review_order(app):
    """Back from Reviewed first, then the newest ready for review."""
    load(app, make_pr(1, ready_h=30), make_pr(2, ready_h=5),
         make_pr(3, ready_h=90, review="COMMENTED", threads=(1, 1), pushed_h=2))
    assert groups(app)["to review"] == [3, 2, 1]


def test_finished_reviews_are_listed_in_to_approve(app):
    load(app, make_pr(1), make_pr(2), make_pr(3))
    app.state["reviews"][key(2)] = {"status": "done", "session": "s", "started": 0, "comments": 0,
                                    "credits": {"total": 1.0, "agents": []}}
    app.state["reviews"][key(3)] = {"status": "running", "session": "s", "started": 0, "pid": 1}
    app._render_menu(*app._groups())
    to_review, to_approve = app.menu_structure[:2]
    assert to_review == [key(1), key(3)]  # running and unreviewed ones stay
    assert to_approve == [key(2)]


def test_new_prs_notify_once_and_not_on_the_first_poll(app, notes):
    load(app, make_pr(1))
    assert notes == []  # already open when the repo was added
    load(app, make_pr(1), make_pr(2))
    load(app, make_pr(1), make_pr(2))
    assert notes == [f"New PR to review: {key(2)}"]


def test_coming_back_to_review_notifies(app, notes):
    load(app, make_pr(1, review="COMMENTED", threads=(1, 2), pushed_h=1))
    load(app, make_pr(1, review="COMMENTED", threads=(2, 2), pushed_h=1))
    assert notes == [f"Back to review: {key(1)} (comments resolved)"]


def test_closed_prs_are_forgotten(app):
    load(app, make_pr(1), make_pr(2))
    app.ignore(key(1))
    load(app, make_pr(2))
    assert app.state["ignored"] == []
    assert app.state["known"]["acme/web"] == [2]


def test_menu_bar_count_is_what_waits_on_me(app):
    titles = []
    app._render_title = titles.append
    del app._render  # the real one, to see the count it passes on
    app._render_menu = lambda *a: None
    load(app, make_pr(1), make_pr(2, review="COMMENTED", threads=(0, 1)), make_pr(3, review="APPROVED"))
    app._render()
    assert titles[-1] == 1


def test_summarize_keeps_an_approval_after_a_comment_review():
    def node(**extra):
        base = {
            "number": 1, "title": "t", "url": "u", "isDraft": False, "createdAt": "2026-10-01T09:00:00Z",
            "author": {"login": "ann"}, "reviewRequests": {"nodes": []}, "commits": {"nodes": []},
            "pushes": {"nodes": []}, "ready": {"nodes": []}, "reviewThreads": {"nodes": []},
            "reviews": {"nodes": []},
        }
        return {**base, **extra}

    reviews = {"nodes": [{"state": "APPROVED", "submittedAt": "2026-10-02T09:00:00Z"},
                         {"state": "COMMENTED", "submittedAt": "2026-10-03T09:00:00Z"}]}
    threads = {"nodes": [
        {"isResolved": True, "started": {"nodes": [{"author": {"login": "Me"}}]},
         "latest": {"nodes": [{"createdAt": "2026-10-04T09:00:00Z"}]}},
        {"isResolved": False, "started": {"nodes": [{"author": {"login": "bob"}}]},
         "latest": {"nodes": [{"createdAt": "2026-10-05T09:00:00Z"}]}},
    ]}
    pr = m.summarize(node(reviews=reviews, reviewThreads=threads), "me")
    assert pr["review"]["state"] == "APPROVED"
    assert pr["review"]["at"] == m.parse_time("2026-10-03T09:00:00Z")  # the last review, whatever its state
    assert pr["threads"] == (1, 1)  # only the threads I started
    assert pr["replied"] == m.parse_time("2026-10-04T09:00:00Z")


def test_a_dismissed_approval_comes_back_with_a_notification(app, notes):
    load(app, make_pr(1, review="APPROVED"))
    load(app, make_pr(1, review="DISMISSED", pushed_h=1))
    assert groups(app)["to review"] == [1]
    assert notes == [f"Back to review: {key(1)} (review dismissed)"]


def summarized(*states):
    """summarize() of a PR with my reviews in these states, an hour apart."""
    node = {
        "number": 1, "title": "t", "url": "u", "isDraft": False, "createdAt": "2026-10-01T09:00:00Z",
        "author": {"login": "ann"}, "reviewRequests": {"nodes": []}, "commits": {"nodes": []},
        "pushes": {"nodes": []}, "ready": {"nodes": []}, "reviewThreads": {"nodes": []},
        "reviews": {"nodes": [{"state": s, "submittedAt": f"2026-10-02T{9 + i:02}:00:00Z"} for i, s in enumerate(states)]},
    }
    return m.summarize(node, "me")


def test_a_comment_review_after_a_dismissal_counts():
    """A dismissed review no longer stands, so a comment review after it is my state, and the PR is reviewed again."""
    assert summarized("APPROVED", "COMMENTED")["review"]["state"] == "APPROVED"
    assert summarized("DISMISSED", "COMMENTED")["review"]["state"] == "COMMENTED"
    assert summarized("COMMENTED", "DISMISSED")["review"]["state"] == "DISMISSED"
    assert summarized("DISMISSED", "COMMENTED", "APPROVED")["review"]["state"] == "APPROVED"


def test_a_review_of_an_older_head_says_so(app):
    load(app, make_pr(1, ready_h=5, pushed_h=1))
    pr = app._tracked()[key(1)]
    job = {"status": "done", "started": NOW - 2 * HOUR, "comments": 2, "credits": {"total": 30.0}}
    assert (" · new commits", m.AMBER) in app._review_detail(job, pr, NOW)
    job["started"] = NOW - HOUR / 2  # after the last push
    assert (" · new commits", m.AMBER) not in app._review_detail(job, pr, NOW)
