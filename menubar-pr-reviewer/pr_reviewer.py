import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from functools import partial

import objc
import rumps

import copilot_review
import history
import settings_view
import stats_view
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBezelBorder,
    NSBezierPath,
    NSButton,
    NSColor,
    NSControlSizeSmall,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSGradient,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSLineBreakByTruncatingHead,
    NSLineBreakByTruncatingTail,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScrollView,
    NSTextAlignmentCenter,
    NSTextAlignmentRight,
    NSTextAttachment,
    NSTextTab,
    NSTextField,
    NSTextView,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSObject, NSRunLoop, NSRunLoopCommonModes
from PyObjCTools import AppHelper

DEFAULT_POLL_MINUTES = 15
AUTOMATION_DEFAULTS = {"wait_minutes": 30, "review": False, "push": False, "approve": False}
RETRY_SECONDS = 120
STARTUP_GRACE = 5 * 60  # nothing automatic this soon after the app starts: time to see what's due
MAX_LISTED = 10  # per group; the rest go in a submenu
API_URL = "https://api.github.com/graphql"
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#]\S*)?$")
CONFIG_PATH = os.path.join(rumps.application_support("menubar-pr-reviewer"), "config.json")

ORANGE = (1.00, 0.58, 0.00)
GREEN = (0.20, 0.78, 0.35)
RED = (1.00, 0.27, 0.23)
GRAY = (0.56, 0.56, 0.58)
BLUE = (0.25, 0.55, 1.00)
AMBER = (0.98, 0.70, 0.10)

SEVERITY_COLORS = {  # the comment levels of copilot_review.SEVERITIES
    "MUST-FIX": RED,
    "SHOULD-FIX": ORANGE,
    "QUESTION": BLUE,
    "CONSIDER": GRAY,
    "NITPICK": GRAY,
}

REVIEW_STATES = {  # my latest review -> (label, LED)
    "APPROVED": ("Approved", GREEN),
    "CHANGES_REQUESTED": ("Changes requested", RED),
    "COMMENTED": ("Commented", GRAY),
    "DISMISSED": ("Reviewed", GRAY),  # always back in To review, which says it was dismissed
}

# Pending (unsubmitted) reviews don't count. A comment review doesn't change an earlier approval or change request,
# as on GitHub, so the last 20 are fetched; it does replace a dismissed one. Pushes = the last commit and the last force push.
# My threads = review threads that I started (up to 100 per PR), and when they last had a reply.
QUERY = """
query($owner: String!, $name: String!, $after: String, $me: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt
        author { login }
        reviews(author: $me, last: 20, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
          nodes { state submittedAt }
        }
        reviewRequests(first: 50) { nodes { requestedReviewer { ... on User { login } } } }
        reviewThreads(first: 100) {
          nodes {
            isResolved
            started: comments(first: 1) { nodes { author { login } } }
            latest: comments(last: 1) { nodes { createdAt } }
          }
        }
        commits(last: 1) { nodes { commit { committedDate } } }
        pushes: timelineItems(last: 1, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {
          nodes { ... on HeadRefForcePushedEvent { createdAt } }
        }
        ready: timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) {
          nodes { ... on ReadyForReviewEvent { createdAt } }
        }
      }
    }
  }
}
"""


# --- Icons -------------------------------------------------------------------

LED_SIZE = 14.0
LED_CORE = 8.0
_led_cache = {}


def led_image(rgb):
    """A flat, solid dot with a soft glow, like the caps-lock / MagSafe LEDs."""
    if rgb in _led_cache:
        return _led_cache[rgb]
    base = NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)

    def draw(_rect):
        c = LED_SIZE / 2
        center = NSMakePoint(c, c)
        halo = NSGradient.alloc().initWithStartingColor_endingColor_(
            base.colorWithAlphaComponent_(0.45), base.colorWithAlphaComponent_(0.0)
        )
        halo.drawFromCenter_radius_toCenter_radius_options_(center, LED_CORE / 2 - 1, center, c, 0)

        base.set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(c - LED_CORE / 2, c - LED_CORE / 2, LED_CORE, LED_CORE)
        ).fill()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(LED_SIZE, LED_SIZE), False, draw)
    _led_cache[rgb] = image
    return image


def glasses_image(font, rgb=None):
    """The menu bar icon: orange when there is something to review, otherwise a template that follows the menu bar."""
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(font.pointSize(), 0.0)
    if rgb:
        config = config.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_(
                [NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)]
            )
        )
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_("eyeglasses", "PRs to review")
    image = image.imageWithSymbolConfiguration_(config)
    image.setTemplate_(rgb is None)
    return image


def color(rgb):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)


def subtitled(title, segments):
    """A menu item title with a colored second line, [(text, rgb or None for the secondary color)]:
    setSubtitle_ only takes plain text."""
    out = NSMutableAttributedString.alloc().init()
    for text, font, c in [(title + "\n", NSFont.menuFontOfSize_(0), NSColor.labelColor())] + [
        (text, NSFont.menuFontOfSize_(11), color(rgb) if rgb else NSColor.secondaryLabelColor())
        for text, rgb in segments
    ]:
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: font, NSForegroundColorAttributeName: c}
        ))
    return out


# --- PR row ------------------------------------------------------------------

ROW_WIDTH = 600
ROW_HEIGHT = 40
ROW_INSET = 5  # the hover highlight, like the native menu selection
TEXT_X = 34


class PRRowView(NSView):
    """A two-line menu row with buttons on the right. Clicking anywhere else opens the PR.

    Menu items with a view get no highlight or click handling from the menu, so this does both.
    """

    @objc.python_method
    def setup(self, on_open, buttons):
        """buttons: [(title, callback)], left to right."""
        self.on_open = on_open
        self.hover = False
        self.segments = []
        self.button_colors = {}
        self.setAutoresizingMask_(NSViewWidthSizable)

        self.led = NSImageView.imageViewWithImage_(led_image(GRAY))
        self.led.setFrame_(NSMakeRect(TEXT_X - 18, (ROW_HEIGHT - LED_SIZE) / 2, LED_SIZE, LED_SIZE))
        self.addSubview_(self.led)

        self.callbacks = [callback for _, callback in buttons]
        self.buttons = []
        for i, (title, _) in enumerate(buttons):  # added left to right, so VoiceOver reads them in order
            button = NSButton.buttonWithTitle_target_action_(title, self, "buttonClicked:")
            button.setBezelStyle_(NSBezelStyleRounded)
            button.setControlSize_(NSControlSizeSmall)
            button.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
            button.setTag_(i)
            button.setRefusesFirstResponder_(True)  # no focus ring on the first button when the menu opens
            button.sizeToFit()
            button.setAutoresizingMask_(NSViewMinXMargin)
            self.addSubview_(button)
            self.buttons.append(button)

        self.title_label = self._label(NSMakeRect(TEXT_X, 3, 0, 18))
        self.detail_label = self._label(NSMakeRect(TEXT_X, 21, 0, 15))
        self.title_label.setFont_(NSFont.menuFontOfSize_(0))
        self.detail_label.setFont_(NSFont.menuFontOfSize_(NSFont.smallSystemFontSize()))
        self._layout()

        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        ))

    @objc.python_method
    def _layout(self):
        """The buttons right-aligned, the labels in the rest."""
        x = self.frame().size.width - 14 - sum(b.frame().size.width + 4 for b in self.buttons) + 4
        right = x
        for button in self.buttons:
            size = button.frame().size
            button.setFrameOrigin_(NSMakePoint(x, (ROW_HEIGHT - size.height) / 2))
            x += size.width + 4
        for label in (self.title_label, self.detail_label):
            label.setFrameSize_(NSMakeSize(right - 6 - TEXT_X, label.frame().size.height))

    @objc.python_method
    def set_button(self, index, title, rgb=None):
        button = self.buttons[index]
        if button.title() == title and self.button_colors.get(index) == rgb:
            return
        self.button_colors[index] = rgb
        if rgb:
            # a coloured, semibold title: menus ignore bezelColor
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSTextAlignmentCenter)
            button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(NSFont.smallSystemFontSize(), NSFontWeightSemibold),
                NSForegroundColorAttributeName: color(rgb),
                NSParagraphStyleAttributeName: style,
            }))
        else:
            button.setTitle_(title)
        button.sizeToFit()
        self._layout()

    @objc.python_method
    def _label(self, frame):
        label = NSTextField.labelWithString_("")
        label.setFrame_(frame)
        label.setLineBreakMode_(NSLineBreakByTruncatingTail)
        label.setAutoresizingMask_(NSViewWidthSizable)
        self.addSubview_(label)
        return label

    @objc.python_method
    def fill(self, title, segments, led):
        """segments: the second line, as [(text, rgb or None for the secondary color)]."""
        self.title_label.setStringValue_(title)
        self.segments = segments
        self.led.setImage_(led_image(led))
        self._color_text()

    @objc.python_method
    def _color_text(self):
        selected = NSColor.selectedMenuItemTextColor()
        self.title_label.setTextColor_(selected if self.hover else NSColor.labelColor())
        font = self.detail_label.font()
        # too long: cut the repo at the start, so the review's status at the end stays readable
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(NSLineBreakByTruncatingHead)
        detail = NSMutableAttributedString.alloc().init()
        for text, rgb in self.segments:
            c = selected if self.hover else color(rgb) if rgb else NSColor.secondaryLabelColor()
            detail.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: font, NSForegroundColorAttributeName: c, NSParagraphStyleAttributeName: style}
            ))
        self.detail_label.setAttributedStringValue_(detail)

    @objc.python_method
    def _set_hover(self, hover):
        if hover != self.hover:
            self.hover = hover
            self._color_text()
            self.setNeedsDisplay_(True)

    @objc.python_method
    def _close_menu_then(self, callback):
        item = self.enclosingMenuItem()
        menu = item and item.menu()
        while menu is not None and menu.supermenu() is not None:
            menu = menu.supermenu()
        if menu is not None:
            menu.cancelTracking()
        self._set_hover(False)
        AppHelper.callAfter(callback)

    def isFlipped(self):
        return True

    def viewDidMoveToWindow(self):
        self._set_hover(False)  # the menu closed while hovered: no mouseExited

    def drawRect_(self, rect):
        if self.hover:
            NSColor.selectedContentBackgroundColor().set()
            bounds = self.bounds()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(ROW_INSET, 0, bounds.size.width - 2 * ROW_INSET, bounds.size.height), 5, 5
            ).fill()

    def hitTest_(self, point):
        # the labels would swallow clicks; the buttons keep theirs
        hit = objc.super(PRRowView, self).hitTest_(point)
        return hit if hit is None or isinstance(hit, NSButton) else self

    def mouseEntered_(self, event):
        self._set_hover(True)

    def mouseExited_(self, event):
        self._set_hover(False)

    def mouseDown_(self, event):
        pass  # claim the click, so mouseUp_ comes here

    def mouseUp_(self, event):
        self._close_menu_then(self.on_open)

    def buttonClicked_(self, sender):
        """Run the button's callback with the menu still open. If it returns a function (one that shows a dialog
        or a window), close the menu and run that."""
        callback = self.callbacks[sender.tag()]

        def run():  # deferred: the callback may rebuild the menu, and with it this view
            then = callback()
            if then:
                self._close_menu_then(then)

        AppHelper.callAfter(run)  # performSelectorOnMainThread: runs while the menu is tracking too


