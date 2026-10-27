"""Reading Copilot's result and credits, and what gets posted to GitHub."""
import json

import pytest

import copilot_review as cr

DIFF = """diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -10,3 +10,4 @@ fn
 keep
-old
+new1
+new2
 tail
diff --git a/gone.txt b/gone.txt
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-x
"""


def comment(severity="SHOULD-FIX", title="Title", path="src/a.ts", line=11, body="Body."):
    return {"severity": severity, "title": title, "path": path, "line": line, "body": body, "source": "code"}


def test_commentable_lines():
    assert cr.commentable_lines(DIFF) == {"src/a.ts": {10, 11, 12, 13}}


@pytest.mark.parametrize("given, level", [
    ("MUST-FIX", "MUST-FIX"), ("[should-fix]", "SHOULD-FIX"), ("nit", "NITPICK"), ("QA", "QUESTION"),
    ("question", "QUESTION"), ("suggestion", "CONSIDER"), ("whatever", "CONSIDER"), (None, "CONSIDER"),
])
def test_severity(given, level):
    assert cr.severity(given) == level


def test_comment_text():
    assert cr.comment_text(comment()) == "**[SHOULD-FIX]** Title\n\nBody."
    assert cr.comment_text(comment(title="")) == "**[SHOULD-FIX]**\n\nBody."
    assert cr.comment_text(comment(), location=True) == "**[SHOULD-FIX]** Title\n\n`src/a.ts:11`\n\nBody."


def test_read_result_validates_and_orders_by_level(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"summary": "S", "comments": [
        {"severity": "nit", "title": "a", "path": "x.ts", "line": 2, "body": "n"},
        {"severity": "MUST-FIX", "title": "b", "path": "x.ts", "line": "3", "body": "m"},  # line isn't a number
        {"severity": "QUESTION", "path": "", "line": 5, "body": "q"},  # no path: a general comment
        {"severity": "MUST-FIX", "body": "  "},  # empty: dropped
        "not a comment",
    ]}))
    result = cr.read_result(str(tmp_path))
    assert result["summary"] == "S"
    assert [(c["severity"], c["path"], c["line"]) for c in result["comments"]] == [
        ("MUST-FIX", "x.ts", None), ("QUESTION", None, None), ("NITPICK", "x.ts", 2)]


def test_read_result_explains_a_missing_result(tmp_path):
    (tmp_path / "copilot.log").write_text(
        json.dumps({"type": "assistant.message", "data": {"content": "The sandbox blocked me."}}) + "\n")
    with pytest.raises(RuntimeError, match="The sandbox blocked me"):
        cr.read_result(str(tmp_path))


def test_a_missing_result_shows_why_the_session_stopped_not_a_subagents_report(tmp_path):
    events = [
        {"type": "assistant.message", "data": {"content": "Waiting for the four reviewers to finish."}},
        {"type": "assistant.message", "agentId": "a1",
         "data": {"content": '```json\n{"findings": []}\n```', "parentToolCallId": "toolu_1"}},
        {"type": "session.error", "data": {"errorType": "session_limits", "errorCode": "ai_credit_soft_cap_exhausted",
                                           "message": "Session limit reached (95.89/60 AI credits used)."}},
    ]
    (tmp_path / "copilot.log").write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(RuntimeError) as e:
        cr.read_result(str(tmp_path))
    assert str(e.value).startswith("Copilot stopped before writing a result: Session limit reached (95.89/60")
    assert "findings" not in str(e.value) and "copilot_max_ai_credits" in str(e.value)
    (tmp_path / "copilot.log").write_text("".join(json.dumps(e) + "\n" for e in events[:2]))
    with pytest.raises(RuntimeError, match="It said: Waiting for the four reviewers"):
        cr.read_result(str(tmp_path))


