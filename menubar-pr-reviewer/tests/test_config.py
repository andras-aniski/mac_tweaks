"""The config file, and the command line that edits it."""
import json

import pytest

import pr_reviewer as m


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(m, "CONFIG_PATH", str(path))
    return path


def test_defaults_for_missing_and_bad_values(config_path):
    config_path.write_text(json.dumps({"repos": [], "monthly_budget": -5, "poll_minutes": "often",
                                       "automation": {"wait_minutes": True, "review": "yes", "approve": True}}))
    config = m.load_config()
    assert "monthly_budget" not in config
    assert config["poll_minutes"] == 15
    assert config["automation"] == {"wait_minutes": 30, "review": False, "push": False, "approve": True}
    assert config["allow"] == [] and config["block"] == []


def test_review_model(config_path):
    config_path.write_text(json.dumps({"repos": [], "copilot_model": "gpt-6.1-sol"}))
    assert m.load_config()["copilot_model"] == "gpt-6.1-sol"
    for bad in ("", " ", 5, None):
        config_path.write_text(json.dumps({"repos": [], "copilot_model": bad}))
        assert "copilot_model" not in m.load_config()  # Copilot's default


def test_review_effort_and_context(config_path):
    config_path.write_text(json.dumps({"repos": []}))
    config = m.load_config()
    assert (config["copilot_effort"], config["copilot_context"]) == ("medium", "long_context")  # the defaults
    config_path.write_text(json.dumps({"repos": [], "copilot_effort": "max", "copilot_context": "default"}))
    config = m.load_config()
    assert (config["copilot_effort"], config["copilot_context"]) == ("max", "default")
    config_path.write_text(json.dumps({"repos": [], "copilot_effort": "huge", "copilot_context": 5}))
    config = m.load_config()
    assert (config["copilot_effort"], config["copilot_context"]) == ("medium", "long_context")


def test_a_broken_config_is_an_error(config_path):
    config_path.write_text(json.dumps({"repos": [{"owner": "a"}]}))
    with pytest.raises(ValueError):
        m.load_config()
    config_path.write_text(json.dumps({"repos": [{"owner": "a", "repo": "b", "admins": "alice"}]}))
    with pytest.raises(ValueError, match="admins"):
        m.load_config()


@pytest.mark.parametrize("text, repo", [
    ("owner/repo", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo/pull/12/files", "owner/repo"),
    ("not a repo", None),
])
def test_parse_repo(text, repo):
    parsed = m.parse_repo(text)
    assert (m.repo_key(parsed) if parsed else None) == repo


def test_parse_logins():
    assert m.parse_logins("alice, @Bob,  carol alice") == ["alice", "Bob", "carol"]


def cli(capsys, *args):
    m.cli(list(args))
    return capsys.readouterr().out.strip()


def test_cli(config_path, capsys):
    assert cli(capsys, "add", "https://github.com/acme/web/pull/3") == "Tracking acme/web"
    assert cli(capsys, "add", "acme/web") == "Already tracking acme/web"
    assert cli(capsys, "allow", "alice", "@Bob,carol") == "alice, Bob, carol"
    assert cli(capsys, "allow", "--remove", "bob") == "alice, carol"
    with pytest.raises(SystemExit, match="one list or the other"):
        m.cli(["block", "renovate[bot]"])
    assert cli(capsys, "allow", "--remove", "alice", "carol") == "(everyone)"
    assert cli(capsys, "block", "renovate[bot]") == "renovate[bot]"
    assert cli(capsys, "budget", "250") == "250 AIC a month, 0.0 used"
    assert cli(capsys, "budget", "--off") == "No limit, 0.0 AIC used this month"
    assert cli(capsys, "list").splitlines() == ["acme/web", "allow: (everyone)", "block: renovate[bot]"]
    assert cli(capsys, "admins", "Acme/Web", "alice", "@Bob") == "alice, Bob"
    assert cli(capsys, "admins", "acme/web", "bob") == "alice, Bob"
    assert cli(capsys, "list").splitlines()[0] == "acme/web  admins: alice, Bob"
    assert cli(capsys, "admins", "acme/web", "--remove", "alice", "bob") == "(none)"
    assert "admins" not in m.load_config()["repos"][0]
    with pytest.raises(SystemExit, match="Not tracked"):
        m.cli(["admins", "nope/x", "alice"])
    with pytest.raises(SystemExit):
        m.cli(["remove", "nope/x"])
    with pytest.raises(SystemExit):
        m.cli(["budget", "-5"])
    assert cli(capsys, "remove", "acme/web") == "Removed acme/web"


def test_subagents_that_are_off(config_path):
    config_path.write_text(json.dumps({"repos": []}))
    assert m.review_agents(m.load_config()) == m.copilot_review.SUBAGENTS  # all on by default
    config_path.write_text(json.dumps({"repos": [], "copilot_agents_off": ["prr-docs", "prr-gone", 5]}))
    config = m.load_config()
    assert config["copilot_agents_off"] == ["prr-docs"]
    assert "prr-docs" not in m.review_agents(config)
    for bad in ("prr-docs", m.copilot_review.SUBAGENTS):  # not a list; every one off
        config_path.write_text(json.dumps({"repos": [], "copilot_agents_off": bad}))
        assert m.load_config()["copilot_agents_off"] == []
