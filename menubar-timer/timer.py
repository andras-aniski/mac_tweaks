import rumps
import os
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem


def install_edit_menu():
    """Give the app a main menu with an Edit menu. A menu bar app shows none, but without one ⌘C, ⌘V, ⌘X, ⌘A and ⌘Z
    do nothing in its text fields: AppKit finds those shortcuts through the main menu."""
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), (None, None, None), ("Cut", "cut:", "x"),
                               ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        if title is None:
            edit.addItem_(NSMenuItem.separatorItem())
        else:  # no target: the action goes to the focused field
            edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    item.setSubmenu_(edit)
    main = NSMenu.alloc().initWithTitle_("")
    main.addItem_(item)
    NSApplication.sharedApplication().setMainMenu_(main)


class TimerApp(rumps.App):
    def __init__(self):
        super().__init__("⏳", quit_button="Quit")
        # Menu bar only: no Dock icon.
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        install_edit_menu()
        self.remaining = 0
        self.timer = rumps.Timer(self.tick, 1)
        self.menu = [
            rumps.MenuItem("5 min", callback=lambda _: self.start(5)),
            rumps.MenuItem("10 min", callback=lambda _: self.start(10)),
            rumps.MenuItem("15 min", callback=lambda _: self.start(15)),
            rumps.MenuItem("25 min", callback=lambda _: self.start(25)),
            rumps.MenuItem("Custom…", callback=self.custom),
            None,
            rumps.MenuItem("Stop", callback=self.stop),
        ]

    def start(self, minutes):
        self.remaining = int(round(minutes * 60))
        self._update_title()
        self.timer.stop()
        self.timer.start()

    def stop(self, _=None):
        self.timer.stop()
        self.remaining = 0
        self.title = "⏳"

    def tick(self, _):
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.stop()
            self.title = "⏳"
            self._alert_done()
        else:
            self._update_title()

    def _update_title(self):
        m, s = divmod(self.remaining, 60)
        self.title = f"{m}:{s:02d}"

    def _alert_done(self):
        os.system("afplay /System/Library/Sounds/Glass.aiff &")
        os.system(
            'osascript -e \'display notification "Time is up!" '
            'with title "Timer"\''
        )

    def custom(self, _):
        response = rumps.Window(
            message="How many minutes?",
            title="Custom timer",
            default_text="30",
            ok="Start",
            cancel="Cancel",
            dimensions=(120, 24),
        ).run()
        if response.clicked:
            text = response.text.strip().replace(",", ".")
            try:
                minutes = float(text)
            except ValueError:
                rumps.alert("Invalid value", "Enter a number, e.g. 30")
                return
            if minutes > 0:
                self.start(minutes)


if __name__ == "__main__":
    TimerApp().run()
