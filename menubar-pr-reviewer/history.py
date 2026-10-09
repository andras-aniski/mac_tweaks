"""The review history: one JSON object per line, appended as things happen.

Events (all have "event", "key" and "at", a Unix time):
- review: a Copilot review ended. "status" (done, failed or stopped), "started", "title", "author", "credits",
  "agents" ([[label, credits]]), "tokens", "models" ([[model, credits]], the most first; model ids alone in
  older events), "model" (what it was started with: a model id, "auto", or missing), "comments", "must_fix"
- push: its comments were posted. "posted", "skipped"
- approve: the PR was approved from the review window
- discard: the review was discarded
"""
import json
import time
from datetime import datetime, timedelta


def append(path, event):
    with open(path, "a") as f:
        f.write(json.dumps(dict(event, at=event.get("at") or time.time())) + "\n")


def load(path):
    events = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") and event.get("key") and event.get("at"):
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def periods(now=None):
    """[(label, start)]: today, this week (from Monday), this month and all time, in local time."""
    today = datetime.fromtimestamp(now or time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        ("Today", today.timestamp()),
        ("This week", (today - timedelta(days=today.weekday())).timestamp()),
        ("This month", today.replace(day=1).timestamp()),
        ("All time", 0),
    ]


def spending(events, now=None):
    """[(label, AI credits, reviews)] for each period."""
    reviews = [e for e in events if e["event"] == "review"]
    return [
        (label, sum(e.get("credits") or 0 for e in reviews if e["at"] >= start),
         sum(1 for e in reviews if e["at"] >= start))
        for label, start in periods(now)
    ]


def daily(events, days=30, now=None):
    """[(date, AI credits, reviews)] for each of the last `days` days, today included, oldest first."""
    today = datetime.fromtimestamp(now or time.time()).date()
    totals = {today - timedelta(days=i): [0.0, 0] for i in range(days)}
    for e in events:
        if e["event"] == "review":
            day = totals.get(datetime.fromtimestamp(e["at"]).date())
            if day:
                day[0] += e.get("credits") or 0
                day[1] += 1
    return [(day, *totals[day]) for day in sorted(totals)]


def by_agent(events, since):
    """[(agent, AI credits)] of the reviews since a time, the most expensive first. Stopped reviews, and ones
    without a per-agent breakdown, are in "Not broken down"."""
    totals, rest = {}, 0.0
    for e in events:
        if e["event"] != "review" or e["at"] < since:
            continue
        agents = e.get("agents") or []
        for label, credits in agents:
            totals[label] = totals.get(label, 0) + credits
        rest += (e.get("credits") or 0) - sum(credits for _, credits in agents)
    if rest > 0.05:
        totals["Not broken down"] = rest
    return sorted(totals.items(), key=lambda item: -item[1])


def review_models(e):
    """[(model, AI credits)] of a review event. Without per-model credits: an older event's one model, or the model
    it was started with (a stopped review has no final metrics), else "Unknown"."""
    models = e.get("models") or []
    credits = e.get("credits") or 0
    if models and all(isinstance(m, list) for m in models):
        return [(model, c) for model, c in models]
    if models:
        return [(" + ".join(models), credits)]
    return [(e.get("model") or "Unknown", credits)]


def by_model(events, since):
    """[(model, AI credits)] of the reviews since a time, the most expensive first."""
    totals = {}
    for e in events:
        if e["event"] == "review" and e["at"] >= since:
            for model, credits in review_models(e):
                totals[model] = totals.get(model, 0) + credits
    return sorted(totals.items(), key=lambda item: -item[1])


def with_model(events, model):
    """The reviews that used a model, whole, and the pushes, approvals and discards that followed them."""
    kept, last = [], {}  # key -> whether its latest review is kept
    for e in sorted(events, key=lambda e: e["at"]):
        if e["event"] == "review":
            last[e["key"]] = model in [m for m, _ in review_models(e)]
        if last.get(e["key"]):
            kept.append(e)
    return kept


def by_pr(events):
    """The reviewed PRs, the most recently reviewed first:
    [{"key", "title", "author", "rounds", "credits", "tokens", "last"}], where each round is a review event with
    "actions": the push, approve and discard events that followed it."""
    prs = {}
    for e in sorted(events, key=lambda e: e["at"]):
        pr = prs.setdefault(e["key"], {"key": e["key"], "title": "", "author": "", "rounds": []})
        if e["event"] == "review":
            pr["title"] = e.get("title") or pr["title"]
            pr["author"] = e.get("author") or pr["author"]
            pr["rounds"].append(dict(e, actions=[]))
        elif pr["rounds"]:
            pr["rounds"][-1]["actions"].append(e)
    reviewed = [pr for pr in prs.values() if pr["rounds"]]
    for pr in reviewed:
        pr["credits"] = sum(r.get("credits") or 0 for r in pr["rounds"])
        pr["tokens"] = sum(r.get("tokens") or 0 for r in pr["rounds"])
        pr["last"] = pr["rounds"][-1]["at"]
    return sorted(reviewed, key=lambda pr: -pr["last"])


def tokens_text(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def round_text(r):
    """One review round, e.g. "9 Oct 10:20 · claude-sonnet-5.5 · 31.7 AIC · 1.2M tokens · 4 comments (1 must-fix) · pushed 3, approved"."""
    parts = [datetime.fromtimestamp(r["at"]).strftime("%-d %b %H:%M")]
    models = [m for m, _ in review_models(r) if m != "Unknown"]
    if models:
        parts.append(", ".join(models))
    parts.append(f'{r.get("credits") or 0:.1f} AIC')
    if r.get("tokens"):
        parts.append(f'{tokens_text(r["tokens"])} tokens')
    if r.get("status") == "done":
        n = r.get("comments") or 0
        parts.append(f'{n} comment{"s" if n != 1 else ""}' + (f' ({r["must_fix"]} must-fix)' if r.get("must_fix") else ""))
    else:
        parts.append(r.get("status") or "?")
    done = []
    for a in r["actions"]:
        if a["event"] == "push":
            done.append(f'pushed {a.get("posted", 0)}' + (" and the summary" if a.get("summary") else ""))
        elif a["event"] == "approve":
            done.append("approved")
        elif a["event"] == "discard":
            done.append("discarded")
    if done:
        parts.append(", ".join(done))
    return " · ".join(parts)
