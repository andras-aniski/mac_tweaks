"""The Review statistics window: stat tiles, credits per day, per agent and per model, and the reviewed PRs, for
every model or one.

Drawn with AppKit. Colors follow the dataviz reference palette: one series hue (blue, with its own dark-mode
step), recessive grid and axes in system colors, status colors only for the budget, always with a label.
"""
import math

import objc
from AppKit import (
    NSApp,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPopUpButton,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSView,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSObject

import history

WIDTH = 760
MARGIN = 20


def _hex(value):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255, 1.0
    )


def _dynamic(light, dark):
    """A color with its own step for light and dark mode."""
    def provide(appearance):
        dark_mode = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
        ) == NSAppearanceNameDarkAqua
        return _hex(dark if dark_mode else light)
    return NSColor.colorWithName_dynamicProvider_(None, provide)


SERIES = _dynamic("#2a78d6", "#3987e5")  # categorical slot 1
WARNING = _hex("#fab219")  # status colors: never for a series, always with a label
CRITICAL = _hex("#d03b3b")


def _text(s, size=11, color=None, weight=NSFontWeightRegular, digits=False):
    font = (NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if digits
            else NSFont.systemFontOfSize_weight_(size, weight))
    return NSAttributedString.alloc().initWithString_attributes_(s, {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color or NSColor.labelColor(),
    })


