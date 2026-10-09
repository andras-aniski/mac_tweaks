"""Copilot reviews of a PR: gather its context, run Copilot CLI headless, read the result and the AI credits, push.

Nothing here touches AppKit; the app calls these from worker threads.
"""
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import urllib.request

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
COPILOT_DIR = os.path.join(TOOL_DIR, "copilot")  # .github/agents/*.agent.md (loaded with --add-dir) and the prompt
PROMPT_PATH = os.path.join(COPILOT_DIR, "review-prompt.md")
SESSION_STATE = os.path.expanduser("~/.copilot/session-state")
COPILOT_SETTINGS = os.path.expanduser("~/.copilot/settings.json")
NANO_AIU = 1e9  # per AI credit

# Comment levels, most important first, as in the sb-fe-mfe review guidelines
SEVERITIES = ["MUST-FIX", "SHOULD-FIX", "QUESTION", "CONSIDER", "NITPICK"]
SEVERITY_ALIASES = {
    "MUSTFIX": "MUST-FIX", "MUST": "MUST-FIX", "BLOCKER": "MUST-FIX",
    "SHOULDFIX": "SHOULD-FIX", "SHOULD": "SHOULD-FIX",
    "QUESTION": "QUESTION", "QA": "QUESTION", "Q": "QUESTION",
    "CONSIDER": "CONSIDER", "SUGGESTION": "CONSIDER",
    "NITPICK": "NITPICK", "NIT": "NITPICK",
}

AGENT_LABELS = {
    "main": "Orchestrator",
    "prr-acceptance": "Acceptance criteria",
    "prr-docs": "Docs alignment",
    "prr-code": "Technical correctness",
    "prr-conventions": "Repo conventions",
}
# Copilot only reads and reports: the app posts the comments, after you've seen them. The PR's description and
# comments are written by others, so it gets an allowlist rather than --allow-all-tools: read-only shell commands,
# and writing its result file. Reading files inside the checkout and the --add-dir folders needs no permission.
ALLOWED_SHELL = [
    "git diff", "git log", "git show", "git blame", "git grep", "git status",
    "cd", "pwd", "ls", "cat", "head", "tail", "wc", "grep", "rg", "sort", "uniq", "diff",
]
# a backstop: deny rules win over allow rules
DENIED_TOOLS = ["shell(gh)", "shell(git push)", "shell(git fetch)", "shell(curl)", "shell(wget)", "url"]

