# mac_tweaks

Personal macOS menu bar tools, one folder per tool, each built with [rumps](https://github.com/jaredks/rumps) (PyObjC underneath).

## Layout

- `<tool>/` has its own `.venv` (git-ignored), `requirements.txt`, `README.md` and `screenshots/`.
- The root `README.md` links every tool with a one-line description.
- Screenshots are Retina 2x PNGs, shown in READMEs at half their pixel width.

## Git

- One commit per tool (e.g. `Add menubar-pr`), with short messages.
- While polishing a tool, amend its commit and `git push --force-with-lease` instead of adding commits.

## Testing a menu bar app

- The user may be running their own instance. Check first with `ps -axo pid,lstart,command | grep "[p]r_watch"` (or the tool's script name).
- Never write test data to a tool's real config in `~/Library/Application Support/<tool>/`. Point the module's `CONFIG_PATH` at a scratch file instead.
- When driving the menu with System Events, target the test instance by PID (`first process whose unix id is <pid>`). `process "Python"` can match the user's instance.
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
- Check each shot for neighbouring icons or other content before committing it.

## rumps / PyObjC pitfalls

- `rumps.MenuItem` is a `dict` subclass, so an item without a submenu is falsy. Compare with `is None`.
- `rumps.Timer` fires immediately on `start()`, and only in the default run loop mode. It pauses while a menu is open unless its `_nstimer` is also added to `NSRunLoopCommonModes`. For a one-off delay, use `PyObjCTools.AppHelper.callLater`.
- Coloured or image-bearing titles: `app._nsapp.nsstatusitem.button().setAttributedTitle_(...)`, with images as `NSTextAttachment`s. `app.title` only takes plain text, and setting it overwrites the attributed title.
- The status item exists only after `run()`; hook `rumps.events.before_start`.
- Network or other slow work goes on a thread; hand results back with `AppHelper.callAfter`, because AppKit isn't thread-safe.
- `rumps.notification` fails when run from a venv (no bundle identifier). Use `osascript` with the text passed as `argv` to avoid quoting problems.
- `NSMenuItem.setSubtitle_` (macOS 14+) gives a native second line in menu items.