def pr_row_item(key, on_open, buttons):
    # placeholder title: rumps keys items by title and skips duplicates
    item = rumps.MenuItem(key)
    row = PRRowView.alloc().initWithFrame_(NSMakeRect(0, 0, ROW_WIDTH, ROW_HEIGHT))
    row.setup(on_open, buttons)
    item._menuitem.setView_(row)
    return item


# --- Review window -------------------------------------------------------------

class ButtonTarget(NSObject):
    def fire_(self, sender):
        self.callback()


class RichText:
    """An attributed string, built piece by piece, with just enough GitHub Markdown to read a comment."""

    def __init__(self):
        self.text = NSMutableAttributedString.alloc().init()
        self.body = NSFont.systemFontOfSize_(13)
        self.bold = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        self.small = NSFont.systemFontOfSize_(11)
        self.code = NSFont.monospacedSystemFontOfSize_weight_(12, 0)

    def add(self, s, font=None, color=None, style=None):
        attrs = {NSFontAttributeName: font or self.body, NSForegroundColorAttributeName: color or NSColor.labelColor()}
        if style:
            attrs[NSParagraphStyleAttributeName] = style
        self.text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(s, attrs))
        return self

    def markdown(self, s):
        """**bold**, `code` and fenced code blocks."""
        for i, part in enumerate(re.split(r"```[^\n]*\n?", s)):
            if i % 2:
                self.add(part, self.code)
                continue
            for token in re.split(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)", part):
                if token.startswith("**") and token.endswith("**") and len(token) > 4:
                    self.add(token[2:-2], self.bold)
                elif token.startswith("`") and token.endswith("`") and len(token) > 2:
                    self.add(token[1:-1], self.code)
                elif token:
                    self.add(token)
        return self


def text_block(text, x, y, width):
    """A read-only, selectable text view sized to its text."""
    view = NSTextView.alloc().initWithFrame_(NSMakeRect(x, y, width, 10))
    view.setEditable_(False)
    view.setDrawsBackground_(False)
    view.setTextContainerInset_(NSMakeSize(0, 0))
    view.textContainer().setLineFragmentPadding_(0)
    view.textStorage().setAttributedString_(text)
    layout = view.layoutManager()
    layout.ensureLayoutForTextContainer_(view.textContainer())
    view.setFrameSize_(NSMakeSize(width, layout.usedRectForTextContainer_(view.textContainer()).size.height + 2))
    return view


class ReviewWindow:
    """The comments a Copilot review is about to push, each with a checkbox, and the AI credits it used.

    actions: {"push": fn(comments, summary or None) or None (no Push button), "approve": fn, "discard": fn,
              "open": fn, "session": fn or None}
    """

    WIDTH, HEIGHT = 760, 600
    SOURCES = {"acceptance": "Acceptance criteria", "docs": "Docs alignment", "code": "Technical correctness",
               "conventions": "Repo conventions"}

    def __init__(self, key, pr, result, fresh, skipped, inline, aic, actions, stale=False, summary_new=True):
        self.targets = []  # the buttons' targets: NSButton doesn't retain them
        self.actions = actions
        self.comments, self.summary = fresh, result["summary"]
        # what Push posts: every comment, and the summary as the review's text; untick to leave one out
        self.picked = [True] * len(fresh)
        self.summary_new = summary_new
        self.summary_picked = bool(self.summary) and summary_new and bool(actions.get("push"))
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(f"Review of {key}")
        self.window.setContentMinSize_(NSMakeSize(self.WIDTH, 360))
        self.window.setContentMaxSize_(NSMakeSize(self.WIDTH, 4000))  # fixed width: the rows are laid out for it
        view = self.window.contentView()
        w, h = self.WIDTH, self.HEIGHT

        # the PR's title, and a link to it on GitHub at the end of the line
        link = self._button("Open on GitHub ↗", "open")
        link.setBordered_(False)
        link.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_("Open on GitHub ↗", {
            NSFontAttributeName: NSFont.systemFontOfSize_(13),
            NSForegroundColorAttributeName: NSColor.linkColor(),
        }))
        link.setRefusesFirstResponder_(True)  # no focus ring on it when the window opens
        link.sizeToFit()
        link_w = link.frame().size.width
        link.setFrameOrigin_(NSMakePoint(w - 20 - link_w, h - 38))
        link.setAutoresizingMask_(NSViewMinYMargin)
        view.addSubview_(link)
        heading = NSTextField.labelWithString_(f'#{pr["number"]} {pr["title"]}')
        heading.setFont_(NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold))
        heading.setLineBreakMode_(NSLineBreakByTruncatingTail)
        heading.setFrame_(NSMakeRect(20, h - 38, w - 52 - link_w, 20))
        heading.setAutoresizingMask_(NSViewMinYMargin)
        view.addSubview_(heading)
        count = f'{len(fresh)} comment{"s" if len(fresh) != 1 else ""} to push' if fresh else "Nothing to push"
        sub = NSTextField.labelWithString_(f'by {pr["author"]} · {count} · {aic["total"]:.1f} AI credits in total'
                                           + (" · ⚠︎ new commits since this review" if stale else ""))
        sub.setTextColor_(NSColor.secondaryLabelColor())
        sub.setFrame_(NSMakeRect(20, h - 60, w - 40, 18))
        sub.setAutoresizingMask_(NSViewMinYMargin)
        view.addSubview_(sub)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 56, w - 40, h - 128))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewHeightSizable)
        scroll.setDocumentView_(self._rows(scroll.contentSize().width, skipped, inline, aic))
        view.addSubview_(scroll)

        self.right = []  # right to left: Push, Approve, Close; laid out again when Push's title changes
        for title, action in (("Push", "push"), ("Approve", "approve"), ("Close", "close")):
            if action == "push" and not actions.get("push"):
                continue
            button = self._button(title, action)
            button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            view.addSubview_(button)
            self.right.append(button)
            if action == "push":
                button.setKeyEquivalent_("\r")  # Approve never is: no approving with Return
                self.push_button = button
            if action == "approve":
                self.approve_button = button
        if actions.get("push"):
            self._update_push()  # its title says what it posts
        else:
            self._layout_right()
        x = 20
        for title, action in (("Discard review", "discard"), ("Copilot session", "session")):
            if action == "session" and not actions.get("session"):
                continue
            button = self._button(title, action)
            button.setFrameOrigin_(NSMakePoint(x, 14))
            button.setAutoresizingMask_(NSViewMaxXMargin | NSViewMaxYMargin)
            view.addSubview_(button)
            x += button.frame().size.width + 8

    def _rows(self, width, skipped, inline, aic):
        """The summary, one row per comment with its checkbox, and the AI credits."""
        doc = stats_view.FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
        pad, indent = 12, 34
        y = pad
        secondary = NSColor.secondaryLabelColor()

        def block(text, x):
            nonlocal y
            view = text_block(text.text, x, y, width - x - pad)
            doc.addSubview_(view)
            y += view.frame().size.height

        def checkbox(index):
            target = ButtonTarget.alloc().init()
            target.callback = partial(self._toggle, index)
            self.targets.append(target)
            box = NSButton.checkboxWithTitle_target_action_("", target, "fire:")
            box.setState_(1)
            box.setFrameOrigin_(NSMakePoint(pad, y - 1))
            doc.addSubview_(box)
            if index is None:
                self.summary_box = box
            else:
                self.boxes[index] = box

        self.boxes = {}
        if self.summary:
            if self.summary_picked:
                checkbox(None)
                block(RichText().add("Summary", RichText().bold)
                      .add("  ·  posted as the review's text\n", RichText().small, secondary)
                      .markdown(self.summary), indent)
            else:
                note = "" if self.summary_new else "  ·  already on the PR"
                block(RichText().add("Summary", RichText().bold).add(note + "\n", RichText().small, secondary)
                      .markdown(self.summary), pad)
            y += 18
        block(RichText().add(f"Comments to push ({len(self.comments)})\n", RichText().bold)
              .add(f"{skipped} more {'are' if skipped != 1 else 'is'} already on the PR and left out.\n"
                   if skipped else "", RichText().small, secondary)
              .add("" if self.comments else "None: there is nothing to push.\n", None, secondary), pad)
        for i, c in enumerate(self.comments):
            y += 12
            checkbox(i)
            text = RichText()
            text.add(f'[{c["severity"]}]', text.bold, color(SEVERITY_COLORS[c["severity"]]))
            text.add(f' {c["title"]}\n' if c["title"] else "\n", text.bold)
            where = [c["path"] + (f':{c["line"]}' if c["line"] else "") if c["path"] else "General comment"]
            if c.get("source"):
                where.append(self.SOURCES.get(c["source"], c["source"]))
            if c["path"] and c not in inline:
                where.append("goes in the review's text: the line isn't in the diff")
            text.add("  ·  ".join(where) + "\n", text.small, secondary)
            block(text.markdown(c["body"]), indent)

        y += 18
        table = NSMutableParagraphStyle.alloc().init()  # the AI credits, in a column
        table.setTabStops_([NSTextTab.alloc().initWithTextAlignment_location_options_(NSTextAlignmentRight, 220, {})])
        credits = RichText().add("AI credits\n", RichText().bold)
        for label, value in aic["agents"]:
            credits.add(f"{label}\t{value:.1f}\n", style=table)
        if not aic["agents"]:
            credits.add("No per-agent breakdown in the session log.\n", credits.small, secondary)
        credits.add(f'Total\t{aic["total"]:.1f}', credits.bold, style=table)
        block(credits, pad)
        doc.setFrameSize_(NSMakeSize(width, y + pad))
        return doc

    def _toggle(self, index):
        if index is None:
            self.summary_picked = bool(self.summary_box.state())
        else:
            self.picked[index] = bool(self.boxes[index].state())
        self._update_push()

    def _picked(self):
        return [c for c, picked in zip(self.comments, self.picked) if picked]

    def _update_push(self):
        n = len(self._picked())
        if n and self.summary_picked:
            title = f"Push {n} + summary"
        elif n:
            title = f'Push {n} comment{"s" if n != 1 else ""}'
        else:
            title = "Push the summary" if self.summary_picked else "Push"
        self.push_button.setTitle_(title)
        self.push_button.setEnabled_(n > 0 or self.summary_picked)
        self._layout_right()

    def _layout_right(self):
        x = self.window.contentView().frame().size.width - 20
        for button in self.right:
            button.sizeToFit()
            x -= button.frame().size.width
            button.setFrameOrigin_(NSMakePoint(x, 14))
            x -= 8

    def _button(self, title, action):
        target = ButtonTarget.alloc().init()
        target.callback = partial(self._fire, action)
        self.targets.append(target)
        button = NSButton.buttonWithTitle_target_action_(title, target, "fire:")
        button.setBezelStyle_(NSBezelStyleRounded)
        return button

    def _fire(self, action):
        if action == "close":
            self.close()
        elif action == "push":
            self.actions["push"](self._picked(), self.summary if self.summary_picked else None)
        else:
            self.actions[action]()

    def show(self):
        self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def set_pushing(self, pushing):
        if pushing:
            self.push_title = self.push_button.title()
        self.push_button.setEnabled_(not pushing)
        self.push_button.setTitle_("Pushing…" if pushing else self.push_title)

    def set_approving(self, state):
        """state: "approving", "approved", or None to undo "approving" after a failure."""
        self.approve_button.setEnabled_(state is None)
        self.approve_button.setTitle_({"approving": "Approving…", "approved": "Approved"}.get(state, "Approve"))

    def close(self):
        self.window.close()