def test_duplicates_are_recognised():
    existing = [{"author": "bob", "path": "src/a.ts", "line": 11, "body": "This  should handle the NULL case."}]
    assert cr.already_on_pr(comment(body="this should handle the null case", title=""), existing)
    assert not cr.already_on_pr(comment(path="src/b.ts", body="This should handle the NULL case."), existing)
    assert not cr.already_on_pr(comment(path=None, body="Missing tests for retries."), existing)
    posted = [{"author": "me", "path": "src/a.ts", "line": 11, "body": cr.comment_text(comment())}]
    assert cr.already_on_pr(comment(), posted)  # an earlier push of the same comment


def test_summary_on_the_pr():
    existing = [{"author": "me", "path": None, "line": None, "body": "Looks good overall.\n\n---\n\nmore"}]
    assert cr.summary_on_pr("Looks  good overall.", existing)
    assert not cr.summary_on_pr("Something else entirely.", existing)


def test_push_puts_comments_on_their_lines_and_the_rest_in_the_text(tmp_path, monkeypatch):
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "diff.patch").write_text(DIFF)
    sent = []
    monkeypatch.setattr(cr, "api", lambda method, path, token, body=None, accept=None: sent.append((path, body)))
    cr.push("acme/web#7", str(tmp_path), "sha", [comment(line=11), comment(line=99), comment(path=None)], "t",
            summary="The summary.")
    path, review = sent[0]
    assert path == "/repos/acme/web/pulls/7/reviews"
    assert review["commit_id"] == "sha" and review["event"] == "COMMENT"
    assert [(c["path"], c["line"]) for c in review["comments"]] == [("src/a.ts", 11)]
    parts = review["body"].split("\n\n---\n\n")
    assert parts[0] == "The summary." and "`src/a.ts:99`" in parts[1] and len(parts) == 3


def test_approve_at_a_commit_or_the_latest(monkeypatch):
    sent = []
    monkeypatch.setattr(cr, "api", lambda method, path, token, body=None, accept=None: sent.append(body))
    cr.approve("acme/web#7", "sha", "t")
    cr.approve("acme/web#7", None, "t")
    assert sent == [{"event": "APPROVE", "commit_id": "sha"}, {"event": "APPROVE"}]


