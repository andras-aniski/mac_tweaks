"""The Settings window: the tracked repos, how often to check, the author lists, the budget, the review model and
the automation.

Changes apply right away: a text field when you press Return or leave it, a checkbox or a pop-up when you click it. The app
calls update() whenever the config changes, also from outside the window (the command line, a hand edit).
"""
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBeep,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImageView,
    NSMenuItem,
    NSMutableAttributedString,
    NSPopUpButton,
    NSTextAlignmentRight,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakePoint, NSMakeRect, NSObject

from stats_view import FlippedView

WIDTH = 600
MARGIN = 20
LABEL_W = 170
CONTROL_X = MARGIN + LABEL_W + 10
CONTROL_W = WIDTH - CONTROL_X - MARGIN
REPO_ROW = 42

FIELDS = (  # name, label, width (None: the whole column), unit
    ("poll", "Check for PRs every", 60, "minutes"),
    ("allow", "Only PRs by", None, None),
    ("block", "Skip PRs by", None, None),
    ("budget", "Monthly AI credit budget", 80, "AIC"),
    ("wait", "Wait before reviewing", 60, "minutes"),
)
MODEL_W = 280

AUTOMATION = (
    ("review", "Review automatically", "PRs in To review, once the wait is over"),
    ("push", "Push comments automatically", "The comments of automatic reviews, once they're done"),
    ("approve", "Approve when there are no comments", "Finished reviews in To review that found nothing"),
)


class BoxView(FlippedView):
    def drawRect_(self, rect):
        NSColor.labelColor().colorWithAlphaComponent_(0.05).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 8, 8).fill()


class SettingsTarget(NSObject):
    def fire_(self, sender):
        self.callback()


class SettingsDelegate(NSObject):
    """The window's and the text fields' delegate."""

    def windowShouldClose_(self, window):
        window.makeFirstResponder_(None)  # commits the field being edited
        return True

    def controlTextDidChange_(self, notification):
        self.owner.edited(notification.object())


def _label(text, size=13, color=None, wrap=False):
    label = NSTextField.wrappingLabelWithString_(text) if wrap else NSTextField.labelWithString_(text)
    label.setFont_(NSFont.systemFontOfSize_(size))
    if color:
        label.setTextColor_(color)
    return label


def _colored(segments, size=11):
    """[(text, NSColor, rgb tuple, or None for the secondary color)] as an attributed string."""
    out = NSMutableAttributedString.alloc().init()
    for text, c in segments:
        if isinstance(c, tuple):
            c = NSColor.colorWithSRGBRed_green_blue_alpha_(*c, 1.0)
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: c or NSColor.secondaryLabelColor(),
        }))
    return out


def _height(label, width):
    if not label.stringValue():
        return 0
    return label.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10000)).height


