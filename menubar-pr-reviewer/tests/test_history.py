"""The review history and the statistics built from it."""
from datetime import datetime

import history

# a Friday; the week started on Monday 5 October, the month on the 1st
NOW = datetime(2026, 10, 9, 12, 0).timestamp()


def at(day, hour=10):
    return datetime(2026, day[0], day[1], hour).timestamp()


def review(key, day, credits, **extra):
    return {"event": "review", "key": key, "at": at(day), "credits": credits, "status": "done", **extra}


EVENTS = [
    review("acme/web#1", (9, 28), 100),  # last month
    review("acme/web#2", (10, 2), 20, comments=3, must_fix=1, tokens=5e5, title="Two", author="bob",
           agents=[["Orchestrator", 5], ["Technical correctness", 15]]),
    {"event": "push", "key": "acme/web#2", "at": at((10, 2), 11), "posted": 3, "summary": True},
    review("acme/web#2", (10, 5), 15, comments=0, tokens=4e5, title="Two", author="bob"),  # Monday
    {"event": "approve", "key": "acme/web#2", "at": at((10, 5), 11)},
    review("acme/api#3", (10, 9), 4.2, status="stopped", title="Three"),
]


def test_periods_start_on_monday_and_the_first():
    starts = dict(history.periods(NOW))
    assert datetime.fromtimestamp(starts["Today"]) == datetime(2026, 10, 9)
    assert datetime.fromtimestamp(starts["This week"]) == datetime(2026, 10, 5)
    assert datetime.fromtimestamp(starts["This month"]) == datetime(2026, 10, 1)


def test_spending():
    assert history.spending(EVENTS, NOW) == [
        ("Today", 4.2, 1), ("This week", 19.2, 2), ("This month", 39.2, 3), ("All time", 139.2, 4)]


def test_daily():
    days = history.daily(EVENTS, 30, NOW)
    assert len(days) == 30 and days[-1][0] == datetime(2026, 10, 9).date()
    assert {d.day: (credits, n) for d, credits, n in days if n} == {28: (100, 1), 2: (20, 1), 5: (15, 1), 9: (4.2, 1)}


def test_by_agent_puts_the_rest_in_not_broken_down():
    agents = dict(history.by_agent(EVENTS, history.periods(NOW)[2][1]))
    assert agents == {"Technical correctness": 15, "Orchestrator": 5, "Not broken down": 19.2}


def test_by_pr_and_round_text():
    prs = history.by_pr(EVENTS)
    assert [pr["key"] for pr in prs] == ["acme/api#3", "acme/web#2", "acme/web#1"]  # the latest reviewed first
    two = prs[1]
    assert (two["title"], len(two["rounds"]), two["credits"], two["tokens"]) == ("Two", 2, 35, 9e5)
    assert history.round_text(two["rounds"][0]).endswith(
        "20.0 AIC · 500k tokens · 3 comments (1 must-fix) · pushed 3 and the summary")
    assert history.round_text(two["rounds"][1]).endswith("0 comments · approved")
    assert history.round_text(prs[0]["rounds"][0]).endswith("4.2 AIC · stopped")


def test_load_skips_broken_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    history.append(str(path), {"event": "review", "key": "a/b#1", "credits": 1})
    with open(path, "a") as f:
        f.write("not json\n{\"no\": \"event\"}\n")
    events = history.load(str(path))
    assert len(events) == 1 and events[0]["at"] > 0
    assert history.load(str(tmp_path / "missing.jsonl")) == []


MODEL_EVENTS = [
    review("acme/web#1", (10, 2), 20, models=["claude-sonnet-5.5"]),  # logged before credits per model
    {"event": "push", "key": "acme/web#1", "at": at((10, 2), 11), "posted": 2},
    review("acme/web#1", (10, 5), 30, models=[["claude-opus-5.5", 30]], model="claude-opus-5.5"),
    {"event": "approve", "key": "acme/web#1", "at": at((10, 5), 11)},
    review("acme/web#2", (10, 6), 12, models=[["claude-opus-5.5", 8], ["claude-haiku-5.5", 4]], model="auto"),
    review("acme/api#3", (10, 7), 3, status="stopped", models=[], model="claude-opus-5.5"),  # no final metrics
    review("acme/api#4", (10, 8), 1, status="stopped"),  # nothing known
]


def test_review_models():
    credits = [history.review_models(e) for e in MODEL_EVENTS if e["event"] == "review"]
    assert credits == [
        [("claude-sonnet-5.5", 20)],
        [("claude-opus-5.5", 30)],
        [("claude-opus-5.5", 8), ("claude-haiku-5.5", 4)],
        [("claude-opus-5.5", 3)],  # the model it was started with
        [("Unknown", 1)],
    ]


def test_by_model():
    assert history.by_model(MODEL_EVENTS, 0) == [
        ("claude-opus-5.5", 41), ("claude-sonnet-5.5", 20), ("claude-haiku-5.5", 4), ("Unknown", 1)]
    assert history.by_model(MODEL_EVENTS, at((10, 5))) == [
        ("claude-opus-5.5", 41), ("claude-haiku-5.5", 4), ("Unknown", 1)]


def test_filtering_by_model_keeps_the_reviews_that_used_it_and_what_followed_them():
    opus = history.with_model(MODEL_EVENTS, "claude-opus-5.5")
    assert [(e["key"], e["event"]) for e in opus] == [
        ("acme/web#1", "review"), ("acme/web#1", "approve"), ("acme/web#2", "review"), ("acme/api#3", "review")]
    prs = history.by_pr(opus)
    assert history.round_text(prs[-1]["rounds"][0]).endswith("claude-opus-5.5 · 30.0 AIC · 0 comments · approved")
    sonnet = history.with_model(MODEL_EVENTS, "claude-sonnet-5.5")
    assert [e["event"] for e in sonnet] == ["review", "push"]  # not the approval of the Opus round
    assert history.spending(sonnet, NOW)[3] == ("All time", 20, 1)


def test_round_text_names_every_model():
    mixed = history.by_pr(MODEL_EVENTS)[2]["rounds"][0]
    assert " · claude-opus-5.5, claude-haiku-5.5 · 12.0 AIC" in history.round_text(mixed)


def test_the_app_logs_credits_per_model(app, monkeypatch):
    logged = []
    monkeypatch.setattr(history, "append", lambda path, event: logged.append(event))
    job = {"status": "done", "started": 1, "model": "auto",
           "credits": {"total": 12.0, "agents": [], "tokens": 0, "models": [("claude-opus-5.5", 8.0001), ("x", 4)]}}
    app._log_review("acme/web#1", job)
    job["credits"]["models"] = ["claude-sonnet-5.5"]  # finished by an older version
    app._log_review("acme/web#1", job)
    assert [(e["models"], e["model"]) for e in logged] == [
        ([["claude-opus-5.5", 8.0], ["x", 4]], "auto"), (["claude-sonnet-5.5"], "auto")]
    assert history.review_models(dict(logged[1], credits=12.0)) == [("claude-sonnet-5.5", 12.0)]