def write_events(directory, session, events):
    (directory / session).mkdir(parents=True)
    (directory / session / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")


def test_credits_per_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSION_STATE", str(tmp_path))
    write_events(tmp_path, "s1", [
        {"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 9e9}},
        {"type": "session.shutdown", "data": {
            "totalNanoAiu": 30e9,
            "modelMetrics": {
                "claude-sonnet-5.5": {"usage": {"inputTokens": 1000, "outputTokens": 50}, "totalNanoAiu": 18e9},
                "claude-haiku-5.5": {"usage": {"inputTokens": 0, "outputTokens": 0}, "totalNanoAiu": 12e9},
            },
            "agentMetrics": {
                "main": {"totalNanoAiu": 6e9},
                "a": {"agentName": "prr-code", "totalNanoAiu": 12e9},
                "b": {"agentName": "prr-docs", "totalNanoAiu": 5e9},
                "c": {"agentName": "prr-docs", "totalNanoAiu": 7e9},  # the same subagent twice: added up
            }}},
    ])
    credits = cr.credits(str(tmp_path), "s1")
    assert credits["total"] == 30
    assert credits["agents"] == [("Orchestrator", 6), ("Docs alignment", 12), ("Technical correctness", 12)]
    assert credits["tokens"] == 1050
    assert credits["models"] == [("claude-sonnet-5.5", 18), ("claude-haiku-5.5", 12)]  # the most credits first
    assert cr.credits_so_far("s1") == 9


def test_credits_without_a_final_summary(tmp_path, monkeypatch):
    """A stopped review: only its last checkpoint."""
    monkeypatch.setattr(cr, "SESSION_STATE", str(tmp_path))
    write_events(tmp_path, "s2", [{"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 4.5e9}}])
    assert cr.credits(str(tmp_path), "s2") == {"total": 4.5, "agents": [], "tokens": 0, "models": []}
    assert cr.credits_so_far("missing") is None


HELP_CONFIG = """\
  `logLevel`: log level for CLI; defaults to "default".

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag option.
    - "claude-sonnet-5.5"
    - "gpt-6.1-sol"
    - "kimi-k3"

  `contextTier`: context window tier for tiered-pricing models (e.g., "default" or "long_context").
    - Can also be set with --context flag (overrides persisted setting)
"""


def test_models_are_read_from_the_config_help():
    assert cr.parse_models(HELP_CONFIG) == ["claude-sonnet-5.5", "gpt-6.1-sol", "kimi-k3"]
    assert cr.parse_models("no models here") == []


def test_default_model_is_copilots_own_setting(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(cr, "COPILOT_SETTINGS", str(settings))
    assert cr.default_model() is None  # no settings file
    settings.write_text('{"model": "claude-sonnet-5.5", "contextTier": "long_context"}')
    assert cr.default_model() == "claude-sonnet-5.5"
    settings.write_text("// not JSON")
    assert cr.default_model() is None


def test_start_passes_the_model_effort_and_context(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(cr, "find_copilot", lambda: "copilot")
    monkeypatch.setattr(cr.subprocess, "Popen", lambda cmd, **kw: runs.append(cmd))
    cr.start("acme/web#1", str(tmp_path), "s", "main", model="claude-opus-5.5", effort="high", context="default")
    cr.start("acme/web#1", str(tmp_path), "s", "main")
    flags = [[a for a in cmd if a in ("--model", "--reasoning-effort", "--context")] for cmd in runs]
    assert flags == [["--model", "--reasoning-effort", "--context"], []]
    cmd = runs[0]
    assert cmd[cmd.index("--reasoning-effort") + 1] == "high" and cmd[cmd.index("--context") + 1] == "default"


PR = {"title": "Add a cache", "html_url": "https://github.com/acme/web/pull/7", "user": {"login": "Alice"},
      "base": {"ref": "main"}, "head": {"sha": "abc"}, "body": "Caches things."}


def test_pr_context_marks_an_admin_author_and_lists_the_admins():
    text = cr.pr_markdown("acme/web#7", PR, [], ["alice", "bob"])
    assert "acme/web#7 by Alice (admin): " in text
    assert "Repo admins (tech lead and architects): alice, bob\n" in text
    assert "by Alice (admin)" not in cr.pr_markdown("acme/web#7", PR, [], ["bob"])
    plain = cr.pr_markdown("acme/web#7", PR, [])
    assert "(admin)" not in plain and "Repo admins" not in plain


def test_existing_comments_mark_the_admins_and_their_approvals(monkeypatch):
    pages = {
        "/repos/acme/web/pulls/7/comments": [{"user": {"login": "bob"}, "path": "src/a.ts", "line": 3, "body": "Ok."}],
        "/repos/acme/web/issues/7/comments": [],
        "/repos/acme/web/pulls/7/reviews": [
            {"user": {"login": "Bob"}, "state": "APPROVED", "body": ""},
            {"user": {"login": "carol"}, "state": "COMMENTED", "body": ""},
            {"user": {"login": "carol"}, "state": "COMMENTED", "body": "Nice."},
        ],
    }
    monkeypatch.setattr(cr, "api_all", lambda path, token: pages[path])
    existing = cr.existing_comments("acme/web#7", "t")
    assert [(c["author"], c.get("approved", False)) for c in existing] == [
        ("bob", False), ("Bob", True), ("carol", False)]
    text = cr.comments_markdown(existing, ["bob"])
    assert "- bob (admin) on src/a.ts:3:\n\nOk.\n\n" in text
    assert "- Bob (admin) approved the PR\n\n" in text
    assert "- carol:\n\nNice.\n\n" in text
    assert cr.comments_markdown([]) == "(no comments yet)\n"