class SettingsWindow:
    def __init__(self, actions):
        """actions: the app. It has track_repo(text) and set_poll / set_wait / set_budget(text), which return an
        error message or None, set_authors(name, text), remove_repo(key), set_automation(name, on),
        set_model(model or None) and show_prompt(_)."""
        self.actions = actions
        self.targets = []  # AppKit doesn't retain targets
        self.dirty = set()  # fields typed in but not committed yet: update() leaves them alone
        self.errors = {}  # field name -> error message
        self.repo_error = None
        self.config = self.repos = None
        self.month_spent, self.budget_share = 0.0, None
        self.models = (None, [])  # (Copilot's default model or None, the models it offers)
        self.model_values = None  # per item of the pop-up: the model id, None for the default, "-" for a separator
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, 400), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_("PR Reviewer Settings")
        self.window.setAutorecalculatesKeyViewLoop_(True)  # Tab goes through the fields top to bottom
        self.delegate = SettingsDelegate.alloc().init()
        self.delegate.owner = self
        self.window.setDelegate_(self.delegate)
        self.view = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 400))
        self.window.setContentView_(self.view)

        self.headings = {name: self._heading(name) for name in ("Repositories", "Pull requests", "Copilot reviews",
                                                                 "Automation")}
        self.repo_box = BoxView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH - 2 * MARGIN, REPO_ROW))
        self.view.addSubview_(self.repo_box)
        self.repo_field = NSTextField.textFieldWithString_("")
        self.repo_field.setPlaceholderString_("https://github.com/owner/repo, or owner/repo")
        self._on(self.repo_field, self._track)  # Return tracks it; leaving the field doesn't
        self.view.addSubview_(self.repo_field)
        self.track_button = self._button("Track", self._track)
        self.window.setInitialFirstResponder_(self.repo_field)  # not a Remove button: Space would press it
        self.repo_error_label = _label("", 11, NSColor.systemRedColor(), wrap=True)
        self.view.addSubview_(self.repo_error_label)

        self.fields = {}
        for name, text, width, unit in FIELDS:
            label = _label(text + ":")
            label.setAlignment_(NSTextAlignmentRight)
            field = NSTextField.textFieldWithString_("")
            field.cell().setSendsActionOnEndEditing_(True)
            field.setDelegate_(self.delegate)
            self._on(field, lambda name=name: self._commit(name))
            hint = _label("", 11, NSColor.secondaryLabelColor(), wrap=True)
            parts = [label, field, hint] + ([_label(unit)] if unit else [])
            for view in parts:
                self.view.addSubview_(view)
            self.fields[name] = {"label": label, "field": field, "hint": hint, "width": width,
                                 "unit": parts[3] if unit else None}
        self.model_label = _label("Review model:")
        self.model_label.setAlignment_(NSTextAlignmentRight)
        self.model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, MODEL_W, 25), False)
        self._on(self.model_popup, self._pick_model)
        self.model_hint = _label("", 11, NSColor.secondaryLabelColor(), wrap=True)
        for view in (self.model_label, self.model_popup, self.model_hint):
            self.view.addSubview_(view)
        self.checks = {}
        for name, title, hint in AUTOMATION:
            target = self._target(lambda name=name: self._toggle(name))
            box = NSButton.checkboxWithTitle_target_action_(title, target, "fire:")
            hint = _label(hint, 11, NSColor.secondaryLabelColor(), wrap=True)
            self.view.addSubview_(box)
            self.view.addSubview_(hint)
            self.checks[name] = (box, hint)
        self.prompt_button = self._button("Review prompt…", lambda: self.actions.show_prompt(None))
        self.close_button = self._button("Close", self.close)

    # building

    def _heading(self, text):
        label = NSTextField.labelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        self.view.addSubview_(label)
        return label

    def _target(self, callback):
        target = SettingsTarget.alloc().init()
        target.callback = callback
        self.targets.append(target)
        return target

    def _on(self, control, callback):
        control.setTarget_(self._target(callback))
        control.setAction_("fire:")

    def _button(self, title, callback, parent=None):
        button = NSButton.buttonWithTitle_target_action_(title, self._target(callback), "fire:")
        button.setBezelStyle_(NSBezelStyleRounded)
        (parent or self.view).addSubview_(button)
        return button

    def _fill_repos(self):
        for view in list(self.repo_box.subviews()):
            view.removeFromSuperview()
        width = WIDTH - 2 * MARGIN
        if not self.repos:
            label = _label("No repos tracked yet. Add one below.", 13, NSColor.secondaryLabelColor())
            label.setFrame_(NSMakeRect(12, (REPO_ROW - 17) / 2, width - 24, 17))
            self.repo_box.addSubview_(label)
        for i, (key, detail, image) in enumerate(self.repos):
            y = i * REPO_ROW
            led = NSImageView.imageViewWithImage_(image)
            led.setFrame_(NSMakeRect(12, y + (REPO_ROW - 14) / 2, 14, 14))
            name = _label(key)
            name.setFrame_(NSMakeRect(34, y + 5, width - 160, 17))
            info = _label(detail, 11, NSColor.secondaryLabelColor())
            info.setFrame_(NSMakeRect(34, y + 22, width - 160, 14))
            remove = self._button("Remove", lambda key=key: self._remove(key), self.repo_box)
            size = remove.frame().size
            remove.setFrameOrigin_(NSMakePoint(width - 10 - size.width, y + (REPO_ROW - size.height) / 2))
            for view in (led, name, info):
                self.repo_box.addSubview_(view)

    def _layout(self):
        inner = WIDTH - 2 * MARGIN
        y = MARGIN

        def place(view, x, width, height):
            view.setFrame_(NSMakeRect(x, y, width, height))

        def heading(name):
            nonlocal y
            y += 6
            place(self.headings[name], MARGIN, inner, 17)
            y += 25

        def hint(label, x, width):
            nonlocal y
            height = _height(label, width)
            place(label, x, width, height)
            y += height

        def field(name):
            nonlocal y
            f = self.fields[name]
            width = f["width"] or CONTROL_W
            y_row = y
            place(f["label"], MARGIN, LABEL_W, 17)
            f["label"].setFrameOrigin_(NSMakePoint(MARGIN, y_row + 3))
            place(f["field"], CONTROL_X, width, 22)
            if f["unit"]:
                f["unit"].setFrame_(NSMakeRect(CONTROL_X + width + 6, y_row + 3, 120, 17))
            y += 26
            hint(f["hint"], CONTROL_X, CONTROL_W)
            y += 10

        heading("Repositories")
        box_h = REPO_ROW * max(1, len(self.repos or ()))
        place(self.repo_box, MARGIN, inner, box_h)
        y += box_h + 10
        track_w = self.track_button.frame().size.width
        place(self.repo_field, MARGIN, inner - track_w - 8, 22)
        self.track_button.setFrameOrigin_(NSMakePoint(WIDTH - MARGIN - track_w, y + (22 - self.track_button.frame().size.height) / 2))
        y += 26
        hint(self.repo_error_label, MARGIN, inner)
        y += 10

        heading("Pull requests")
        for name in ("poll", "allow", "block"):
            field(name)
        heading("Copilot reviews")
        field("budget")
        place(self.model_label, MARGIN, LABEL_W, 17)
        self.model_label.setFrameOrigin_(NSMakePoint(MARGIN, y + 4))
        place(self.model_popup, CONTROL_X - 3, MODEL_W, 25)  # the pop-up's bezel is inset: line it up with the fields
        y += 30
        hint(self.model_hint, CONTROL_X, CONTROL_W)
        y += 10
        heading("Automation")
        field("wait")
        for box, label in self.checks.values():
            place(box, CONTROL_X, CONTROL_W, 18)
            y += 20
            hint(label, CONTROL_X + 20, CONTROL_W - 20)
            y += 10

        y += 10
        button_h = self.close_button.frame().size.height
        self.prompt_button.setFrameOrigin_(NSMakePoint(MARGIN, y))
        close_w = self.close_button.frame().size.width
        self.close_button.setFrameOrigin_(NSMakePoint(WIDTH - MARGIN - close_w, y))
        height = y + button_h + MARGIN

        frame = self.window.frame()
        new = self.window.frameRectForContentRect_(NSMakeRect(0, 0, WIDTH, height))
        if new.size.height != frame.size.height:  # grow or shrink downwards: the title bar stays put
            top = frame.origin.y + frame.size.height
            self.window.setFrame_display_(
                NSMakeRect(frame.origin.x, top - new.size.height, new.size.width, new.size.height), True
            )

    # showing the config

    def update(self, config, repos, month_spent, budget_share=None, models=(None, [])):
        """repos: [(owner/repo, status line, LED image)]. budget_share: (percentage, rgb) of the budget used.
        models: (the model Copilot uses by default or None, the models it offers)."""
        self.config, self.month_spent, self.budget_share, self.models = config, month_spent, budget_share, models
        if repos != self.repos:
            self.repos = repos
            self._fill_repos()
        self._refresh()

    def _refresh(self):
        config, automation = self.config, self.config["automation"]
        budget = config.get("monthly_budget")
        values = {
            "poll": f'{config["poll_minutes"]:g}',
            "allow": ", ".join(config["allow"]),
            "block": ", ".join(config["block"]),
            "budget": f"{budget:g}" if budget is not None else "",
            "wait": f'{automation["wait_minutes"]:g}',
        }
        hints = {
            "poll": "Each check is one GitHub request per repo and 50 open PRs.",
            "allow": "GitHub logins, separated by commas. Empty: everyone's PRs.",
            "block": "Their PRs never show up in To review.",
            "budget": [(f"{self.month_spent:.1f} AIC used this month", None)]
                      + ([(" (", None), self.budget_share, (")", None)] if self.budget_share else [])
                      + [(". Once the budget is used up, no new review starts. Empty: no limit.", None)],
            "wait": "After the PR is marked ready, pushed to, or replied to in your threads.",
        }
        for name, f in self.fields.items():
            if name not in self.dirty and f["field"].stringValue() != values[name]:
                f["field"].setStringValue_(values[name])
            error = self.errors.get(name)
            if error:
                f["hint"].setAttributedStringValue_(_colored([(error, NSColor.systemRedColor())]))
            elif isinstance(hints[name], list):
                f["hint"].setAttributedStringValue_(_colored(hints[name]))
            else:
                f["hint"].setAttributedStringValue_(_colored([(hints[name], None)]))
        self._fill_models()
        for name, (box, _) in self.checks.items():
            box.setState_(int(automation[name]))
        self.repo_error_label.setStringValue_(self.repo_error or "")
        self._layout()

    def _fill_models(self):
        default, models = self.models
        current = self.config.get("copilot_model")
        entries = [(None, f"Copilot default ({default})" if default else "Copilot default"),
                   ("auto", "Auto: Copilot picks"), ("-", None)]
        entries += [(model, model) for model in models if model != "auto"]
        if current and current not in [value for value, _ in entries]:
            entries.append((current, current))  # not offered (any more): keep showing what's saved
        values = [value for value, _ in entries]
        if values != self.model_values:
            self.model_values = values
            self.model_popup.removeAllItems()
            for value, title in entries:
                if value == "-":
                    self.model_popup.menu().addItem_(NSMenuItem.separatorItem())
                else:
                    self.model_popup.addItemWithTitle_(title)
        self.model_popup.selectItemAtIndex_(values.index(current))
        self.model_hint.setStringValue_(
            "Used by the reviews that start from now on. Copilot default: the model set in Copilot CLI (/model)."
        )

    # actions

    def edited(self, field):
        self.dirty.update(name for name, f in self.fields.items() if f["field"] is field)

    def _commit(self, name):
        if name not in self.dirty:
            return  # Return, then leaving the field: one commit is enough
        self.dirty.discard(name)
        text = self.fields[name]["field"].stringValue()
        if name in ("allow", "block"):
            self.errors[name] = self.actions.set_authors(name, text)
        else:
            self.errors[name] = getattr(self.actions, f"set_{name}")(text)
        if self.errors[name]:
            NSBeep()  # the field goes back to the saved value, and the hint says what was wrong
        self._refresh()

    def _track(self):
        text = self.repo_field.stringValue().strip()
        if not text:
            return
        self.repo_error = self.actions.track_repo(text)
        if self.repo_error:
            NSBeep()
        else:
            self.repo_field.setStringValue_("")
        self._refresh()

    def _remove(self, key):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Stop tracking {key}?")
        alert.setInformativeText_("Its PRs leave the menu, and their Copilot reviews are deleted.")
        alert.addButtonWithTitle_("Stop tracking")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() == NSAlertFirstButtonReturn:
            self.actions.remove_repo(key)

    def _pick_model(self):
        self.actions.set_model(self.model_values[self.model_popup.indexOfSelectedItem()])
        self._refresh()

    def _toggle(self, name):
        self.actions.set_automation(name, bool(self.checks[name][0].state()))
        self._refresh()  # back off when it asked first and you cancelled

    # the window

    def show(self, suggest=None, focus=None):
        """suggest: a repo to pre-fill the Track field with. focus: the field to put the cursor in."""
        if suggest and not self.repo_field.stringValue():
            self.repo_field.setStringValue_(suggest)
        if not self.window.isVisible():
            self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if focus:
            self.window.makeFirstResponder_(self.fields[focus]["field"])

    def close(self):
        self.window.makeFirstResponder_(None)  # commits the field being edited
        self.window.close()
