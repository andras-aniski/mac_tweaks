# mac_tweaks

Personal macOS customizations.

- [menubar-timer](menubar-timer/) — countdown timer in the menu bar
- [menubar-pr-watchlist](menubar-pr-watchlist/) — status LEDs for a watchlist of GitHub pull requests
- [menubar-pr-idle](menubar-pr-idle/) — flags the open pull requests of GitHub repos that nobody has touched for a while
- [menubar-pr-reviewer](menubar-pr-reviewer/) — new pull requests to review, with Copilot reviews you check before they are posted; keeps an eye on the ones you reviewed

## Commands

`bin/` has a command per tool: `pr-watchlist`, `pr-idle`, `pr-reviewer` and `timer`. Without arguments, a command starts the menu bar app in the background, the same as the `nohup` line in the tool's README, and refuses if it's already running. With arguments, it runs the tool's command line, e.g. `pr-watchlist list` or `pr-reviewer help`.

To install them, link them into `~/.local/bin` (it must be on your `PATH`). Run this from the repo root:

```sh
ln -sf "$PWD"/bin/* ~/.local/bin/
```