def _nice_max(value):
    """The axis maximum: the next 1, 2, 2.5 or 5 times a power of ten."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    return next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= value)


def _bar(x, y, w, h, radius, top=True):
    """A bar with its data end rounded: the top (vertical bars) or the right end (horizontal ones)."""
    radius = min(radius, w / 2, h / 2)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), radius, radius)
    path.appendBezierPathWithRect_(
        NSMakeRect(x, y + h - radius, w, radius) if top else NSMakeRect(x, y, radius, h)  # square the base
    )
    path.fill()


class TileView(NSView):
    """A stat tile: a label, a number of AI credits, a detail line, and an optional budget bar."""

    @objc.python_method
    def setup(self, title, credits, detail, budget=None):
        """budget: (fraction used, label, status color or None) or None."""
        self.title, self.credits, self.detail, self.budget = title, credits, detail, budget

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        b = self.bounds()
        NSColor.labelColor().colorWithAlphaComponent_(0.05).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, 8, 8).fill()
        secondary = NSColor.secondaryLabelColor()
        _text(self.title, 11, secondary).drawAtPoint_(NSMakePoint(12, 10))
        value = _text(f"{self.credits:.1f}", 22, None, NSFontWeightSemibold, digits=True)
        value.drawAtPoint_(NSMakePoint(12, 26))
        _text(" AIC", 12, secondary).drawAtPoint_(NSMakePoint(12 + value.size().width, 36))
        _text(self.detail, 11, secondary).drawAtPoint_(NSMakePoint(12, 58))
        if self.budget:
            fraction, label, status = self.budget
            track = NSMakeRect(12, b.size.height - 16, b.size.width - 24, 5)
            NSColor.separatorColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(track, 2.5, 2.5).fill()
            (status or SERIES).set()
            width = track.size.width * min(fraction, 1)
            if width > 0:
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(track.origin.x, track.origin.y, max(width, 5), 5), 2.5, 2.5
                ).fill()
            text = _text(label, 10, secondary)
            text.drawAtPoint_(NSMakePoint(b.size.width - 12 - text.size().width, 10))


class DailyChartView(NSView):
    """AI credits per day: one bar per day, three gridlines, a date every week, a tooltip on every day."""

    LEFT, RIGHT, TOP, BOTTOM = 44, 6, 8, 22

    @objc.python_method
    def setup(self, days):
        """days: [(date, AI credits, reviews)], oldest first."""
        self.days = days
        self.removeAllToolTips()
        plot_w = self.bounds().size.width - self.LEFT - self.RIGHT
        slot = plot_w / len(days)
        plot_h = self.bounds().size.height - self.TOP - self.BOTTOM
        for i in range(len(days)):
            # the view itself is the owner: AppKit doesn't retain tooltip owners, so a string owner gets freed
            self.addToolTipRect_owner_userData_(NSMakeRect(self.LEFT + i * slot, self.TOP, slot, plot_h), self, None)

    def view_stringForToolTip_point_userData_(self, view, tag, point, data):
        slot = (self.bounds().size.width - self.LEFT - self.RIGHT) / len(self.days)
        i = min(max(int((point[0] - self.LEFT) // slot), 0), len(self.days) - 1)
        day, credits, reviews = self.days[i]
        return f'{day.strftime("%a %-d %b")}: {credits:.1f} AIC, {reviews} review{"s" if reviews != 1 else ""}'

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        b = self.bounds()
        plot_w = b.size.width - self.LEFT - self.RIGHT
        plot_h = b.size.height - self.TOP - self.BOTTOM
        top = _nice_max(max(credits for _, credits, _ in self.days))
        secondary = NSColor.secondaryLabelColor()
        for fraction in (0, 0.5, 1):  # gridlines, recessive
            y = self.TOP + plot_h * (1 - fraction)
            NSColor.separatorColor().set()
            NSBezierPath.fillRect_(NSMakeRect(self.LEFT, round(y) - 0.5, plot_w, 1))
            label = _text(f"{top * fraction:g}", 10, secondary, digits=True)
            label.drawAtPoint_(NSMakePoint(self.LEFT - 6 - label.size().width, y - label.size().height / 2))
        slot = plot_w / len(self.days)
        bar_w = max(2, slot * 0.62)
        SERIES.set()
        for i, (day, credits, _) in enumerate(self.days):
            if credits > 0:
                h = max(2, plot_h * credits / top)
                _bar(self.LEFT + i * slot + (slot - bar_w) / 2, self.TOP + plot_h - h, bar_w, h, 4)
        last = len(self.days) - 1
        for i, (day, _, _) in enumerate(self.days):
            if (last - i) % 7 == 0:  # a date every week, ending today
                label = _text("Today" if i == last else day.strftime("%-d %b"), 10, secondary)
                x = self.LEFT + i * slot + slot / 2 - label.size().width / 2
                x = min(max(x, self.LEFT), b.size.width - label.size().width)
                label.drawAtPoint_(NSMakePoint(x, b.size.height - self.BOTTOM + 6))


class BarsView(NSView):
    """Horizontal bars: AI credits per agent or model, each labelled with its name and value."""

    ROW, LABEL_W, VALUE_W = 26, 160, 70

    @objc.python_method
    def setup(self, agents):
        """agents: [(label, AI credits)]"""
        self.agents = agents

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        b = self.bounds()
        top = max(credits for _, credits in self.agents) or 1
        bar_max = b.size.width - self.LABEL_W - self.VALUE_W
        for i, (label, credits) in enumerate(self.agents):
            y = i * self.ROW
            name = _text(label, 12)
            name.drawAtPoint_(NSMakePoint(0, y + (self.ROW - name.size().height) / 2))
            w = max(2, bar_max * credits / top)
            SERIES.set()
            _bar(self.LABEL_W, y + 8, w, self.ROW - 16, 4, top=False)
            value = _text(f"{credits:.1f} AIC", 11, NSColor.secondaryLabelColor(), digits=True)
            value.drawAtPoint_(NSMakePoint(self.LABEL_W + w + 8, y + (self.ROW - value.size().height) / 2))


class FlippedView(NSView):
    def isFlipped(self):
        return True


class _Target(NSObject):
    def fire_(self, sender):
        self.callback()


class StatsWindow:
    HEIGHT = 760

    def __init__(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, self.HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_("Review statistics")
        self.window.setContentMinSize_(NSMakeSize(WIDTH, 400))
        self.window.setContentMaxSize_(NSMakeSize(WIDTH, 4000))  # fixed width: the charts are laid out for it
        view = self.window.contentView()
        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 52, WIDTH, self.HEIGHT - 52))
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        view.addSubview_(self.scroll)
        self.target = _Target.alloc().init()
        self.target.callback = self.window.close
        close = NSButton.buttonWithTitle_target_action_("Close", self.target, "fire:")
        close.setBezelStyle_(NSBezelStyleRounded)
        close.setKeyEquivalent_("\r")
        close.setFrameOrigin_(NSMakePoint(WIDTH - MARGIN - close.frame().size.width, 12))
        close.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        view.addSubview_(close)
        label = NSTextField.labelWithString_("Model:")
        label.sizeToFit()
        label.setFrameOrigin_(NSMakePoint(MARGIN, 12 + (close.frame().size.height - label.frame().size.height) / 2))
        label.setAutoresizingMask_(NSViewMaxYMargin)
        view.addSubview_(label)
        self.model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(MARGIN + label.frame().size.width + 6, 12, 240, close.frame().size.height), False)
        self.model_popup.setAutoresizingMask_(NSViewMaxYMargin)
        self.model_target = _Target.alloc().init()
        self.model_target.callback = self._pick_model
        self.model_popup.setTarget_(self.model_target)
        self.model_popup.setAction_("fire:")
        view.addSubview_(self.model_popup)
        self.model = None  # None: every model
        self.data = None

    def update(self, events, budget=None, month_spent=None, now=None):
        """budget: AI credits a month, or None. month_spent: this month's credits, running reviews included."""
        self.data = (events, budget, month_spent, now)
        models = [model for model, _ in history.by_model(events, 0)]
        if self.model not in models:
            self.model = None
        self.model_popup.removeAllItems()
        self.model_popup.addItemsWithTitles_(["All models"] + models)
        self.model_popup.selectItemAtIndex_(models.index(self.model) + 1 if self.model else 0)
        self._render()

    def _pick_model(self):
        i = self.model_popup.indexOfSelectedItem()
        self.model = self.model_popup.itemTitleAtIndex_(i) if i > 0 else None
        self._render()

    def _render(self):
        """The statistics of every review, or of the ones that used the picked model."""
        events, budget, month_spent, now = self.data
        if self.model:
            events = history.with_model(events, self.model)
            month_spent = None  # running reviews' models aren't known yet
        doc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 100))
        inner = WIDTH - 2 * MARGIN
        y = MARGIN

        def heading(text, size=13):
            nonlocal y
            label = NSTextField.labelWithString_(text)
            label.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold))
            label.setFrame_(NSMakeRect(MARGIN, y, inner, 20))
            doc.addSubview_(label)
            y += 26

        def add(view, height):
            nonlocal y
            view.setFrame_(NSMakeRect(MARGIN, y, inner, height))
            doc.addSubview_(view)
            y += height + 28

        # tiles
        spending = history.spending(events, now)
        if month_spent is None:
            month_spent = spending[2][1]
        running = month_spent - spending[2][1]  # what running reviews have spent so far: they started recently
        heading("AI credits spent on reviews" + (f" with {self.model}" if self.model else ""), 15)
        gap = 12
        tile_w = (inner - 3 * gap) / 4
        for i, (label, credits, reviews) in enumerate(spending):
            tile = TileView.alloc().initWithFrame_(NSMakeRect(MARGIN + i * (tile_w + gap), y, tile_w, 96))
            detail = f'{reviews} review{"s" if reviews != 1 else ""}'
            meter = None
            credits += running
            if label == "This month":
                if budget and not self.model:  # the budget is for every model
                    fraction = month_spent / budget
                    status = CRITICAL if fraction >= 1 else WARNING if fraction >= 0.8 else None
                    word = "⚠︎ Over budget" if fraction >= 1 else f"{fraction:.0%} of {budget:g}"
                    meter = (fraction, ("⚠︎ " if status is WARNING else "") + word, status)
            tile.setup(label, credits, detail, meter)
            doc.addSubview_(tile)
        y += 96 + 28

        # credits per day
        heading("Per day, the last 30 days")
        chart = DailyChartView.alloc().initWithFrame_(NSMakeRect(MARGIN, y, inner, 170))
        chart.setup(history.daily(events, 30, now))
        add(chart, 170)

        # credits per agent
        heading("This month, by agent")
        agents = history.by_agent(events, history.periods(now)[2][1])
        if agents:
            bars = BarsView.alloc().initWithFrame_(NSMakeRect(0, 0, inner, 10))
            bars.setup(agents)
            add(bars, len(agents) * BarsView.ROW)
        else:
            add(NSTextField.labelWithString_("No reviews this month."), 18)

        # credits per model
        models = history.by_model(events, history.periods(now)[2][1])
        if models and not self.model:
            heading("This month, by model")
            bars = BarsView.alloc().initWithFrame_(NSMakeRect(0, 0, inner, 10))
            bars.setup(models)
            add(bars, len(models) * BarsView.ROW)

        # the reviewed PRs
        prs = history.by_pr(events)
        heading(f"Reviewed PRs ({len(prs)})")
        text = NSTextView.alloc().initWithFrame_(NSMakeRect(MARGIN, y, inner, 100))
        text.setEditable_(False)
        text.setDrawsBackground_(False)
        text.setTextContainerInset_(NSMakeSize(0, 0))
        text.textContainer().setLineFragmentPadding_(0)
        text.textStorage().setAttributedString_(self._pr_list(prs))
        layout = text.layoutManager()
        layout.ensureLayoutForTextContainer_(text.textContainer())
        height = layout.usedRectForTextContainer_(text.textContainer()).size.height + 4
        text.setFrame_(NSMakeRect(MARGIN, y, inner, height))
        doc.addSubview_(text)
        y += height + MARGIN

        doc.setFrameSize_(NSMakeSize(WIDTH, y))
        self.scroll.setDocumentView_(doc)
        doc.scrollPoint_(NSMakePoint(0, 0))

    @staticmethod
    def _pr_list(prs):
        secondary = NSColor.secondaryLabelColor()
        indented = NSMutableParagraphStyle.alloc().init()
        indented.setFirstLineHeadIndent_(14)
        indented.setHeadIndent_(14)
        out = NSMutableAttributedString.alloc().init()

        def add(s, size=13, color=None, weight=NSFontWeightRegular, style=None):
            text = NSMutableAttributedString.alloc().initWithAttributedString_(_text(s, size, color, weight))
            if style:
                text.addAttribute_value_range_(NSParagraphStyleAttributeName, style, (0, text.length()))
            out.appendAttributedString_(text)

        if not prs:
            add("No reviews yet. Reviews are logged from when statistics were added.\n", 13, secondary)
        for i, pr in enumerate(prs):
            n = len(pr["rounds"])
            add(("\n" if i else "") + f'#{pr["key"].split("#")[1]} {pr["title"]}\n', 13, None, NSFontWeightSemibold)
            details = [pr["key"].split("#")[0], pr["author"], f'{n} round{"s" if n != 1 else ""}',
                       f'{pr["credits"]:.1f} AIC']
            if pr["tokens"]:
                details.append(f'{history.tokens_text(pr["tokens"])} tokens')
            add(" · ".join(d for d in details if d) + "\n", 11, secondary)
            for j, r in enumerate(pr["rounds"], 1):
                add(f"Round {j}: {history.round_text(r)}\n", 12, None, NSFontWeightRegular, indented)
        return out

    def show(self):
        if not self.window.isVisible():
            self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