# --- Prompt window -------------------------------------------------------------

class PromptWindow:
    """The review prompt and the subagents' instructions, read-only, from the files Copilot gets."""

    WIDTH, HEIGHT = 820, 680

    def __init__(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_("Review prompt")
        self.window.setMinSize_(NSMakeSize(520, 360))
        view = self.window.contentView()
        w, h = self.WIDTH, self.HEIGHT
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 56, w - 40, h - 76))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        size = scroll.contentSize()
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, size.width, size.height))
        self.text.setEditable_(False)  # selectable, to copy from
        self.text.setAutoresizingMask_(NSViewWidthSizable)
        self.text.setTextContainerInset_(NSMakeSize(10, 10))
        scroll.setDocumentView_(self.text)
        view.addSubview_(scroll)
        self.target = ButtonTarget.alloc().init()
        self.target.callback = self.window.close
        close = NSButton.buttonWithTitle_target_action_("Close", self.target, "fire:")
        close.setBezelStyle_(NSBezelStyleRounded)
        close.setKeyEquivalent_("\r")
        close.setFrameOrigin_(NSMakePoint(w - 20 - close.frame().size.width, 14))
        close.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        view.addSubview_(close)

    def update(self):
        bold = NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold)
        small = NSFont.systemFontOfSize_(11)
        mono = NSFont.monospacedSystemFontOfSize_weight_(12, 0)
        text = NSMutableAttributedString.alloc().init()

        def add(s, font, color=None):
            text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(s, {
                NSFontAttributeName: font, NSForegroundColorAttributeName: color or NSColor.labelColor(),
            }))

        agents = os.path.join(copilot_review.COPILOT_DIR, ".github", "agents")
        sections = [("Review prompt", copilot_review.PROMPT_PATH)] + [
            (copilot_review.AGENT_LABELS.get(name.removesuffix(".agent.md"), name) + " subagent",
             os.path.join(agents, name))
            for name in sorted(os.listdir(agents)) if name.endswith(".agent.md")
        ]
        for i, (title, path) in enumerate(sections):
            note = os.path.relpath(path, copilot_review.TOOL_DIR)
            if path == copilot_review.PROMPT_PATH:
                note += "  ·  {{key}} and {{base}} are filled in for each PR"
            add(("\n\n" if i else "") + title + "\n", bold)
            add(note + "\n\n", small, NSColor.secondaryLabelColor())
            with open(path) as f:
                add(f.read().rstrip() + "\n", mono)
        self.text.textStorage().setAttributedString_(text)
        self.text.scrollPoint_(NSMakePoint(0, 0))

    def show(self):
        if not self.window.isVisible():
            self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)


# --- GitHub ------------------------------------------------------------------

def find_gh():
    return shutil.which("gh") or next(
        (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None
    )


def gh_token():
    gh = find_gh()
    if not gh:
        raise RuntimeError("gh CLI not found")
    out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError("gh auth token failed; run `gh auth login`")
    return out.stdout.strip()


def graphql(query, variables, token):
    """The response's data; raises if there is none. Partial data (e.g. a repo not found) is returned as is."""
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    data = body.get("data")
    if data is None:
        raise RuntimeError("; ".join(e.get("message", "?") for e in body.get("errors", [])) or "empty response")
    return data


def fetch_repo(r, me, token):
    """Open, non-draft PRs of a repo; None if the repo isn't found."""
    prs, after = [], None
    while True:
        data = graphql(QUERY, {"owner": r["owner"], "name": r["repo"], "after": after, "me": me}, token)
        if data.get("repository") is None:
            return None
        page = data["repository"]["pullRequests"]
        prs += [summarize(pr, me) for pr in page["nodes"] if not pr["isDraft"]]
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        after = page["pageInfo"]["endCursor"]


def summarize(pr, me):
    reviews = [r for r in pr["reviews"]["nodes"] if r["submittedAt"]]
    state = None
    for r in reviews:
        if r["state"] != "COMMENTED" or state in (None, "COMMENTED", "DISMISSED"):
            state = r["state"]
    pushes = [c["commit"]["committedDate"] for c in pr["commits"]["nodes"]]
    pushes += [e["createdAt"] for e in pr["pushes"]["nodes"] if e]
    ready = [pr["createdAt"]] + [e["createdAt"] for e in pr["ready"]["nodes"] if e]
    mine = [
        t for t in pr["reviewThreads"]["nodes"]
        if any(((c["author"] or {}).get("login") or "").lower() == me.lower() for c in t["started"]["nodes"])
    ]
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": (pr["author"] or {}).get("login", "ghost"),
        "ready": max(parse_time(t) for t in ready),
        "pushed": max((parse_time(t) for t in pushes), default=0),
        # my review state as GitHub shows it, and when I last reviewed
        "review": {
            "state": state,
            "at": parse_time(reviews[-1]["submittedAt"]),
        } if reviews else None,
        "threads": (sum(t["isResolved"] for t in mine), len(mine)),  # (resolved, total) of my threads
        "replied": max((parse_time(c["createdAt"]) for t in mine for c in t["latest"]["nodes"]), default=0),
        "requested": any(
            ((n["requestedReviewer"] or {}).get("login") or "").lower() == me.lower()
            for n in pr["reviewRequests"]["nodes"]
        ),
    }


def parse_time(s):
    return datetime.fromisoformat(s).timestamp()


def http_error_text(e):
    """e.g. "HTTP 422 Unprocessable Entity: <GitHub's reasons>"."""
    try:
        body = json.load(e)
    except (ValueError, TypeError, OSError):
        body = {}
    message = body.get("message") or e.reason if isinstance(body, dict) else e.reason
    errors = body.get("errors") if isinstance(body, dict) else None
    reasons = [err if isinstance(err, str) else err.get("message") or json.dumps(err) for err in errors or []]
    return f"HTTP {e.code} {message}" + (f": {'; '.join(reasons)}" if reasons else "")


def repo_key(r):
    return f'{r["owner"]}/{r["repo"]}'


def parse_repo(text):
    """A repo URL (any page in it) or owner/repo; None if it isn't one."""
    match = REPO_RE.match(text.strip())
    return match and {"owner": match.group(1), "repo": match.group(2)}


def parse_logins(text):
    """GitHub logins separated by commas or whitespace, with or without @."""
    return list(dict.fromkeys(login.lstrip("@") for login in re.split(r"[\s,]+", text) if login.lstrip("@")))


def parse_minutes(text, minimum):
    """A number of minutes, at least minimum, as an int when it's whole; raises ValueError."""
    minutes = float(text.strip())
    if not (math.isfinite(minutes) and minutes >= minimum):
        raise ValueError(text)
    return int(minutes) if minutes.is_integer() else minutes


def parse_budget(text):
    """AI credits a month: None (no limit) for an empty text, else a positive number; raises ValueError."""
    if not text.strip():
        return None
    credits = float(text.strip())
    if not (math.isfinite(credits) and credits > 0):
        raise ValueError(text)
    return int(credits) if credits.is_integer() else credits


def same_login(login):
    # GraphQL drops the [bot] suffix that the REST API and the web UI show
    return login.lower().removesuffix("[bot]")


def author_ok(author, config):
    """The allow list applies only when it isn't empty; the block list always."""
    allow = {same_login(a) for a in config["allow"]}
    block = {same_login(a) for a in config["block"]}
    return (not allow or same_login(author) in allow) and same_login(author) not in block


def last_activity(pr):
    """When the PR was marked ready for review, last pushed to, or last replied to in my threads."""
    return max(pr["ready"], pr["pushed"], pr["replied"])


def back_to_review(pr):
    """Why a PR I reviewed is back in To review, or None: my review was requested again or dismissed (e.g. a repo
    that dismisses approvals on a new push), or all the threads I started are resolved and something happened since
    my last review (new commits, or replies in them)."""
    review = pr["review"]
    if not review:
        return None
    if pr["requested"]:
        return "review re-requested"  # GitHub drops you from the requests when you submit a review
    if review["state"] == "DISMISSED":
        return "review dismissed"
    resolved, total = pr["threads"]
    if review["state"] != "APPROVED" and total and resolved == total and max(pr["pushed"], pr["replied"]) > review["at"]:
        return "comments resolved"
    return None