CONTEXT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 10) { nodes { number title url body } }
    }
  }
}
"""


def split_key(key):
    """"owner/repo#123" -> ("owner", "repo", 123)"""
    repo, number = key.split("#")
    owner, name = repo.split("/")
    return owner, name, int(number)


def find_copilot():
    return shutil.which("copilot") or next(
        (p for p in ("/opt/homebrew/bin/copilot", "/usr/local/bin/copilot") if os.path.exists(p)), None
    )


def parse_models(help_text):
    """The model ids listed under `model` in `copilot help config`."""
    lines = help_text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("`model`:")), None)
    models = []
    for line in lines[start + 1:] if start is not None else []:
        match = re.fullmatch(r'\s*- "([^"]+)"\s*', line)
        if not match:
            break
        models.append(match.group(1))
    return models


def models():
    """The models Copilot CLI offers; [] if it can't tell. It has no command that lists only those."""
    copilot = find_copilot()
    if not copilot:
        return []
    try:
        out = subprocess.run([copilot, "help", "config"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_models(out.stdout)


def default_model():
    """The model Copilot uses without --model: the one set in Copilot CLI, or None."""
    try:
        with open(COPILOT_SETTINGS) as f:
            model = json.load(f).get("model")
    except (OSError, ValueError, AttributeError):
        return None
    return model if isinstance(model, str) and model else None


# --- GitHub ------------------------------------------------------------------

def api(method, path, token, body=None, accept="application/vnd.github+json"):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Accept": accept,
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return data.decode() if "diff" in accept else json.loads(data or b"null")


def api_all(path, token):
    items, page = [], 1
    while True:
        batch = api("GET", f"{path}?per_page=100&page={page}", token)
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def existing_comments(key, token):
    """Every comment on the PR, by anyone: [{"author", "path", "line", "body"}]; path is None for general ones."""
    owner, name, number = split_key(key)
    base = f"/repos/{owner}/{name}"

    def login(c):
        return (c.get("user") or {}).get("login", "ghost")

    comments = [
        {"author": login(c), "path": c["path"], "line": c.get("line") or c.get("original_line"), "body": c["body"]}
        for c in api_all(f"{base}/pulls/{number}/comments", token)
    ]
    comments += [
        {"author": login(c), "path": None, "line": None, "body": c["body"]}
        for c in api_all(f"{base}/issues/{number}/comments", token)
    ]
    comments += [
        {"author": login(r), "path": None, "line": None, "body": r["body"]}
        for r in api_all(f"{base}/pulls/{number}/reviews", token) if r.get("body")
    ]
    return comments


def _normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def already_on_pr(comment, existing):
    """True if an existing comment says (nearly) the same at the same place. Copilot already drops the ones that
    say it in other words; this catches the ones posted since the review, e.g. by an earlier push."""
    texts = {_normalize(comment["body"]), _normalize(comment_text(comment)), _normalize(comment_text(comment, True))}
    for e in existing:
        if comment["path"] and e["path"] not in (None, comment["path"]):
            continue
        other = _normalize(e["body"])
        if any(difflib.SequenceMatcher(None, text, other).ratio() >= 0.85 for text in texts):
            return True
    return False


def commentable_lines(diff):
    """{path: set of line numbers in the new version that are in the diff}: where inline comments can go."""
    lines, path, right = {}, None, None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
            right = None
        elif row.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", row)
            right = int(match.group(1)) if match else None
        elif path and right is not None and row[:1] in (" ", "+"):
            lines.setdefault(path, set()).add(right)
            right += 1
    return lines


def git(args, cwd=None, token=None):
    cmd, env = ["git"], dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        # the token goes through the environment, never on the command line or into a git config
        cmd += ["-c", "credential.helper=", "-c",
                'credential.helper=!f() { echo username=x-access-token; echo "password=$PRR_GIT_TOKEN"; }; f']
        env["PRR_GIT_TOKEN"] = token
    out = subprocess.run(cmd + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=1800)
    if out.returncode:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[-300:]}")
    return out.stdout


# --- Review ------------------------------------------------------------------

def prepare(key, workdir, clones_dir, token):
    """Write the PR's context to workdir/context and check out its head in workdir/checkout.

    Returns {"head": sha, "base": the base branch's ref in the checkout}.
    """
    owner, name, number = split_key(key)
    context = os.path.join(workdir, "context")
    os.makedirs(context, exist_ok=True)

    pr = api("GET", f"/repos/{owner}/{name}/pulls/{number}", token)
    issues = api("POST", "/graphql", token, {"query": CONTEXT_QUERY, "variables": {
        "owner": owner, "name": name, "number": number,
    }})["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    parts = [
        f'# {pr["title"]}\n\n{key} by {(pr.get("user") or {}).get("login", "ghost")}: {pr["html_url"]}\n'
        f'Base branch: {pr["base"]["ref"]} · head commit: {pr["head"]["sha"]}\n\n## Description\n\n'
        f'{pr.get("body") or "(no description)"}\n'
    ]
    for issue in issues:
        parts.append(f'\n## Linked issue #{issue["number"]}: {issue["title"]}\n\n{issue["url"]}\n\n'
                     f'{issue.get("body") or "(no description)"}\n')
    write(os.path.join(context, "pr.md"), "".join(parts))
    write(os.path.join(context, "diff.patch"),
          api("GET", f"/repos/{owner}/{name}/pulls/{number}", token, accept="application/vnd.github.diff"))
    existing = existing_comments(key, token)
    write(os.path.join(context, "existing-comments.md"), "".join(
        f'- {c["author"]}' + (f' on {c["path"]}:{c["line"]}' if c["path"] else "") + f':\n\n{c["body"]}\n\n'
        for c in existing
    ) or "(no comments yet)\n")

    # One full clone per repo, reused. Copilot's sandbox only reads inside the review folder, so each review gets
    # its own clone of it there (hardlinked, so it's quick): a worktree's .git would point outside.
    clone = os.path.join(clones_dir, owner, name)
    partial = os.path.isdir(clone) and subprocess.run(
        ["git", "config", "--get", "remote.origin.partialclonefilter"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    if partial or not os.path.isdir(os.path.join(clone, ".git")):  # partial: git would fetch blobs in the sandbox
        shutil.rmtree(clone, ignore_errors=True)
        git(["clone", "--no-checkout", f"https://github.com/{owner}/{name}.git", clone], token=token)
    base = pr["base"]["ref"]
    refs = [f"+refs/remotes/origin/{base}:refs/remotes/origin/{base}", f"+refs/prr/{number}:refs/prr/{number}"]
    git(["fetch", "--force", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
         f"+refs/pull/{number}/head:refs/prr/{number}"], cwd=clone, token=token)
    checkout = os.path.join(workdir, "checkout")
    remove_checkout(workdir)
    git(["clone", "--local", "--no-checkout", clone, checkout])
    git(["fetch", "--force", "--no-tags", "origin"] + refs, cwd=checkout)
    git(["checkout", "--quiet", "--detach", pr["head"]["sha"]], cwd=checkout)
    return {"head": pr["head"]["sha"], "base": f"origin/{base}"}


def remove_checkout(workdir):
    shutil.rmtree(os.path.join(workdir, "checkout"), ignore_errors=True)


def start(key, workdir, session_id, base, model=None, max_ai_credits=None):
    """Start Copilot in the background; returns the Popen. It outlives the app, so a restart can pick it up."""
    copilot = find_copilot()
    if not copilot:
        raise RuntimeError("Copilot CLI not found; install it with `brew install copilot-cli`")
    with open(PROMPT_PATH) as f:
        prompt = f.read()
    for name, value in {"key": key, "base": base}.items():
        prompt = prompt.replace("{{" + name + "}}", value)
    cmd = [
        copilot, "-p", prompt,
        "--no-ask-user", "--disable-builtin-mcps", "--sandbox", "--disallow-temp-dir",
        "--add-dir", COPILOT_DIR,
        "--session-id", session_id, "--name", f"Review {key}",
        "--output-format", "json",
        "--usage-output-file", os.path.join(workdir, "usage.json"),
        "--share", os.path.join(workdir, "session.md"),
        f'--allow-tool=write({os.path.join(workdir, "result.json")})',
    ]
    cmd += [f"--allow-tool=shell({command})" for command in ALLOWED_SHELL]
    cmd += [f"--deny-tool={tool}" for tool in DENIED_TOOLS]
    if model:
        cmd += ["--model", model]
    if max_ai_credits:
        cmd += ["--max-ai-credits", str(max_ai_credits)]
    # COPILOT_ALLOW_ALL would approve every tool, and "true" would also trust the checkout's hooks and MCP servers
    env = {k: v for k, v in os.environ.items() if k != "COPILOT_ALLOW_ALL"}
    log = open(os.path.join(workdir, "copilot.log"), "w")
    # The review folder is the working directory: the sandbox only reads there. It isn't the checkout either, so
    # Copilot doesn't load the PR's own instructions, agents or hooks.
    return subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def stop(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_result(workdir):
    """Copilot's comments: {"summary": str, "comments": [{"path", "line", "body", "source"}]}; raises if missing."""
    try:
        with open(os.path.join(workdir, "result.json")) as f:
            result = json.load(f)
    except FileNotFoundError:
        said = last_message(workdir)
        raise RuntimeError("Copilot finished without writing a result" + (f". It said: {said}" if said else "")
                           + f"\n\nLog: {workdir}/copilot.log") from None
    except ValueError as e:
        raise RuntimeError(f"Copilot wrote an invalid result.json: {e}") from None
    comments = []
    for c in result.get("comments") or []:
        if not (isinstance(c, dict) and isinstance(c.get("body"), str) and c["body"].strip()):
            continue
        path = c.get("path") if isinstance(c.get("path"), str) and c.get("path") else None
        line = c.get("line") if isinstance(c.get("line"), int) and path else None
        comments.append({
            "severity": severity(c.get("severity")),
            "title": str(c.get("title") or "").strip(),
            "path": path,
            "line": line,
            "body": c["body"].strip(),
            "source": str(c.get("source", "")),
        })
    comments.sort(key=lambda c: SEVERITIES.index(c["severity"]))  # stable: Copilot's order within a level
    return {"summary": str(result.get("summary") or ""), "comments": comments}


def severity(value):
    """One of SEVERITIES; anything unrecognised becomes CONSIDER, so nothing is overstated."""
    text = re.sub(r"[^A-Z]", "", str(value or "").upper())
    return SEVERITY_ALIASES.get(text, "CONSIDER")


def comment_text(comment, location=False):
    """The comment as posted: the repo review format's `**[LEVEL]** Title` heading, then the body.
    location: name the file and line too, for comments that go in the review's body."""
    heading = f'**[{comment["severity"]}]**' + (f' {comment["title"]}' if comment["title"] else "")
    where = ""
    if location and comment["path"]:
        where = f'`{comment["path"]}' + (f':{comment["line"]}' if comment["line"] else "") + "`\n\n"
    return f'{heading}\n\n{where}{comment["body"]}'


def last_message(workdir):
    """Copilot's last words in the run's JSONL output, e.g. why it gave up; None if it said nothing."""
    said = None
    try:
        with open(os.path.join(workdir, "copilot.log"), errors="ignore") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "assistant.message" and (event.get("data") or {}).get("content"):
                    said = event["data"]["content"].strip()
    except FileNotFoundError:
        pass
    return said[:600] if said else None


def _events(session_id):
    path = os.path.join(SESSION_STATE, session_id, "events.jsonl")
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def credits_so_far(session_id):
    """AI credits used by a running session, from its latest usage checkpoint; None before the first one."""
    total = None
    for e in _events(session_id):
        if e.get("type") == "session.usage_checkpoint":
            total = (e.get("data") or {}).get("totalNanoAiu", total)
    return None if total is None else total / NANO_AIU


def credits(workdir, session_id):
    """{"total": AI credits, "agents": [(label, AI credits)], "tokens": n, "models": [(id, AI credits)]}: the
    orchestrator and each subagent, and each model, the most credits first, from the session's final metrics. Without them (e.g. a stopped review) only the total is known."""
    summary = None
    for e in _events(session_id):
        if e.get("type") == "session.shutdown":
            summary = e.get("data") or {}
    if summary is None:
        try:
            with open(os.path.join(workdir, "usage.json")) as f:
                summary = json.load(f)
        except (FileNotFoundError, ValueError):
            summary = {}
    total = summary.get("totalNanoAiu")
    if total is None:
        so_far = credits_so_far(session_id)
        return {"total": so_far or 0.0, "agents": [], "tokens": 0, "models": []}
    model_metrics = summary.get("modelMetrics") or {}
    if not model_metrics:  # add up the agents' instead
        for metrics in (summary.get("agentMetrics") or {}).values():
            for model, m in (metrics.get("modelMetrics") or {}).items():
                total_m = model_metrics.setdefault(model, {"usage": {}, "totalNanoAiu": 0})
                total_m["totalNanoAiu"] += m.get("totalNanoAiu") or 0
                for k, v in (m.get("usage") or {}).items():
                    total_m["usage"][k] = total_m["usage"].get(k, 0) + (v or 0)
    tokens = sum((m.get("usage") or {}).get("inputTokens", 0) + (m.get("usage") or {}).get("outputTokens", 0)
                 for m in model_metrics.values())
    per_agent = {}
    for agent_id, metrics in (summary.get("agentMetrics") or {}).items():
        name = "main" if agent_id == "main" else metrics.get("agentName") or agent_id
        per_agent[name] = per_agent.get(name, 0) + (metrics.get("totalNanoAiu") or 0)
    order = list(AGENT_LABELS)
    agents = sorted(per_agent.items(), key=lambda item: order.index(item[0]) if item[0] in order else len(order))
    return {
        "total": total / NANO_AIU,
        "agents": [(AGENT_LABELS.get(name, name), nano / NANO_AIU) for name, nano in agents],
        "tokens": tokens,  # input (cached included) and output, every agent
        "models": sorted(((model, (m.get("totalNanoAiu") or 0) / NANO_AIU) for model, m in model_metrics.items()),
                         key=lambda item: -item[1]),
    }


def to_push(key, comments, token, summary=None):
    """(the comments that aren't on the PR yet, how many were left out because they are, the summary unless it's on
    the PR already)."""
    existing = existing_comments(key, token)
    fresh = [c for c in comments if not already_on_pr(c, existing)]
    if summary and summary_on_pr(summary, existing):
        summary = None
    return fresh, len(comments) - len(fresh), summary


def summary_on_pr(summary, existing):
    """True if a review's text already has the summary (it's posted at the top of it)."""
    text = _normalize(summary)
    return any(text in _normalize(e["body"]) or difflib.SequenceMatcher(None, text, _normalize(e["body"])).ratio() >= 0.85
               for e in existing if e["path"] is None)


def push(key, workdir, head, comments, token, summary=None):
    """Post the comments as one review on the reviewed commit. Its text is the summary, if given, and the comments on
    lines outside the diff."""
    owner, name, number = split_key(key)
    with open(os.path.join(workdir, "context", "diff.patch")) as f:
        lines = commentable_lines(f.read())
    inline = [c for c in comments if c["path"] and c["line"] in lines.get(c["path"], ())]
    general = [c for c in comments if c not in inline]
    review = {
        "commit_id": head,
        "event": "COMMENT",
        "comments": [
            {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": comment_text(c)} for c in inline
        ],
    }
    body = "\n\n---\n\n".join(([summary] if summary else []) + [comment_text(c, location=True) for c in general])
    if body:
        review["body"] = body
    api("POST", f"/repos/{owner}/{name}/pulls/{number}/reviews", token, review)


def head_sha(key, token):
    """The PR's current head commit."""
    owner, name, number = split_key(key)
    return api("GET", f"/repos/{owner}/{name}/pulls/{number}", token)["head"]["sha"]


def approve(key, head, token):
    """Approve the PR at a commit; head None: at its latest one."""
    owner, name, number = split_key(key)
    review = {"event": "APPROVE"}
    if head:
        review["commit_id"] = head
    api("POST", f"/repos/{owner}/{name}/pulls/{number}/reviews", token, review)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
