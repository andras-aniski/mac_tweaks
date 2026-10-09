# mac_tweaks

Personal macOS menu bar tools, one folder per tool, each built with [rumps](https://github.com/jaredks/rumps) (PyObjC underneath).

## Layout

- `<tool>/` has its own `.venv` (git-ignored), `requirements.txt`, `README.md` and `screenshots/`.
- The root `README.md` links every tool with a one-line description.
- A tool's `README.md` has a `## Features` list right after its intro and screenshots: one bullet per feature, with its name, what it does and how to use it, in plain words. Only what's worth knowing, not every detail. Update it in the same change as the feature, without being asked.
- Screenshots are Retina 2x PNGs, shown in READMEs at half their pixel width.

## Git

- One commit per tool (e.g. `Add menubar-pr`), with short messages.
- While polishing a tool, amend its commit and `git push --force-with-lease` instead of adding commits.

## Testing a menu bar app

- Logic (rules, parsing, state changes) gets pytest tests in `<tool>/tests/`, on scratch folders with the network stubbed; `pytest` goes in the tool's `requirements-dev.txt`. See `menubar-pr-reviewer/tests/`. Run them before committing, and when fixing a logic bug, add a test that fails without the fix. Menus and windows are checked by hand, as below.
- The user may be running their own instance. Check first with `ps -axo pid,lstart,command | grep "[p]r_watch"` (or the tool's script name).
- Never write test data to a tool's real config in `~/Library/Application Support/<tool>/`. Point the module's `CONFIG_PATH` at a scratch file instead.
- When driving the menu with System Events, target the test instance by PID (`first process whose unix id is <pid>`). `process "Python"` can match the user's instance.
- System Events can't address a status item whose title has an image attachment, and can't reach buttons inside menu rows. Use the accessibility API from a scratch venv with `pyobjc-framework-ApplicationServices`: `AXUIElementCreateApplication(pid)` → `AXExtrasMenuBar` → the item; `AXPress` it to open or close the menu, then find buttons and checkboxes in the rows and windows by `AXRole` and `AXTitle`.
- Never send a synthetic Escape keypress: it lands in the terminal and interrupts Claude Code. Close menus by clicking the item again.
- Stop a test instance by PID. The venv python hands off to Homebrew's `Python.app`, so the process shows as `.../Python.app/Contents/MacOS/Python <script>.py`.
- `screencapture` and System Events need `dangerouslyDisableSandbox`.

## Screenshots

`scripts/shoot.py` runs the app, puts a solid dark panel behind the area it captures, takes one shot, and quits. Run it with the tool's venv python from the repo root:

```sh
# menu bar item only / with the menu open
menubar-timer/.venv/bin/python scripts/shoot.py menubar-timer timer:TimerApp menubar-timer/screenshots/idle.png
menubar-timer/.venv/bin/python scripts/shoot.py menubar-timer timer:TimerApp menubar-timer/screenshots/menu.png --menu

# --exec runs once the app is up (`app` is the instance); --wait gives it time to render
menubar-timer/.venv/bin/python scripts/shoot.py menubar-timer timer:TimerApp menubar-timer/screenshots/running.png --exec "app.start(25)" --wait 3

# --setup runs before the app is created (`m` is the module): keep test data out of the real config
menubar-pr/.venv/bin/python scripts/shoot.py menubar-pr pr_watch:PRWatchApp menubar-pr/screenshots/menu.png \
    --setup "m.CONFIG_PATH='<scratchpad>/test-config.json'" --wait 6 --menu
```

- It exits with an error if the item is hidden behind the notch, which happens when the menu bar is full. Ask the user to quit some menu bar apps.
- The scale factor it prints tells whether the shot is 2x. The item can land on an external, non-Retina display.
- Check each shot for neighbouring icons or other content before committing it. Paint the menu bar strip outside the item's highlight with the backdrop colour if needed.
- When the menu bar is crowded, the test item can be hidden while the menu still opens: the strip then shows the user's own items (their instance, the camera/mic indicator). Discard such a shot. If the camera indicator is on, the user may be in a call: don't pop up test menus or windows then.
- Screenshots show made-up repos, people and PRs only: stub the fetch in `--setup`, never point a shot at real data.

## rumps / PyObjC pitfalls

- `rumps.MenuItem` is a `dict` subclass, so an item without a submenu is falsy. Compare with `is None`.
- `rumps.Timer` fires immediately on `start()`, and only in the default run loop mode. It pauses while a menu is open unless its `_nstimer` is also added to `NSRunLoopCommonModes`. For a one-off delay, use `PyObjCTools.AppHelper.callLater`.
- Coloured or image-bearing titles: `app._nsapp.nsstatusitem.button().setAttributedTitle_(...)`, with images as `NSTextAttachment`s. `app.title` only takes plain text, and setting it overwrites the attributed title.
- The status item exists only after `run()`; hook `rumps.events.before_start`.
- Network or other slow work goes on a thread; hand results back with `AppHelper.callAfter`, because AppKit isn't thread-safe.
- `rumps.notification` fails when run from a venv (no bundle identifier). Use `osascript` with the text passed as `argv` to avoid quoting problems.
- `NSMenuItem.setSubtitle_` (macOS 14+) gives a native second line in menu items.
- Views in menu items (rows with buttons, see `menubar-pr-reviewer`): the menu gives them no highlight or clicks, so the view handles both. `AppHelper.callAfter` runs while a menu is open; use it to act on a button without closing the menu, and never rebuild the menu inside the button's own action. After rebuilding an open menu, call `menu._menu.update()` so items without an action turn grey again. `bezelColor` is ignored in menus: colour the button's `attributedTitle` instead.
- Tooltip rects don't retain their owner: a Python `str` owner is freed and crashes the app on hover. Make the view the owner and implement `view_stringForToolTip_point_userData_`.