def new_commits(pr, job):
    """Whether the PR got commits after its Copilot review started: the review is of an older head."""
    return pr["pushed"] > job["started"]


def duration(seconds):
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if days < 7 and hours else f"{days}d"


def short(title):
    return title if len(title) <= 60 else title[:59] + "…"


def notify(title, message):
    subprocess.Popen([
        "osascript",
        "-e", "on run argv",
        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
        "-e", "end run",
        title, message,
    ])


# --- Config and state ----------------------------------------------------------

def state_path():
    # next to the config, so pointing CONFIG_PATH at a scratch file moves both
    return os.path.join(os.path.dirname(CONFIG_PATH), "state.json")


def review_dir(key):
    """Where a Copilot review keeps the PR's context, its checkout and its result."""
    return os.path.join(os.path.dirname(CONFIG_PATH), "reviews", re.sub(r"[/#]", "__", key))


def history_path():
    return os.path.join(os.path.dirname(CONFIG_PATH), "history.jsonl")


def clones_dir():
    return os.path.join(os.path.dirname(CONFIG_PATH), "clones")


def load_config():
    """Raises ValueError if the file isn't a valid config."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.setdefault("repos", []), list):
        raise ValueError('expected an object with a "repos" list')
    for r in config["repos"]:
        if not (isinstance(r, dict) and isinstance(r.get("owner"), str) and isinstance(r.get("repo"), str)):
            raise ValueError(f"invalid repo entry: {json.dumps(r)}")
    for name in ("allow", "block"):
        logins = config.setdefault(name, [])
        if not (isinstance(logins, list) and all(isinstance(a, str) for a in logins)):
            raise ValueError(f'"{name}" must be a list of GitHub logins')
    budget = config.get("monthly_budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
        del config["monthly_budget"]  # AI credits per calendar month; no limit without it
    minutes = config.get("poll_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 1:
        config["poll_minutes"] = DEFAULT_POLL_MINUTES
    # "since": when auto-review was switched on; PRs whose wait was over by then aren't reviewed automatically
    automation = config.get("automation") if isinstance(config.get("automation"), dict) else {}
    automation = {**AUTOMATION_DEFAULTS, **automation}
    wait = automation["wait_minutes"]
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
        automation["wait_minutes"] = AUTOMATION_DEFAULTS["wait_minutes"]
    for name in ("review", "push", "approve"):
        automation[name] = automation[name] is True
    config["automation"] = automation
    model = config.get("copilot_model")
    if not (isinstance(model, str) and model.strip()):
        config.pop("copilot_model", None)  # Copilot's default
    return config


def load_state():
    """What the app remembers between runs; starts over if the file is missing or broken."""
    try:
        with open(state_path()) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    return {
        "known": state.get("known") if isinstance(state.get("known"), dict) else {},  # repo -> PR numbers seen
        "ignored": state.get("ignored") if isinstance(state.get("ignored"), list) else [],  # "owner/repo#123"
        # key -> Copilot review: {"status": preparing|running|done|failed, "session", "started", "pid", "head",
        #                         "model", "comments", "credits", "error"}
        "reviews": state.get("reviews") if isinstance(state.get("reviews"), dict) else {},
        # key -> the PR's last activity when its last review started: no automatic review without anything new
        "reviewed_activity": state.get("reviewed_activity") if isinstance(state.get("reviewed_activity"), dict) else {},
    }


def write_json(path, data):
    # write-then-rename, so nobody reads a half-written file
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_config(config):
    write_json(CONFIG_PATH, config)


def config_mtime():
    try:
        return os.stat(CONFIG_PATH).st_mtime_ns
    except FileNotFoundError:
        return None


# --- App ---------------------------------------------------------------------

class PRReviewerApp(rumps.App):
    def __init__(self):
        super().__init__("PR Reviewer", title="", quit_button=None)
        # Menu bar only: no Dock icon.
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        try:
            self.config = load_config()
        except ValueError:
            self.config = {"repos": [], "allow": [], "block": [], "automation": dict(AUTOMATION_DEFAULTS),
                           "poll_minutes": DEFAULT_POLL_MINUTES}
        self.config_mtime = config_mtime()
        self.state = load_state()
        for job in self.state["reviews"].values():
            if job.get("status") == "preparing":  # the app quit before Copilot started
                job.update(status="failed", error="Interrupted: the app quit while preparing the review")
        self.procs = {}  # key -> Popen of the reviews started by this run
        self.auto_busy = set()  # keys with an automatic push or approval in flight
        self.automated_at = 0
        self.started_at = time.time()
        self.models = []  # what Copilot CLI offers, for Settings; fetched when Settings opens
        self.windows = {}  # key -> ReviewWindow
        self.info = {}  # repo key -> {"prs": [summarize() dicts]} or {"error": text}
        self.back = set()  # keys of reviewed PRs that are back in To review
        self.token = self.login = None
        self.busy = False
        self.error = None
        self.updated_at = None
        self.next_poll_at = 0
        self.rendered_minute = None
        self.menu_structure = None
        self.rows = {}  # "owner/repo#123" -> PRRowView
        self.approved_items = {}  # "owner/repo#123" -> MenuItem in the Approved submenu
        self.repo_items = {}  # repo key -> MenuItem
        self.stats_item = self.settings_item = self.status_item = None
        self.month_spent = 0.0
        self.stats_window = self.prompt_window = self.settings_window = None
        self.timer = rumps.Timer(self.tick, 1)
        rumps.events.before_start.register(self._on_start)

    @property
    def repos(self):
        return self.config["repos"]

    def _on_start(self):
        self._render()
        self.timer.start()
        # keep ticking while the menu is open, so the countdown stays live
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer._nstimer, NSRunLoopCommonModes)

    # config and state

    def _save_config(self):
        save_config(self.config)
        self.config_mtime = config_mtime()

    def _save_state(self):
        write_json(state_path(), self.state)

    def _check_config(self):
        """Pick up edits made outside the app, e.g. by `pr_reviewer.py add`."""
        mtime = config_mtime()
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            config = load_config()
        except ValueError:
            return  # broken hand edit: keep what we have
        before = {repo_key(r) for r in self.repos}
        self.config = config
        if {repo_key(r) for r in self.repos} - before:
            self.next_poll_at = 0  # fetch new repos on the next tick
        self._forget_untracked()
        self._render()

    def _forget_untracked(self):
        keys = {repo_key(r) for r in self.repos}
        self.info = {k: v for k, v in self.info.items() if k in keys}
        self.back = {k for k in self.back if k.split("#")[0] in keys}
        self.state["known"] = {k: v for k, v in self.state["known"].items() if k in keys}
        self.state["ignored"] = [k for k in self.state["ignored"] if k.split("#")[0] in keys]
        self.state["reviewed_activity"] = {
            k: v for k, v in self.state["reviewed_activity"].items() if k.split("#")[0] in keys
        }
        for key in [k for k in self.state["reviews"] if k.split("#")[0] not in keys]:
            self._drop_review(key)
        self._save_state()

    # polling

    def tick(self, _):
        self._check_config()
        self._check_reviews()
        now = time.time()
        if now - self.automated_at >= 20:
            self.automated_at = now
            self._automate()
        if now >= self.next_poll_at:
            self.poll()
        if int(now // 60) != self.rendered_minute:
            self._render()  # the times shown in the menu grow
        else:
            self._render_status()

    def poll(self, _=None):
        if self.busy or not self.repos:
            return
        self.busy = True
        self.next_poll_at = time.time() + self.config["poll_minutes"] * 60
        snapshot = list(self.repos)
        threading.Thread(target=self._poll_worker, args=(snapshot,), daemon=True).start()

    def _poll_worker(self, snapshot):
        results, error = {}, None
        try:
            if not self.token:
                self.token = gh_token()
                self.login = graphql("{ viewer { login } }", {}, self.token)["viewer"]["login"]
            for r in snapshot:
                prs = fetch_repo(r, self.login, self.token)
                results[repo_key(r)] = {"error": "Not found or no access"} if prs is None else {"prs": prs}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.token = None  # refetch from gh next time
            error = http_error_text(e)
        except Exception as e:
            error = str(e)
        AppHelper.callAfter(self._apply, results, error)

    def _apply(self, results, error):
        self.busy = False
        self.error = error
        if error:
            self.next_poll_at = time.time() + RETRY_SECONDS
        else:
            self.updated_at = time.strftime("%H:%M")
        current = {repo_key(r) for r in self.repos}
        results = {k: v for k, v in results.items() if k in current}
        # no notifications for what was already there when a repo was added (or the app started)
        first_seen = {k for k, v in results.items() if "prs" in v and k not in self.state["known"]}
        first_polled = {k for k in results if "prs" not in self.info.get(k, {})}
        self.info.update(results)

        new = []
        for rk, result in results.items():
            if "prs" not in result:
                continue
            numbers = sorted(pr["number"] for pr in result["prs"])
            if rk not in first_seen:
                known = set(self.state["known"][rk])
                new += [f"{rk}#{n}" for n in numbers if n not in known]
            self.state["known"][rk] = numbers
            # forget merged and closed PRs (and ones turned back into drafts)
            still_open = {f"{rk}#{n}" for n in numbers}
            self.state["ignored"] = [k for k in self.state["ignored"] if k.split("#")[0] != rk or k in still_open]
            self.state["reviewed_activity"] = {
                k: v for k, v in self.state["reviewed_activity"].items() if k.split("#")[0] != rk or k in still_open
            }
            for key in [k for k in self.state["reviews"] if k.split("#")[0] == rk and k not in still_open]:
                self._drop_review(key)
        self._save_state()

        to_review = self._groups()[0]
        tracked = self._tracked()
        fresh = [k for k in new if k in to_review]
        if len(fresh) == 1:
            pr = tracked[fresh[0]]
            notify(f"New PR to review: {fresh[0]}", f'{pr["title"]} · {pr["author"]}')
        elif fresh:
            notify(f"{len(fresh)} new PRs to review", ", ".join(fresh))
        self._notify_back(quiet=first_polled)
        self._automate()
        self._render()

    # groups

    def _tracked(self):
        """Every open, non-draft PR of the tracked repos, keyed by "owner/repo#123"."""
        return {f'{k}#{pr["number"]}': pr for k, info in self.info.items() for pr in info.get("prs", [])}

    def _groups(self):
        """(to review, reviewed, ignored, approved), each {key: PR} in menu order.

        Reviewed: PRs I submitted a review for, whoever the author is, waiting on the author. The Review button
        alone doesn't count.
        Approved: the ones I approved.
        To review: PRs waiting on me. Those I reviewed come back when back_to_review() says so, e.g. my approval
        was dismissed.
        To review / ignored: the rest, by authors that pass the allow and block lists. My own PRs are in none.
        """
        to_review, reviewed, ignored, approved = [], [], [], []
        for key, pr in self._tracked().items():
            if self.login and same_login(pr["author"]) == same_login(self.login):
                continue
            if back_to_review(pr):
                to_review.append((key, pr))
            elif pr["review"] and pr["review"]["state"] == "APPROVED":
                approved.append((key, pr))
            elif pr["review"]:
                reviewed.append((key, pr))
            elif not author_ok(pr["author"], self.config):
                continue
            elif key in self.state["ignored"]:
                ignored.append((key, pr))
            else:
                to_review.append((key, pr))
        # the ones back from Reviewed first, then the newest ready for review
        to_review.sort(key=lambda item: (not item[1]["review"], -item[1]["ready"]))
        ignored.sort(key=lambda item: -item[1]["ready"])
        reviewed.sort(key=lambda item: -item[1]["review"]["at"])
        approved.sort(key=lambda item: -item[1]["review"]["at"])
        return dict(to_review), dict(reviewed), dict(ignored), dict(approved)

    def _notify_back(self, quiet=()):
        """Notify about PRs I reviewed that are back in To review."""
        to_review = self._groups()[0]
        back = {key: back_to_review(pr) for key, pr in to_review.items() if pr["review"]}
        new = sorted(k for k in back.keys() - self.back if k.split("#")[0] not in quiet)
        self.back = set(back)
        if len(new) == 1:
            notify(f"Back to review: {new[0]} ({back[new[0]]})", to_review[new[0]]["title"])
        elif new:
            notify(f"{len(new)} PRs are back to review", ", ".join(new))

    # rendering

    def _month_spent(self):
        """AI credits spent on reviews this calendar month: the finished ones, and the running ones so far."""
        finished = history.spending(history.load(history_path()))[2][1]
        running = sum(copilot_review.credits_so_far(job["session"]) or 0
                      for job in self.state["reviews"].values() if job["status"] == "running")
        return finished + running

    def _spending_summary(self):
        """The statistics item's second line, as [(text, rgb or None)]: today, this week and this month, and the
        budget if there is one. The share of the budget is green, amber from 80% and red once it's used up."""
        spent = history.spending(history.load(history_path()))
        running = self.month_spent - spent[2][1]  # what running reviews have spent so far: they started recently
        text = (f"Today {spent[0][1] + running:.1f} · this week {spent[1][1] + running:.1f} · "
                f"this month {self.month_spent:.1f}")
        share = self._budget_share()
        if share is None:
            return [(text + " AIC", None)]
        budget = self.config["monthly_budget"]
        if share[1] == RED:
            return [("⚠︎ Over budget", RED), (f" · {text} of {budget:g} AIC", None)]
        return ([("⚠︎ ", AMBER)] if share[1] == AMBER else []) + [(f"{text} of {budget:g} AIC (", None), share, (")", None)]

    def _budget_share(self):
        """(percentage, rgb) of the monthly budget used: green, amber from 80%, red once it's used up. None without
        a budget."""
        budget = self.config.get("monthly_budget")
        if budget is None:
            return None
        fraction = self.month_spent / budget
        return f"{fraction:.0%}", RED if fraction >= 1 else AMBER if fraction >= 0.8 else GREEN

    def _over_budget(self):
        budget = self.config.get("monthly_budget")
        return budget is not None and self._month_spent() >= budget

    def _render(self):
        self.rendered_minute = int(time.time() // 60)
        self.month_spent = self._month_spent()
        to_review, reviewed, ignored, approved = self._groups()
        self._render_title(len(to_review))  # the PRs waiting on me
        self._render_menu(to_review, reviewed, ignored, approved)
        if self.settings_window is not None and self.settings_window.window.isVisible():
            self._update_settings(to_review)

    def _render_title(self, count):
        font = NSFont.menuBarFontOfSize_(0)
        attrs = {NSFontAttributeName: font}
        title = NSMutableAttributedString.alloc().init()

        def text(s):
            title.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(s, attrs))

        icon = glasses_image(font, ORANGE if count else None)
        size = icon.size()
        attachment = NSTextAttachment.alloc().init()
        attachment.setImage_(icon)
        attachment.setBounds_(NSMakeRect(0, (font.capHeight() - size.height) / 2, size.width, size.height))
        title.appendAttributedString_(NSAttributedString.attributedStringWithAttachment_(attachment))
        if count:
            text(f" {count}")
        if self.error:
            text(" ⚠︎")
        self._nsapp.nsstatusitem.button().setAttributedTitle_(title)

    def _render_menu(self, to_review, reviewed, ignored, approved):
        tracked = self._tracked()
        # To approve: the PRs in To review whose Copilot review is done, waiting for me to push or approve
        done = {key for key in to_review if self.state["reviews"].get(key, {}).get("status") == "done"}
        structure = (
            [key for key in to_review if key not in done],
            [key for key in to_review if key in done],
            [key for key in to_review if tracked[key]["review"]],  # back from Reviewed: no Ignore button
            list(reviewed),
            list(ignored),
            list(approved),
            [(repo_key(r), "error" in self.info.get(repo_key(r), {})) for r in self.repos],
        )
        if structure != self.menu_structure:
            self.menu_structure = structure
            self._build_menu()
        now = time.time()
        tracked = self._tracked()
        for key, row in self.rows.items():
            self._fill_row(row, key, tracked[key], key in reviewed, now)
        for key, item in self.approved_items.items():
            item._menuitem.setSubtitle_(f'{tracked[key]["author"]} · approved {duration(now - tracked[key]["review"]["at"])} ago')
        for rk, item in self.repo_items.items():
            self._fill_repo_item(item, rk, to_review)
        self.stats_item._menuitem.setAttributedTitle_(subtitled("Review statistics…", self._spending_summary()))
        self._render_status()

    def _build_menu(self):
        # rumps registers every MenuItem in a class-level dict and clear() doesn't drop them
        self._forget_items(self.menu)
        self.menu.clear()
        to_review, to_approve, back, reviewed, ignored, approved, repos = self.menu_structure
        self.rows = {}
        items = []

        def rows(keys, buttons, back=()):
            def row(key):
                item = pr_row_item(key, partial(self._open_pr, key), [
                    (title, partial(callback, key)) for title, callback in buttons
                    if not (key in back and title == "Ignore")  # it's been reviewed: no ignoring
                ])
                self.rows[key] = item._menuitem.view()
                return item

            items.extend(row(key) for key in keys[:MAX_LISTED])
            if len(keys) > MAX_LISTED:
                more = rumps.MenuItem(f"{len(keys) - MAX_LISTED} more")
                more.update([row(key) for key in keys[MAX_LISTED:]])
                items.append(more)
            items.append(None)

        if repos:
            items.append(rumps.MenuItem(f"To review ({len(to_review)})" if to_review else "Nothing to review"))
            rows(to_review, [("Review", self.review), ("Ignore", self.ignore)], back)
        if to_approve:
            items.append(rumps.MenuItem(f"To approve ({len(to_approve)})"))
            rows(to_approve, [("Review", self.review), ("Ignore", self.ignore)], back)
        if reviewed:
            items.append(rumps.MenuItem(f"Reviewed ({len(reviewed)})"))
            rows(reviewed, [("Review", self.review)])

        self.repo_items = {}
        if ignored:
            tracked = self._tracked()
            item = rumps.MenuItem(f"Ignored ({len(ignored)})")
            item.update([rumps.MenuItem("Click one to put it back in To review")] + [
                rumps.MenuItem(f'{key} {short(tracked[key]["title"])}', callback=partial(self.unignore, key))
                for key in ignored
            ])
            items.append(item)
        self.approved_items = {}
        if approved:
            tracked = self._tracked()
            item = rumps.MenuItem(f"Approved ({len(approved)})")
            for key in approved:
                self.approved_items[key] = rumps.MenuItem(
                    f'{key} {short(tracked[key]["title"])}', callback=partial(self._open, tracked[key]["url"])
                )
            item.update([rumps.MenuItem("Click one to open it")] + list(self.approved_items.values()))
            items.append(item)
        for rk in [rk for rk, failed in repos if failed]:  # the rest are in Settings
            item = self.repo_items[rk] = rumps.MenuItem(
                rk, callback=partial(self._open, f"https://github.com/{rk}/pulls"))
            items.append(item)
        if ignored or approved or self.repo_items:
            items.append(None)

        self.stats_item = rumps.MenuItem("Review statistics…", callback=self.show_stats)
        if repos:
            items.append(rumps.MenuItem("Refresh now", callback=self.refresh))
        items.append(self.stats_item)
        self.settings_item = rumps.MenuItem("Settings…", callback=self.show_settings)
        if not repos:
            self.settings_item._menuitem.setSubtitle_("Track a repo to start")
        items.append(self.settings_item)
        items.append(None)

        self.status_item = rumps.MenuItem("")
        items.append(self.status_item)
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))
        self.menu.update(items)
        self.menu._menu.update()  # grey out the headers again when this runs with the menu open

    @classmethod
    def _forget_items(cls, menu):
        for item in menu.values():
            if isinstance(item, rumps.MenuItem):
                rumps.rumps.NSApp._ns_to_py_and_callback.pop(item._menuitem, None)
                cls._forget_items(item)

    def _fill_row(self, row, key, pr, reviewed, now):
        title = f'#{pr["number"]} {short(pr["title"])}'
        detail = [(f'{key.split("#")[0]} · {pr["author"]} · ', None)]
        job = self.state["reviews"].get(key)
        row.set_button(0, *self._review_button(job, self._budget_used_up()))
        if not reviewed and not pr["review"]:
            if job:  # room for the review's status
                detail[-1] = (detail[-1][0].removesuffix(" · "), None)
            else:
                detail.append((f'ready {duration(now - pr["ready"])} ago', None))
                detail += self._auto_detail(key, pr, now)
            detail += self._review_detail(job, pr, now)
            row.fill(title, detail, ORANGE)
            return
        label, led = REVIEW_STATES[pr["review"]["state"]]
        detail.append((f'{label} {duration(now - pr["review"]["at"])} ago', None))
        resolved, total = pr["threads"]
        if total:
            detail.append((" · ", None))
            detail.append((f"{resolved}/{total} resolved", GREEN if resolved == total else None))
        detail += self._review_detail(job, pr, now)
        if not reviewed:  # back in To review
            detail.append((f" · {back_to_review(pr)}", GREEN))
            if not job:
                detail += self._auto_detail(key, pr, now)
            row.fill(title, detail, ORANGE)
            return
        row.fill(title, detail, led)

    def _budget_used_up(self):
        budget = self.config.get("monthly_budget")
        return budget is not None and self.month_spent >= budget

    @staticmethod
    def _review_button(job, over_budget=False):
        """(title, bezel color) of a row's Review button."""
        if not job and over_budget:
            return "Over budget", RED
        status = (job or {}).get("status")
        if status in ("preparing", "running"):
            return "Reviewing…", ORANGE
        if status == "done":
            return ("Ready to comment", GREEN) if job["comments"] else ("No comments", BLUE)
        if status == "failed":
            return "Review failed", RED
        return "Review", None

    def _auto_detail(self, key, pr, now):
        """When a PR without a review gets an automatic one."""
        due = self._auto_review_in(key, pr, now)
        if due is None or self._budget_used_up():
            return []
        return [(" · ", None), (f"auto-review in {duration(due)}" if due > 0 else "auto-review due", BLUE)]

    @staticmethod
    def _review_detail(job, pr, now):
        """The review's part of a row's second line."""
        if not job:
            return []
        status = job["status"]
        # the review is of an older head: pushing puts comments on that commit, approving needs the latest
        stale = [(" · new commits", AMBER)] if status in ("running", "done") and new_commits(pr, job) else []
        if status == "preparing":
            return [(" · preparing", ORANGE)]
        if status == "running":
            credits = copilot_review.credits_so_far(job["session"])
            spent = f" · {credits:.1f} AIC" if credits is not None else ""
            auto = " (automatic)" if job.get("auto") else ""
            return [(f' · reviewing{auto} {duration(now - job["started"])}{spent}', ORANGE)] + stale
        if status == "done":
            n, must = job["comments"], job.get("must_fix", 0)
            detail = [(f' · {n} comment{"s" if n != 1 else ""}', GREEN if n else BLUE)]
            if must:
                detail.append((f" ({must} must-fix)", RED))
            return detail + [(f' · {job["credits"]["total"]:.1f} AIC', GREEN if n else BLUE)] + stale
        return [(" · review failed", RED)]

    def _repo_status(self, key, to_review):
        """(status line, LED color) of a tracked repo."""
        info = self.info.get(key)
        waiting = sum(1 for k in to_review if k.split("#")[0] == key)
        if info is None:
            return "Checking…", GRAY
        if "error" in info:
            return info["error"], RED
        return f'{len(info["prs"])} open' + (f" · {waiting} to review" if waiting else ""), ORANGE if waiting else GRAY

    def _fill_repo_item(self, item, key, to_review):
        detail, rgb = self._repo_status(key, to_review)
        item._menuitem.setImage_(led_image(rgb))
        item._menuitem.setSubtitle_(detail)

    def _render_status(self):
        if self.status_item is None:
            return
        if not self.repos:
            text = "No repos tracked"
        elif self.busy or not (self.updated_at or self.error):
            text = "Updating…"
        else:
            left = max(0, round(self.next_poll_at - time.time()))
            left = f"{left}s" if left < 60 else f"{left // 60}m"
            if self.error:
                text = f"Error: {self.error[:80]} · retry in {left}"
            else:
                text = f"Updated {self.updated_at} · next in {left}"
        self.status_item.title = text

    # actions

    @staticmethod
    def _open(url, _):
        webbrowser.open(url)

    def _open_pr(self, key):
        pr = self._tracked().get(key)
        if pr:
            webbrowser.open(pr["url"])

    # Copilot reviews

    def review(self, key):
        """The Review button. Starting a review keeps the menu open; for a review there is already, this returns
        the dialog or window to show once the menu has closed."""
        if key in self.state["reviews"]:
            return partial(self._review_dialog, key)
        if self._over_budget():
            return self._budget_alert
        self._start_review(key)

    def _review_dialog(self, key):
        job = self.state["reviews"].get(key)
        status = (job or {}).get("status")
        if status in ("preparing", "running"):
            if rumps.alert(f"Stop the review of {key}?", "Copilot keeps the AI credits it has used so far.",
                           ok="Stop review", cancel="Keep reviewing") == 1:
                self._drop_review(key)
                self._save_state()
                self._render()
        elif status == "done":
            self._open_review(key)
        elif status == "failed":
            if rumps.alert(f"The review of {key} failed", job.get("error") or "Unknown error",
                           ok="Retry", cancel="Close") == 1:
                if self._over_budget():
                    self._budget_alert()
                    return
                self._drop_review(key)
                self._start_review(key)

    def _budget_alert(self):
        if rumps.alert(
            "This month's review budget is used up",
            f'{self._month_spent():.1f} of {self.config["monthly_budget"]:g} AI credits spent. New reviews can start '
            "again next month, or after raising the budget.",
            ok="Change budget…", cancel="Close",
        ) == 1:
            self.show_settings(focus="budget")

    def _start_review(self, key, auto=False):
        if key not in self._tracked():
            return
        session = str(uuid.uuid4())
        pr = self._tracked()[key]
        self.state["reviewed_activity"][key] = last_activity(pr)
        self.state["reviews"][key] = {
            "status": "preparing", "session": session, "started": time.time(),
            "title": pr["title"], "author": pr["author"], "auto": auto,
            "model": self.config.get("copilot_model") or copilot_review.default_model(),  # for the statistics
        }
        self._save_state()
        self._render()
        threading.Thread(target=self._review_worker, args=(key, session), daemon=True).start()

    def _review_cap(self):
        """--max-ai-credits for a new review: the configured cap, lowered to what's left of the monthly budget.
        Copilot's minimum is 30, so the budget can be overshot by up to that."""
        caps = [self.config.get("copilot_max_ai_credits")]
        budget = self.config.get("monthly_budget")
        if budget is not None:
            caps.append(max(30, int(budget - self._month_spent()) + 1))
        caps = [c for c in caps if c]
        return min(caps) if caps else None

    def _review_worker(self, key, session):
        workdir = review_dir(key)
        try:
            token = self.token or gh_token()
            shutil.rmtree(workdir, ignore_errors=True)
            os.makedirs(workdir)
            meta = copilot_review.prepare(key, workdir, clones_dir(), token)
            proc = copilot_review.start(key, workdir, session, meta["base"],
                                        model=self.config.get("copilot_model"),
                                        max_ai_credits=self._review_cap())
        except urllib.error.HTTPError as e:
            AppHelper.callAfter(self._review_failed, key, session, http_error_text(e))
            return
        except Exception as e:
            AppHelper.callAfter(self._review_failed, key, session, str(e))
            return
        AppHelper.callAfter(self._review_started, key, session, proc, meta)

    def _review_started(self, key, session, proc, meta):
        job = self.state["reviews"].get(key)
        if not job or job["session"] != session:  # stopped while preparing
            copilot_review.stop(proc.pid)
            return
        self.procs[key] = proc
        job.update(status="running", pid=proc.pid, head=meta["head"])
        self._save_state()
        self._render()

    def _review_failed(self, key, session, error):
        job = self.state["reviews"].get(key)
        if job and job["session"] == session:
            job.update(status="failed", error=error)
            self._save_state()
            notify(f"Review of {key} failed", error)
            self._render()

    def _auto_review_in(self, key, pr, now):
        """Seconds until a PR in To review gets an automatic review (0: due), or None if it won't get one."""
        automation = self.config["automation"]
        activity = last_activity(pr)
        due = activity + automation["wait_minutes"] * 60
        if not automation["review"] or due < automation.get("since", 0):
            return None  # the backlog: its wait was over before auto-review was switched on
        if activity <= self.state["reviewed_activity"].get(key, 0):
            return None  # reviewed since its last activity (pushed, approved, discarded or failed)
        return max(0.0, due - now, self.started_at + STARTUP_GRACE - now)

    def _automate(self):
        """Start a due automatic review, and push or approve finished ones, as the Automation settings say."""
        automation = self.config["automation"]
        jobs = self.state["reviews"]
        to_review = self._groups()[0]
        now = time.time()
        busy = any(job.get("auto") and job["status"] in ("preparing", "running") for job in jobs.values())
        if now < self.started_at + STARTUP_GRACE:
            return  # _auto_review_in() counts down to its end
        if automation["review"] and not busy and not self._over_budget():
            for key, pr in to_review.items():  # one automatic review at a time
                if key not in jobs and self._auto_review_in(key, pr, now) == 0:
                    self._start_review(key, auto=True)
                    break
        for key, job in list(jobs.items()):
            pr = self._tracked().get(key)
            if job["status"] != "done" or key in self.auto_busy or job.get("auto_failed") or pr is None:
                continue
            if new_commits(pr, job):
                continue  # new commits since the review started: leave it to me
            if job["comments"] and automation["push"] and job.get("auto"):
                self._auto_act(key, "push")
            elif not job["comments"] and automation["approve"] and key in to_review:
                self._auto_act(key, "approve")

    def _auto_act(self, key, action):
        """Push a finished automatic review's comments, or approve a PR whose review found nothing."""
        self.auto_busy.add(key)
        job = self.state["reviews"][key]

        def worker():
            try:
                token = self.token or gh_token()
                if action == "approve":
                    if copilot_review.head_sha(key, token) != job["head"]:
                        raise RuntimeError("the PR has new commits since the review")
                    copilot_review.approve(key, job["head"], token)
                    AppHelper.callAfter(self._approved, key, 0, True)
                    return
                result = copilot_review.read_result(review_dir(key))
                fresh, skipped, summary = copilot_review.to_push(key, result["comments"], token, result["summary"])
                if fresh or summary:
                    copilot_review.push(key, review_dir(key), job["head"], fresh, token, summary)
                AppHelper.callAfter(self._pushed, key, len(fresh), skipped, True, bool(summary))
            except urllib.error.HTTPError as e:
                AppHelper.callAfter(self._auto_failed, key, action, http_error_text(e))
            except Exception as e:
                AppHelper.callAfter(self._auto_failed, key, action, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _auto_failed(self, key, action, error):
        """Don't try again: the review stays for me to push or approve by hand."""
        self.auto_busy.discard(key)
        job = self.state["reviews"].get(key)
        if job:
            job["auto_failed"] = error
            self._save_state()
        notify(f"Couldn't {action} {key} automatically", error)
        self._render()

    def _check_reviews(self):
        """Pick up the reviews whose Copilot has exited, including ones started before an app restart."""
        for key, job in list(self.state["reviews"].items()):
            if job["status"] != "running":
                continue
            proc = self.procs.get(key)
            if proc.poll() is None if proc else copilot_review.alive(job["pid"]):
                continue
            self.procs.pop(key, None)
            workdir = review_dir(key)
            try:
                job["credits"] = copilot_review.credits(workdir, job["session"])
            except Exception:
                job["credits"] = {"total": 0.0, "agents": [], "tokens": 0, "models": []}
            try:
                comments = copilot_review.read_result(workdir)["comments"]
                job["comments"] = len(comments)
                job["must_fix"] = sum(1 for c in comments if c["severity"] == "MUST-FIX")
                job["status"] = "done"
                n = job["comments"]
                notify(f"Review of {key} is done",
                       f'{n} comment{"s" if n != 1 else ""} · {job["credits"]["total"]:.1f} AI credits')
            except Exception as e:
                job.update(status="failed", error=str(e))
                notify(f"Review of {key} failed", str(e))
            self._log_review(key, job)
            self._save_state()
            threading.Thread(target=copilot_review.remove_checkout, args=(workdir,), daemon=True).start()
            self._render()

    def _drop_review(self, key):
        """Forget a review: stop its Copilot, close its window, delete its files. The caller saves the state."""
        job = self.state["reviews"].pop(key, None)
        self.procs.pop(key, None)
        window = self.windows.pop(key, None)
        if window:
            window.close()
        if job and job.get("status") == "running" and job.get("pid"):
            copilot_review.stop(job["pid"])
            # what it has spent so far: its last usage checkpoint
            spent = copilot_review.credits_so_far(job["session"]) or 0.0
            self._log_review(key, dict(job, status="stopped", credits={"total": spent, "agents": [], "tokens": 0}))

        def cleanup():
            workdir = review_dir(key)
            copilot_review.remove_checkout(workdir)
            shutil.rmtree(workdir, ignore_errors=True)

        threading.Thread(target=cleanup, daemon=True).start()

    def _open_review(self, key):
        """Show the comments a finished review is about to push, without the ones already on the PR."""
        if key in self.windows:
            self.windows[key].show()
            return
        session = self.state["reviews"][key]["session"]

        def worker():
            try:
                result = copilot_review.read_result(review_dir(key))
                fresh, skipped, summary = copilot_review.to_push(key, result["comments"], self.token or gh_token(),
                                                                 result["summary"])
            except Exception as e:
                AppHelper.callAfter(rumps.alert, f"Can't open the review of {key}", str(e))
                return
            AppHelper.callAfter(self._show_review, key, session, result, fresh, skipped, summary)

        threading.Thread(target=worker, daemon=True).start()

    def _show_review(self, key, session, result, fresh, skipped, summary):
        job = self.state["reviews"].get(key)
        pr = self._tracked().get(key)
        if not job or job["session"] != session or pr is None:
            return
        workdir = review_dir(key)
        with open(os.path.join(workdir, "context", "diff.patch")) as f:
            lines = copilot_review.commentable_lines(f.read())
        inline = [c for c in fresh if c["path"] and c["line"] in lines.get(c["path"], ())]
        transcript = next((p for p in (os.path.join(workdir, "session.md"), os.path.join(workdir, "copilot.log"))
                           if os.path.exists(p)), None)
        window = ReviewWindow(key, pr, result, fresh, skipped, inline, job["credits"], {
            "push": partial(self._push_review, key) if fresh else None,
            "approve": partial(self._approve, key, len(fresh)),
            "discard": partial(self._discard_review, key),
            "open": partial(webbrowser.open, pr["url"]),
            "session": partial(subprocess.Popen, ["open", "-e", transcript]) if transcript else None,
        }, stale=new_commits(pr, job), summary_new=summary is not None)
        self.windows[key] = window
        window.show()

    def _push_review(self, key, comments, summary):
        window = self.windows.get(key)
        job = self.state["reviews"].get(key)
        if not window or not job:
            return
        window.set_pushing(True)

        def worker():
            try:
                token = self.token or gh_token()
                # someone may have commented meanwhile
                fresh, _, summary_new = copilot_review.to_push(key, comments, token, summary)
                if fresh or summary_new:
                    copilot_review.push(key, review_dir(key), job["head"], fresh, token, summary_new)
            except urllib.error.HTTPError as e:
                AppHelper.callAfter(self._push_failed, key, http_error_text(e))
                return
            except Exception as e:
                AppHelper.callAfter(self._push_failed, key, str(e))
                return
            AppHelper.callAfter(self._pushed, key, len(fresh), len(comments) - len(fresh), False, bool(summary_new))

        threading.Thread(target=worker, daemon=True).start()

    def _push_failed(self, key, error):
        window = self.windows.get(key)
        if window:
            window.set_pushing(False)
        rumps.alert(f"Couldn't push the comments to {key}", error)

    def _log_review(self, key, job):
        credits = job.get("credits") or {}
        history.append(history_path(), {
            "event": "review", "key": key, "status": job["status"], "started": job["started"],
            "title": job.get("title", ""), "author": job.get("author", ""),
            "credits": round(credits.get("total") or 0, 3),
            "agents": [[label, round(c, 3)] for label, c in credits.get("agents") or []],
            "tokens": credits.get("tokens") or 0,
            # [model, credits]; model ids alone when an older version finished the review
            "models": [m if isinstance(m, str) else [m[0], round(m[1], 3)] for m in credits.get("models") or []],
            "model": job.get("model"),
            "comments": job.get("comments", 0), "must_fix": job.get("must_fix", 0),
        })

    def _pushed(self, key, posted, skipped, auto=False, summary=False):
        self.auto_busy.discard(key)
        history.append(history_path(), {"event": "push", "key": key, "posted": posted, "skipped": skipped,
                                        "auto": auto, "summary": summary})
        message = f'Posted {posted} comment{"s" if posted != 1 else ""}' + (" and the summary" if summary else "")
        if skipped:
            message += f", left out {skipped} already on the PR"
        notify(f"Review of {key} pushed" + (" automatically" if auto else ""), message)
        self._drop_review(key)
        self._save_state()
        self.next_poll_at = 0  # the PR moves to Reviewed
        self._render()

    def _approve(self, key, unpushed, latest=False):
        """Approve the PR at the commit Copilot reviewed; latest: at its latest commit, after I agreed to that.
        The comments stay, to push or discard."""
        window = self.windows.get(key)
        job = self.state["reviews"].get(key)
        if not window or not job:
            return
        comments = "1 comment from this review isn't" if unpushed == 1 else f"{unpushed} comments from this review aren't"
        if unpushed and not latest and rumps.alert(
            f"Approve {key}?",
            f"The {comments} pushed. You can still push or discard them afterwards.",
            ok="Approve", cancel="Cancel",
        ) != 1:
            return
        window.set_approving("approving")

        def worker():
            try:
                token = self.token or gh_token()
                if not latest:
                    head = copilot_review.head_sha(key, token)
                    if head != job["head"]:
                        AppHelper.callAfter(self._approve_moved, key, unpushed, job["head"], head)
                        return
                copilot_review.approve(key, None if latest else job["head"], token)
            except urllib.error.HTTPError as e:
                AppHelper.callAfter(self._approve_failed, key, http_error_text(e))
                return
            except Exception as e:
                AppHelper.callAfter(self._approve_failed, key, str(e))
                return
            AppHelper.callAfter(self._approved, key, unpushed)

        threading.Thread(target=worker, daemon=True).start()

    def _approve_moved(self, key, unpushed, reviewed, head):
        """The PR got new commits since the review: approving the reviewed commit fails, so ask about the latest."""
        window = self.windows.get(key)
        if window:
            window.set_approving(None)
        if rumps.alert(
            f"{key} has new commits since this review",
            f"Copilot reviewed {reviewed[:7]}; the PR is now at {head[:7]}. Approve the latest commit anyway? "
            "Its changes since the review weren't reviewed. Or discard this review and review it again.",
            ok="Approve anyway", cancel="Cancel",
        ) == 1:
            self._approve(key, unpushed, latest=True)

    def _approve_failed(self, key, error):
        window = self.windows.get(key)
        if window:
            window.set_approving(None)
        rumps.alert(f"Couldn't approve {key}", error)

    def _approved(self, key, unpushed, auto=False):
        self.auto_busy.discard(key)
        history.append(history_path(), {"event": "approve", "key": key, "auto": auto})
        notify(f"Approved {key}" + (" automatically" if auto else ""), self._tracked().get(key, {}).get("title", ""))
        if unpushed:  # keep the window, to push or discard the comments
            window = self.windows.get(key)
            if window:
                window.set_approving("approved")
        else:
            self._drop_review(key)
            self._save_state()
        self.next_poll_at = 0  # the PR moves to Reviewed, as approved
        self._render()

    def show_prompt(self, _):
        if self.prompt_window is None:
            self.prompt_window = PromptWindow()
        self.prompt_window.update()  # from the files, so it's always what the next review gets
        self.prompt_window.show()

    def show_stats(self, _):
        if self.stats_window is None:
            self.stats_window = stats_view.StatsWindow()
        self.stats_window.update(history.load(history_path()), self.config.get("monthly_budget"), self._month_spent())
        self.stats_window.show()

    def _discard_review(self, key):
        if rumps.alert(f"Discard the review of {key}?", "Its comments are deleted without being pushed.",
                       ok="Discard", cancel="Cancel") != 1:
            return
        history.append(history_path(), {"event": "discard", "key": key})
        self._drop_review(key)
        self._save_state()
        self._render()

    def ignore(self, key):
        if key not in self.state["ignored"]:
            self.state["ignored"].append(key)
            self._save_state()
        self._render()

    def unignore(self, key, _):
        self.state["ignored"] = [k for k in self.state["ignored"] if k != key]
        self._save_state()
        self._render()

    def refresh(self, _):
        self.poll()

    def show_settings(self, _=None, focus=None):
        if self.settings_window is None:
            self.settings_window = settings_view.SettingsWindow(self)
        self._update_settings(self._groups()[0])
        clip = (NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or "").strip()
        r = parse_repo(clip)
        new = r and repo_key(r).lower() not in {repo_key(x).lower() for x in self.repos}
        self.settings_window.show(suggest=clip if new else None, focus=focus)
        threading.Thread(target=self._models_worker, daemon=True).start()

    def _models_worker(self):
        models = copilot_review.models()
        AppHelper.callAfter(self._got_models, models)

    def _got_models(self, models):
        if models and models != self.models:
            self.models = models
            self._update_settings(self._groups()[0])

    def _update_settings(self, to_review):
        repos = []
        for r in self.repos:
            detail, rgb = self._repo_status(repo_key(r), to_review)
            repos.append((repo_key(r), detail, led_image(rgb)))
        self.settings_window.update(self.config, repos, self.month_spent, self._budget_share(),
                                    (copilot_review.default_model(), self.models))

    # settings: each returns an error message, or None once it's saved

    def track_repo(self, text):
        r = parse_repo(text)
        if not r:
            return "Not a repo. Expected e.g. https://github.com/owner/repo"
        self._check_config()
        same = next((x for x in self.repos if repo_key(x).lower() == repo_key(r).lower()), None)
        if same:
            return f"Already tracking {repo_key(same)}"
        self.repos.append(r)
        self._save_config()
        self.next_poll_at = 0  # a poll may be running with the old list
        self._render()

    def remove_repo(self, key):
        self._check_config()
        self.config["repos"] = [x for x in self.repos if repo_key(x) != key]
        self._save_config()
        self._forget_untracked()
        self._render()

    def set_automation(self, name, on):
        self._check_config()
        automation = self.config["automation"]
        if on == automation[name]:
            return
        if name == "approve" and on and rumps.alert(
            "Approve automatically?",
            "Every finished review in To review that found nothing to comment on will be approved without asking.",
            ok="Turn on", cancel="Cancel",
        ) != 1:
            return
        automation[name] = on
        if name == "review" and on:
            automation["since"] = time.time()  # not the backlog: only PRs still waiting, or active from now on
        self._save_config()
        self._render()

    def set_poll(self, text):
        try:
            minutes = parse_minutes(text, 1)
        except ValueError:
            return "Not a number of minutes. Expected 1 or more, e.g. 15."
        self._check_config()
        self.config["poll_minutes"] = minutes
        self._save_config()
        self.next_poll_at = min(self.next_poll_at, time.time() + minutes * 60)  # a shorter interval applies now
        self._render()

    def set_wait(self, text):
        try:
            minutes = parse_minutes(text, 0)
        except ValueError:
            return "Not a number of minutes. Expected e.g. 30."
        self._check_config()
        self.config["automation"]["wait_minutes"] = minutes
        self._save_config()
        self._render()

    def set_budget(self, text):
        try:
            budget = parse_budget(text)
        except ValueError:
            return "Not a number of AI credits. Expected e.g. 500, or nothing for no limit."
        self._check_config()
        if budget is None:
            self.config.pop("monthly_budget", None)
        else:
            self.config["monthly_budget"] = budget
        self._save_config()
        self._render()

    def set_model(self, model):
        """model: a Copilot model id, "auto", or None for Copilot's default."""
        self._check_config()
        if model:
            self.config["copilot_model"] = model
        else:
            self.config.pop("copilot_model", None)
        self._save_config()

    def set_authors(self, name, text):
        self._check_config()
        self.config[name] = parse_logins(text)
        self._save_config()
        self._render()


# --- CLI ---------------------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(
        prog="pr_reviewer",
        description="Lists the PRs of GitHub repos for you to review, in the macOS menu bar. Run without a command "
        "to start the app; the commands edit its config, and a running app picks up changes within a second.",
        epilog=f"Config: {CONFIG_PATH}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add", help="track a repo",
        description="Track a repo's open, non-draft PRs. Adding a repo that is already tracked does nothing.",
        epilog="examples:\n  pr_reviewer add https://github.com/owner/repo\n  pr_reviewer add owner/repo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("repo", metavar="REPO", help="repo URL or owner/repo")
    remove = commands.add_parser(
        "remove", help="stop tracking repos",
        description="Stop tracking one or more repos.\nExits with status 1 if any of them isn't tracked.",
        epilog="example:\n  pr_reviewer remove owner/repo other/repo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    remove.add_argument("repo", nargs="+", metavar="REPO", help="repo URL or owner/repo")
    for name, what in (
        ("allow", "Only PRs by these authors show up in To review. When the list is empty, everyone's do."),
        ("block", "PRs by these authors never show up in To review."),
    ):
        sub = commands.add_parser(
            name, help=f"show or edit the {name} list of authors",
            description=what + " Without logins: show the list.",
            epilog=f"examples:\n  pr_reviewer {name}\n  pr_reviewer {name} alice bob\n"
            f"  pr_reviewer {name} --remove alice",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.add_argument("logins", nargs="*", metavar="LOGIN", help="GitHub login")
        sub.add_argument("-r", "--remove", action="store_true", help="remove the logins instead of adding them")
    budget = commands.add_parser(
        "budget", help="show or set the monthly AI credit budget",
        description="AI credits Copilot reviews may use per calendar month. Once they're used up, no new review "
        "starts. Without a number: show the budget and this month's spending.",
        epilog="examples:\n  pr_reviewer budget\n  pr_reviewer budget 500\n  pr_reviewer budget --off",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    budget.add_argument("credits", nargs="?", type=float, metavar="AIC", help="the new budget")
    budget.add_argument("--off", action="store_true", help="remove the budget: no limit")
    stats = commands.add_parser("stats", help="show the AI credits spent on reviews, and the reviewed PRs",
                                description="AI credits spent on Copilot reviews today, this week, this month and in "
                                "all, then by model, then every reviewed PR with its review rounds.",
                                epilog=f"History: {history_path()}")
    stats.add_argument("--model", help="only the reviews that used this model, e.g. claude-sonnet-5.5")
    commands.add_parser("list", help="show tracked repos and the author lists",
                        description="Print the tracked repos, then the allow and block lists.")
    commands.add_parser("help", help="show this help and every command's parameters")
    args = parser.parse_args(argv)

    if args.command == "help":
        print(parser.format_help())
        for name, sub in commands.choices.items():
            if name != "help":
                print(f"--- {name} ---\n{sub.format_help()}")
        return

    try:
        config = load_config()
    except ValueError as e:
        sys.exit(f"{CONFIG_PATH}: {e}")
    repos = config["repos"]

    if args.command == "budget":
        if args.off:
            config.pop("monthly_budget", None)
            save_config(config)
        elif args.credits is not None:
            if not args.credits > 0:
                sys.exit("Expected a positive number of AI credits, or --off")
            config["monthly_budget"] = int(args.credits) if args.credits.is_integer() else args.credits
            save_config(config)
        spent = history.spending(history.load(history_path()))[2][1]
        limit = config.get("monthly_budget")
        print(f"{limit:g} AIC a month, {spent:.1f} used" if limit is not None
              else f"No limit, {spent:.1f} AIC used this month")
        return

    if args.command == "stats":
        events = history.load(history_path())
        models = history.by_model(events, 0)
        if args.model:
            if args.model not in dict(models):
                sys.exit(f'No reviews with {args.model}. Models used: {", ".join(m for m, _ in models) or "none"}')
            events = history.with_model(events, args.model)
        for label, credits, reviews in history.spending(events):
            print(f'{label:<11}{credits:>9.1f} AIC   {reviews} review{"s" if reviews != 1 else ""}')
        if not args.model:
            print()
            for model, credits in models:
                print(f"{model:<20}{credits:>9.1f} AIC")
        for pr in history.by_pr(events):
            n = len(pr["rounds"])
            print(f'\n{pr["key"]} {pr["title"]}\n  {pr["author"]} · {n} round{"s" if n != 1 else ""} · '
                  f'{pr["credits"]:.1f} AIC' + (f' · {history.tokens_text(pr["tokens"])} tokens' if pr["tokens"] else ""))
            for i, r in enumerate(pr["rounds"], 1):
                print(f"  Round {i}: {history.round_text(r)}")
        return

    if args.command == "list":
        for r in repos:
            print(repo_key(r))
        print(f'allow: {", ".join(config["allow"]) or "(everyone)"}')
        print(f'block: {", ".join(config["block"]) or "(nobody)"}')
        return

    if args.command in ("allow", "block"):
        logins = config[args.command]
        changes = parse_logins(" ".join(args.logins))
        if args.remove:
            gone = {same_login(a) for a in changes}
            config[args.command] = [a for a in logins if same_login(a) not in gone]
        else:
            have = {same_login(a) for a in logins}
            logins += [a for a in changes if same_login(a) not in have]
        if changes:
            save_config(config)
        print(", ".join(config[args.command]) or ("(everyone)" if args.command == "allow" else "(nobody)"))
        return

    if args.command == "add":
        r = parse_repo(args.repo)
        if not r:
            sys.exit("Not a repo. Expected e.g. https://github.com/owner/repo")
        if repo_key(r).lower() in {repo_key(x).lower() for x in repos}:
            print(f"Already tracking {repo_key(r)}")
            return
        repos.append(r)
        save_config(config)
        print(f"Tracking {repo_key(r)}")
        return

    missing = []
    for ref in args.repo:
        r = parse_repo(ref)
        match = r and next((x for x in repos if repo_key(x).lower() == repo_key(r).lower()), None)
        if not match:
            missing.append(ref)
            continue
        repos.remove(match)
        print(f"Removed {repo_key(match)}")
    if len(missing) < len(args.repo):
        save_config(config)
    if missing:
        sys.exit("Not tracked: " + ", ".join(missing))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        PRReviewerApp().run()
