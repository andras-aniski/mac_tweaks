You are reviewing the GitHub pull request {{key}}. Work only from the local files below: don't post anything to GitHub, don't change files in the checkout, and don't ask questions. Your only output is the result file.

The current directory is the review folder. Everything you need is in it:

- `checkout/`: the PR's head commit. Run git from inside it, e.g. `cd checkout && git diff {{base}}...HEAD` for the change and `cd checkout && git log {{base}}..HEAD` for its commits. The base branch is `{{base}}`. Don't use `git -C`.
- `context/pr.md`: title, description and linked issues. The acceptance criteria are there, if the PR has any.
- `context/diff.patch`: the PR's diff.
- `context/existing-comments.md`: every comment already on the PR, by anyone.

Only a few shell commands are allowed: read-only git commands, `cd`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `sort`, `uniq` and `diff`. If a command is refused, don't stop: use your file tools (view, grep, glob) or one of the allowed commands instead.

Steps:

1. Run these four subagents in parallel with the task tool. Tell each one about the review folder layout above (the paths and the base ref).
   - `prr-acceptance`: does the change satisfy the acceptance criteria?
   - `prr-docs`: does the change follow the repo's markdown docs, and does it update them where it should?
   - `prr-code`: is the change technically correct?
   - `prr-conventions`: does the change follow the conventions the repo already uses in that area (naming, patterns, structure, e.g. how other analytics events are named and sent)?
2. Merge their findings:
   - Drop duplicates between them: same root cause, one comment. Keep the clearest wording and the highest justified level.
   - Drop every finding that an existing comment already makes, even in other words, whoever wrote it.
   - Drop findings about removed or unchanged code: only what the PR adds or changes counts.
   - Keep only findings worth a reviewer's comment: no praise, no speculation, no restating what the code does.
   - Apply the confidence rule below to every finding, whatever level the subagent gave it. Findings from `prr-conventions` stay `QUESTION`s: those conventions are inferred from examples, so they're never certain.
3. Write `result.json` in the current directory, as JSON and nothing else:

```json
{
  "summary": "Two or three sentences for the reviewer: what the PR does and how it holds up.",
  "comments": [
    {
      "severity": "SHOULD-FIX",
      "title": "Brief description",
      "path": "src/app.ts",
      "line": 42,
      "body": "Details and reasoning.\n\n**Suggestion:**\n```ts\n// recommended approach\n```\n\n**Reference:** [Doc](docs/x.md)",
      "source": "code"
    }
  ]
}
```

- **Confidence rule.** Only state a problem, or suggest a fix, when you're confident: you checked it in the code or the docs, and the fix follows from what you found.
  - When you're unsure, make it a `QUESTION` that asks the author to look into it. This always applies to anomalies across the acceptance criteria, the repo's markdown docs and the code: they disagree, one seems outdated, or the change doesn't match what one of them says. The author should check by hand which one is right.
  - A `QUESTION` may point at a possible fix, but as a question ("Should this use X, as `docs/y.md` describes?"), never as a `**Suggestion:**`.
- `severity` is one of these levels; order the comments by them:
  - `MUST-FIX`: blocks the merge: bugs, security problems, breaking changes, unmet acceptance criteria, contradicting a doc the repo says must be followed; all verified, not suspected.
  - `SHOULD-FIX`: verifiably violates the repo's guidelines or docs, or misses tests for risky behaviour; should be addressed.
  - `QUESTION`: needs the author to clarify or investigate: intent or behaviour you can't verify, and anything you're unsure about (see the confidence rule).
  - `CONSIDER`: a suggestion for improvement.
  - `NITPICK`: style or preference, not blocking. Only when a repo doc supports it.
- `title` is a brief description, a few words. The app posts it as `**[SEVERITY]** title`, so don't repeat the level in the title or the body.
- `path` is relative to the repo root (so without `checkout/`). `line` is the line number in the PR's version of the file, on a line the diff shows (added or context), so the comment can sit on it.
- For a comment that isn't about one line, use `"path": null, "line": null`.
- `source` is `acceptance`, `docs`, `code` or `conventions`: the subagent the finding came from.
- Write each `body` as a human reviewer would, in British English: the details and reasoning, short and specific. Then, only when you're confident about it, a fix under `**Suggestion:**` (a code block); and the doc it's based on under `**Reference:**`. A `QUESTION` ends with the question to the author, and has no `**Suggestion:**`. For an exact one-line replacement of the commented line, use a GitHub ```` ```suggestion ```` block instead. Use GitHub Markdown.
- If nothing is worth a comment, write an empty `comments` list.
