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
import webbrowser
from datetime import datetime
from functools import partial

import objc
import rumps
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSControlSizeSmall,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSGradient,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPasteboard,
    NSPasteboardTypeString,
    NSTextAlignmentLeft,
    NSTextAttachment,
    NSTextField,
    NSTextTab,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSViewMinXMargin,
    NSViewWidthSizable,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSRunLoop, NSRunLoopCommonModes
from PyObjCTools import AppHelper

POLL_SECONDS = 3600
RETRY_SECONDS = 300
DEFAULT_ATTENTION_HOURS = 24
DEFAULT_ATTENTION_DAYS = 5
FLAG_BY = ("idle", "age")  # what makes a PR need attention: time without activity, or time since ready for review
MAX_LISTED = 15  # PRs needing attention shown at the top of the menu; the rest are in the repo submenus
API_URL = "https://api.github.com/graphql"
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#]\S*)?$")
CONFIG_PATH = os.path.join(rumps.application_support("menubar-pr-idle"), "config.json")

ORANGE = (1.00, 0.58, 0.00)
GRAY = (0.56, 0.56, 0.58)

# Activity = the PR being opened or marked ready, commits, force pushes, comments, reviews (thread replies
# are reviews too). Only the last 10 comments and reviews are fetched; bots' ones don't count.
QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt
        author { login }
        commits(last: 1) { nodes { commit { committedDate } } }
        comments(last: 10) { nodes { createdAt author { __typename } } }
        reviews(last: 10) { nodes { submittedAt author { __typename } } }
        timelineItems(last: 1, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, READY_FOR_REVIEW_EVENT]) {
          nodes { ... on HeadRefForcePushedEvent { createdAt } ... on ReadyForReviewEvent { createdAt } }
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


def hourglass_image(font, rgb=None):
    """The menu bar icon: orange when PRs need attention, otherwise a template that follows the menu bar."""
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(font.pointSize(), 0.0)
    if rgb:
        config = config.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_(
                [NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)]
            )
        )
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_("hourglass", "Repo PRs")
    image = image.imageWithSymbolConfiguration_(config)
    image.setTemplate_(rgb is None)
    return image


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


def fetch_repo(r, token):
    """Open, non-draft PRs of a repo, most idle first; None if the repo isn't found."""
    prs, after = [], None
    while True:
        data = graphql(QUERY, {"owner": r["owner"], "name": r["repo"], "after": after}, token)
        if data.get("repository") is None:
            return None
        page = data["repository"]["pullRequests"]
        prs += [summarize(pr) for pr in page["nodes"] if not pr["isDraft"]]
        if not page["pageInfo"]["hasNextPage"]:
            return sorted(prs, key=lambda pr: pr["active"])
        after = page["pageInfo"]["endCursor"]


def summarize(pr):
    def human(node):
        return (node["author"] or {}).get("__typename") != "Bot"

    times = [pr["createdAt"]]
    times += [c["commit"]["committedDate"] for c in pr["commits"]["nodes"]]
    times += [c["createdAt"] for c in pr["comments"]["nodes"] if human(c)]
    times += [r["submittedAt"] for r in pr["reviews"]["nodes"] if r["submittedAt"] and human(r)]
    times += [e["createdAt"] for e in pr["timelineItems"]["nodes"] if e]
    # up for review since it was opened as a non-draft, or last marked ready
    ready = [pr["createdAt"]] + [e["createdAt"] for e in pr["ready"]["nodes"] if e]
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": (pr["author"] or {}).get("login", "ghost"),
        "ready": max(parse_time(t) for t in ready),
        "active": max(parse_time(t) for t in times),
    }


def parse_time(s):
    return datetime.fromisoformat(s).timestamp()


def http_error_text(e):
    try:
        message = json.load(e)["message"]
    except (ValueError, KeyError, TypeError, OSError):
        message = e.reason
    return f"HTTP {e.code} {message}"


def repo_key(r):
    return f'{r["owner"]}/{r["repo"]}'


def parse_repo(text):
    """A repo URL (any page in it) or owner/repo; None if it isn't one."""
    match = REPO_RE.match(text.strip())
    return match and {"owner": match.group(1), "repo": match.group(2)}


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


def pr_row_lines(idle, age, title, detail, by, flagged, selected=False):
    """The two lines of a PR row, with the idle time and age in columns up front and their labels below them:

        6d 23h   12d   #123 Title
        idle     age   owner/repo · author

    The column PRs are flagged by (FLAG_BY) is bold, and orange once the PR needs attention.
    selected: in the menu's selection colour, for the hovered row.
    """
    size = NSFont.menuFontOfSize_(0).pointSize()
    bold = NSFont.monospacedDigitSystemFontOfSize_weight_(size, NSFontWeightSemibold)
    digits = NSFont.monospacedDigitSystemFontOfSize_weight_(size, NSFontWeightRegular)
    small = NSFont.menuFontOfSize_(NSFont.smallSystemFontSize())
    column = NSAttributedString.alloc().initWithString_attributes_("6d 23h", {NSFontAttributeName: bold}).size().width + 14
    style = NSMutableParagraphStyle.alloc().init()
    style.setTabStops_([
        NSTextTab.alloc().initWithTextAlignment_location_options_(NSTextAlignmentLeft, column * i, {}) for i in (1, 2)
    ])
    style.setLineBreakMode_(NSLineBreakByTruncatingTail)
    primary, secondary = NSColor.labelColor(), NSColor.secondaryLabelColor()
    flag_color = NSColor.colorWithSRGBRed_green_blue_alpha_(*ORANGE, 1.0) if flagged else primary
    if selected:
        primary = secondary = flag_color = NSColor.selectedMenuItemTextColor()

    def column(text, name):
        return (text, bold, flag_color) if by == name else (text, digits, primary)

    def line(parts):
        text = NSMutableAttributedString.alloc().init()
        for part, font, color in parts:
            text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(part, {
                NSFontAttributeName: font, NSForegroundColorAttributeName: color, NSParagraphStyleAttributeName: style,
            }))
        return text

    return (
        line([column(idle, "idle"), column("\t" + age, "age"), ("\t" + title, NSFont.menuFontOfSize_(0), primary)]),
        line([("idle\tage\t" + detail, small, secondary)]),
    )


# --- PR row ------------------------------------------------------------------

ROW_WIDTH = 620
ROW_HEIGHT = 40
ROW_INSET = 5  # the hover highlight, like the native menu selection
TEXT_X = 34


class PRRowView(NSView):
    """A two-line menu row with an Ignore button on the right. Clicking anywhere else opens the PR.

    Menu items with a view get no highlight or click handling from the menu, so this does both.
    """

    @objc.python_method
    def setup(self, on_open, on_ignore):
        self.on_open, self.on_ignore = on_open, on_ignore
        self.hover = False
        self.parts = None
        self.setAutoresizingMask_(NSViewWidthSizable)

        self.led = NSImageView.imageViewWithImage_(led_image(GRAY))
        self.led.setFrame_(NSMakeRect(TEXT_X - 18, (ROW_HEIGHT - LED_SIZE) / 2, LED_SIZE, LED_SIZE))
        self.addSubview_(self.led)

        button = NSButton.buttonWithTitle_target_action_("Ignore", self, "ignoreClicked:")
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setControlSize_(NSControlSizeSmall)
        button.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        button.setRefusesFirstResponder_(True)  # no focus ring when the menu opens
        button.sizeToFit()
        button.setAutoresizingMask_(NSViewMinXMargin)
        size = button.frame().size
        right = ROW_WIDTH - 14 - size.width
        button.setFrameOrigin_(NSMakePoint(right, (ROW_HEIGHT - size.height) / 2))
        self.addSubview_(button)

        self.lines = []
        for y, height in ((3, 18), (21, 15)):
            label = NSTextField.labelWithString_("")
            label.setFrame_(NSMakeRect(TEXT_X, y, right - 6 - TEXT_X, height))
            label.setAutoresizingMask_(NSViewWidthSizable)
            self.addSubview_(label)
            self.lines.append(label)

        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        ))

    @objc.python_method
    def fill(self, idle, age, title, detail, by, flagged):
        parts = (idle, age, title, detail, by, flagged)
        if parts != self.parts:
            self.parts = parts
            self.led.setImage_(led_image(ORANGE if flagged else GRAY))
            self._color_text()

    @objc.python_method
    def _color_text(self):
        for label, text in zip(self.lines, pr_row_lines(*self.parts, selected=self.hover)):
            label.setAttributedStringValue_(text)

    @objc.python_method
    def _set_hover(self, hover):
        if hover != self.hover:
            self.hover = hover
            if self.parts:
                self._color_text()
            self.setNeedsDisplay_(True)

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
        # the labels would swallow clicks; the button keeps its own
        hit = objc.super(PRRowView, self).hitTest_(point)
        return hit if hit is None or isinstance(hit, NSButton) else self

    def mouseEntered_(self, event):
        self._set_hover(True)

    def mouseExited_(self, event):
        self._set_hover(False)

    def mouseDown_(self, event):
        pass  # claim the click, so mouseUp_ comes here

    def mouseUp_(self, event):
        item = self.enclosingMenuItem()
        menu = item and item.menu()
        while menu is not None and menu.supermenu() is not None:
            menu = menu.supermenu()
        if menu is not None:
            menu.cancelTracking()
        self._set_hover(False)
        AppHelper.callAfter(self.on_open)

    def ignoreClicked_(self, sender):
        # with the menu still open; deferred, because ignoring rebuilds the menu, and with it this view
        AppHelper.callAfter(self.on_ignore)


def pr_row_item(key, on_open, on_ignore):
    # placeholder title: rumps keys items by title and skips duplicates
    item = rumps.MenuItem(key)
    row = PRRowView.alloc().initWithFrame_(NSMakeRect(0, 0, ROW_WIDTH, ROW_HEIGHT))
    row.setup(on_open, on_ignore)
    item._menuitem.setView_(row)
    return item


def ask_thresholds(hours, days):
    """Both attention thresholds in one dialog: (hours text, days text) as typed, or None if cancelled."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Needs attention after")
    alert.setInformativeText_(
        "Idle time: hours without a commit, comment or review.\n"
        "Age: days since the PR was ready for review.\n"
        "Flag by picks which one applies."
    )
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 220, 56))
    fields = []
    for y, label, value, unit in ((32, "Idle time", hours, "hours"), (2, "Age", days, "days")):
        for text, x, width in ((label, 0, 70), (unit, 152, 60)):
            text_label = NSTextField.labelWithString_(text)
            text_label.setFrame_(NSMakeRect(x, y + 3, width, 17))
            view.addSubview_(text_label)
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(74, y, 72, 22))
        field.setStringValue_(f"{value:g}")
        view.addSubview_(field)
        fields.append(field)
    fields[0].setNextKeyView_(fields[1])
    fields[1].setNextKeyView_(fields[0])
    alert.setAccessoryView_(view)
    alert.layout()
    alert.window().setInitialFirstResponder_(fields[0])
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    if alert.runModal() != NSAlertFirstButtonReturn:
        return None
    return fields[0].stringValue(), fields[1].stringValue()


def parse_threshold(text):
    """A positive number of hours or days, an int when whole; None if the text isn't one."""
    try:
        value = float(text.strip())
    except ValueError:
        return None
    if not (value > 0 and math.isfinite(value)):
        return None
    return int(value) if value.is_integer() else value


def notify(title, message):
    subprocess.Popen([
        "osascript",
        "-e", "on run argv",
        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
        "-e", "end run",
        title, message,
    ])


# --- Config ------------------------------------------------------------------

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
    hours = config.get("attention_hours")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        config["attention_hours"] = DEFAULT_ATTENTION_HOURS
    days = config.get("attention_days")
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        config["attention_days"] = DEFAULT_ATTENTION_DAYS
    if config.get("attention_by") not in FLAG_BY:
        config["attention_by"] = "idle"
    ignored = config.get("ignored")  # "owner/repo#123"
    if not (isinstance(ignored, list) and all(isinstance(k, str) for k in ignored)):
        config["ignored"] = []
    return config


def save_config(config):
    # write-then-rename, so the app never reads a half-written file
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_mtime():
    try:
        return os.stat(CONFIG_PATH).st_mtime_ns
    except FileNotFoundError:
        return None


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


# --- App ---------------------------------------------------------------------

class PRIdleApp(rumps.App):
    def __init__(self):
        super().__init__("PR Idle", title="", quit_button=None)
        # Menu bar only: no Dock icon.
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        try:
            self.config = load_config()
        except ValueError:
            self.config = {"repos": [], "attention_hours": DEFAULT_ATTENTION_HOURS,
                           "attention_days": DEFAULT_ATTENTION_DAYS, "attention_by": "idle", "ignored": []}
        self.config_mtime = config_mtime()
        self.info = {}  # repo key -> {"prs": [summarize() dicts, most idle first]} or {"error": text}
        self.attention = set()  # "owner/repo#123" of PRs over the threshold: see _flagged_keys()
        self.token = None
        self.busy = False
        self.error = None
        self.updated_at = None
        self.next_poll_at = 0
        self.rendered_minute = None
        self.menu_structure = None
        self.rows = {}  # (section, "owner/repo#123") -> PRRowView
        self.repo_items = {}  # repo key -> MenuItem
        self.attention_item = self.status_item = None
        self.timer = rumps.Timer(self.tick, 1)
        rumps.events.before_start.register(self._on_start)

    @property
    def repos(self):
        return self.config["repos"]

    def _on_start(self):
        install_edit_menu()
        self._render()
        self.timer.start()
        # keep ticking while the menu is open, so the countdown stays live
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer._nstimer, NSRunLoopCommonModes)

    # config

    def _save_config(self):
        save_config(self.config)
        self.config_mtime = config_mtime()

    def _check_config(self):
        """Pick up edits made outside the app, e.g. by `pr_idle.py add`."""
        mtime = config_mtime()
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            config = load_config()
        except ValueError:
            return  # broken hand edit: keep what we have
        before = {repo_key(r) for r in self.repos}
        flagging = self._flagging()
        self.config = config
        keys = {repo_key(r) for r in self.repos}
        self.info = {k: v for k, v in self.info.items() if k in keys}
        if keys - before:
            self.next_poll_at = 0  # fetch new repos on the next tick
        if self._flagging() != flagging:
            self.attention = self._flagged_keys()  # no notifications for a new setting, or PRs no longer ignored
        self._update_attention()
        self._render()

    # polling

    def tick(self, _):
        self._check_config()
        now = time.time()
        if now >= self.next_poll_at:
            self.poll()
        # PRs cross the threshold between polls, and the idle times shown in the menu grow
        if self._update_attention() or int(now // 60) != self.rendered_minute:
            self._render()
        else:
            self._render_status()

    def poll(self, _=None):
        if self.busy or not self.repos:
            return
        self.busy = True
        self.next_poll_at = time.time() + POLL_SECONDS
        snapshot = list(self.repos)
        threading.Thread(target=self._poll_worker, args=(snapshot,), daemon=True).start()

    def _poll_worker(self, snapshot):
        results, error = {}, None
        try:
            if not self.token:
                self.token = gh_token()
            for r in snapshot:
                prs = fetch_repo(r, self.token)
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
        # PRs that were already idle when their repo was added (or the app started) don't notify
        first = {k for k in results if "prs" not in self.info.get(k, {})}
        self.info.update({k: v for k, v in results.items() if k in current})
        # forget ignored PRs that were merged or closed (or turned back into drafts)
        fetched = {k for k, v in results.items() if "prs" in v}
        still_open = set(self._open_prs())
        self._edit_ignored(lambda keys: [k for k in keys if k.split("#")[0] not in fetched or k in still_open])
        self._update_attention(quiet=first)
        self._render()

    # attention

    def _since(self, pr):
        """Since when the PR counts toward attention: its last activity, or when it was ready for review."""
        return pr["active"] if self.config["attention_by"] == "idle" else pr["ready"]

    def _flagging(self):
        """The settings that decide which PRs need attention."""
        return tuple(self.config[k] for k in ("attention_by", "attention_hours", "attention_days", "ignored"))

    def _open_prs(self):
        """Every open PR of the tracked repos, keyed by "owner/repo#123", most idle (or oldest) first."""
        prs = [(f'{k}#{pr["number"]}', pr) for k, info in self.info.items() for pr in info.get("prs", [])]
        return dict(sorted(prs, key=lambda item: self._since(item[1])))

    def _tracked(self):
        """The open PRs that aren't ignored, most idle (or oldest) first."""
        return {k: pr for k, pr in self._open_prs().items() if k not in self.config["ignored"]}

    def _flagged_keys(self):
        """The PRs idle for longer than attention_hours, or ready for review for longer than attention_days."""
        if self.config["attention_by"] == "idle":
            cutoff = time.time() - self.config["attention_hours"] * 3600
        else:
            cutoff = time.time() - self.config["attention_days"] * 86400
        return {k for k, pr in self._tracked().items() if self._since(pr) < cutoff}

    def _update_attention(self, quiet=()):
        """Notify about PRs that just crossed the threshold; True if the set changed."""
        flagged = self._flagged_keys()
        if flagged == self.attention:
            return False
        new = sorted(k for k in flagged - self.attention if k.split("#")[0] not in quiet)
        self.attention = flagged
        if len(new) == 1:
            pr = self._tracked()[new[0]]
            how = "no activity" if self.config["attention_by"] == "idle" else "ready for review"
            notify(f'{new[0]}: {how} for {duration(time.time() - self._since(pr))}', pr["title"])
        elif new:
            notify(f"{len(new)} PRs need attention", ", ".join(new))
        return True

    # rendering

    def _render(self):
        self.rendered_minute = int(time.time() // 60)
        self._render_title()
        self._render_menu()

    def _render_title(self):
        font = NSFont.menuBarFontOfSize_(0)
        attrs = {NSFontAttributeName: font}
        title = NSMutableAttributedString.alloc().init()

        def text(s):
            title.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(s, attrs))

        icon = hourglass_image(font, ORANGE if self.attention else None)
        size = icon.size()
        attachment = NSTextAttachment.alloc().init()
        attachment.setImage_(icon)
        attachment.setBounds_(NSMakeRect(0, (font.capHeight() - size.height) / 2, size.width, size.height))
        title.appendAttributedString_(NSAttributedString.attributedStringWithAttachment_(attachment))
        if self.attention:
            text(f" {len(self.attention)}")
        if self.error:
            text(" ⚠︎")
        self._nsapp.nsstatusitem.button().setAttributedTitle_(title)

    def _render_menu(self):
        tracked = self._tracked()
        structure = (
            [k for k in tracked if k in self.attention][:MAX_LISTED],
            len(self.attention),
            [(repo_key(r), "error" in self.info.get(repo_key(r), {}),
              [k for k in tracked if k.split("#")[0] == repo_key(r)]) for r in self.repos],
            [k for k in self._open_prs() if k in self.config["ignored"]],
            self.config["attention_by"],
        )
        if structure != self.menu_structure:
            self.menu_structure = structure
            self._build_menu()
        now = time.time()
        for (section, key), row in self.rows.items():
            self._fill_row(row, key, tracked[key], section, now)
        for r in self.repos:
            self._fill_repo_item(self.repo_items[repo_key(r)], r)
        if self.attention_item is not None:
            self.attention_item._menuitem.setSubtitle_(
                f'{self.config["attention_hours"]:g}h idle · {self.config["attention_days"]:g}d old'
            )
        self._render_status()

    def _build_menu(self):
        # rumps registers every MenuItem in a class-level dict and clear() doesn't drop them
        self._forget_items(self.menu)
        self.menu.clear()
        listed, needing, repos, ignored, by = self.menu_structure
        prs = self._open_prs()
        self.rows = {}
        items = []

        def row(section, key):
            item = pr_row_item(key, partial(self._open, prs[key]["url"], None), partial(self.ignore, key))
            self.rows[section, key] = item._menuitem.view()
            return item

        if needing:
            items.append(rumps.MenuItem(f"Needs attention ({needing})"))
            for key in listed:
                items.append(row("top", key))
            if needing > len(listed):
                items.append(rumps.MenuItem(f"+{needing - len(listed)} more in the repo menus"))
            items.append(None)

        self.repo_items = {}
        for rk, failed, keys in repos:
            item = self.repo_items[rk] = rumps.MenuItem(rk)
            entries = []
            if failed:
                entries.append(rumps.MenuItem(self.info[rk]["error"]))
            elif rk not in self.info:
                entries.append(rumps.MenuItem("Checking…"))
            elif not keys:
                entries.append(rumps.MenuItem(
                    "Only ignored PRs" if any(k.split("#")[0] == rk for k in ignored) else "No open PRs"
                ))
            # most idle (or oldest) first, so the ones needing attention come first; a separator below them
            for i, key in enumerate(keys):
                if i and (keys[i - 1] in self.attention) != (key in self.attention):
                    entries.append(None)
                entries.append(row("repo", key))
            entries += [
                None,
                rumps.MenuItem("Open on GitHub", callback=partial(self._open, f"https://github.com/{rk}/pulls")),
                rumps.MenuItem("Stop tracking", callback=partial(self.remove_repo, rk)),
            ]
            item.update(entries)
            items.append(item)
        if ignored:
            item = rumps.MenuItem(f"Ignored ({len(ignored)})")
            item.update([rumps.MenuItem("Click one to stop ignoring it")] + [
                rumps.MenuItem(f'{key} {short(prs[key]["title"])}', callback=partial(self.unignore, key))
                for key in ignored
            ])
            items.append(item)
        if repos:
            items.append(None)

        items.append(rumps.MenuItem("Track repo…", callback=self.add_repo))
        self.attention_item = None
        if repos:
            items.append(rumps.MenuItem("Refresh now", callback=self.refresh))
            flag_by = rumps.MenuItem("Flag by: " + ("idle time" if by == "idle" else "age"))
            for name, title, subtitle in (
                ("idle", "Idle time", "No commit, comment or review for a while"),
                ("age", "Age", "Ready for review for a while"),
            ):
                choice = rumps.MenuItem(title, callback=partial(self.set_flag_by, name))
                choice.state = name == by
                choice._menuitem.setSubtitle_(subtitle)
                flag_by.add(choice)
            items.append(flag_by)
            self.attention_item = rumps.MenuItem("Needs attention after…", callback=self.set_thresholds)
            items.append(self.attention_item)
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

    def _fill_row(self, row, key, pr, section, now):
        rk = key.split("#")[0]
        row.fill(
            duration(now - pr["active"]),
            duration(now - pr["ready"]),
            f'#{pr["number"]} {short(pr["title"])}',
            f'{rk} · {pr["author"]}' if section == "top" else pr["author"],
            self.config["attention_by"],
            key in self.attention,
        )

    def _fill_repo_item(self, item, r):
        key = repo_key(r)
        info = self.info.get(key)
        prs = [pr for pr in (info or {}).get("prs", []) if f'{key}#{pr["number"]}' not in self.config["ignored"]]
        ignored = len((info or {}).get("prs", [])) - len(prs)
        needing = sum(1 for pr in prs if f'{key}#{pr["number"]}' in self.attention)
        if info is None:
            detail = "Checking…"
        elif "error" in info:
            detail = info["error"]
        else:
            detail = f"{len(prs)} open" + (f" · {needing} need attention" if needing else "")
            detail += f" · {ignored} ignored" if ignored else ""
        item._menuitem.setImage_(led_image(ORANGE if needing else GRAY))
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

    def refresh(self, _):
        self.poll()

    def add_repo(self, _):
        clip = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or ""
        response = rumps.Window(
            message="Paste a repo URL (or owner/repo). Its open, non-draft PRs are checked every hour.",
            title="Track repo",
            default_text=clip.strip() if parse_repo(clip) else "",
            ok="Track",
            cancel="Cancel",
            dimensions=(360, 24),
        ).run()
        if not response.clicked:
            return
        r = parse_repo(response.text)
        if not r:
            rumps.alert("Not a repo", "Expected e.g. https://github.com/owner/repo")
            return
        self._check_config()
        if repo_key(r).lower() in {repo_key(x).lower() for x in self.repos}:
            return
        self.repos.append(r)
        self._save_config()
        self._render()
        self.next_poll_at = 0  # a poll may be running with the old list

    def remove_repo(self, key, _):
        self._check_config()
        self.config["repos"] = [x for x in self.repos if repo_key(x) != key]
        self.config["ignored"] = [k for k in self.config["ignored"] if k.split("#")[0] != key]
        self.info.pop(key, None)
        self._save_config()
        self._update_attention()
        self._render()

    def _edit_ignored(self, change):
        """Replace the ignored list with change(list), and save it if that changed it."""
        self._check_config()  # don't overwrite edits made outside the app
        ignored = change(self.config["ignored"])
        if ignored != self.config["ignored"]:
            self.config["ignored"] = ignored
            self._save_config()

    def ignore(self, key):
        """No attention, notification or menu row for the PR, until it's merged or closed."""
        self._edit_ignored(lambda keys: keys if key in keys else keys + [key])
        self._update_attention()
        self._render()

    def unignore(self, key, _):
        self._edit_ignored(lambda keys: [k for k in keys if k != key])
        self.attention |= self._flagged_keys() & {key}  # no notification: you just put it back
        self._render()

    def set_flag_by(self, by, _):
        self._check_config()
        self.config["attention_by"] = by
        self._save_config()
        self.attention = self._flagged_keys()  # no notifications for a new setting
        self._render()

    def set_thresholds(self, _):
        """Both thresholds, hours idle and days since ready for review, in one dialog. Flag by picks which applies."""
        answer = ask_thresholds(self.config["attention_hours"], self.config["attention_days"])
        if answer is None:
            return
        hours, days = (parse_threshold(text) for text in answer)
        if hours is None or days is None:
            rumps.alert("Not a number", "Expected a number of hours and of days, e.g. 24 and 5, or 1.5 and 2.5")
            return
        self._check_config()
        self.config.update(attention_hours=hours, attention_days=days)
        self._save_config()
        self.attention = self._flagged_keys()  # no notifications for a new threshold
        self._render()


# --- CLI ---------------------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(
        prog="pr_idle",
        description="Tracks the open PRs of GitHub repos from the macOS menu bar. Run without a command to start "
        "the app; the commands edit its config, and a running app picks up changes within a second.",
        epilog=f"Config: {CONFIG_PATH}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add", help="track a repo",
        description="Track a repo's open, non-draft PRs. Adding a repo that is already tracked does nothing.",
        epilog="examples:\n  pr_idle add https://github.com/owner/repo\n  pr_idle add owner/repo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("repo", metavar="REPO", help="repo URL or owner/repo")
    remove = commands.add_parser(
        "remove", help="stop tracking repos",
        description="Stop tracking one or more repos.\nExits with status 1 if any of them isn't tracked.",
        epilog="example:\n  pr_idle remove owner/repo other/repo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    remove.add_argument("repo", nargs="+", metavar="REPO", help="repo URL or owner/repo")
    by = commands.add_parser(
        "by", help="show or set what flags PRs: idle time or age",
        description="idle: PRs need attention after HOURS without a commit, comment or review (see hours).\n"
        "age: PRs need attention once they've been ready for review for DAYS (see days).",
        epilog="examples:\n  pr_idle by\n  pr_idle by age",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    by.add_argument("by", nargs="?", choices=FLAG_BY, help="new setting (default: show it)")
    hours = commands.add_parser(
        "hours", help="show or set the idle threshold",
        description="When flagging by idle time: PRs need attention after this many hours without a commit, "
        "comment or review.",
        epilog="examples:\n  pr_idle hours\n  pr_idle hours 48",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hours.add_argument("hours", nargs="?", type=float, metavar="HOURS", help="new threshold (default: show it)")
    days = commands.add_parser(
        "days", help="show or set the age threshold",
        description="When flagging by age: PRs need attention once they've been ready for review for this many days.",
        epilog="examples:\n  pr_idle days\n  pr_idle days 7",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    days.add_argument("days", nargs="?", type=float, metavar="DAYS", help="new threshold (default: show it)")
    commands.add_parser("list", help="show tracked repos", description="Print one line per tracked repo.")
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

    if args.command == "list":
        for r in repos:
            print(repo_key(r))
        return

    if args.command == "by":
        if args.by is None:
            print(config["attention_by"])
            return
        config["attention_by"] = args.by
        save_config(config)
        print(f"Flagging PRs by {'idle time' if args.by == 'idle' else 'age'}")
        return

    if args.command in ("hours", "days"):
        name, value = f"attention_{args.command}", getattr(args, args.command)
        if value is None:
            print(f"{config[name]:g}")
            return
        if not value > 0:
            sys.exit(f"Expected a positive number of {args.command}")
        config[name] = int(value) if value.is_integer() else value
        save_config(config)
        what = "idle" if args.command == "hours" else "old"
        print(f"PRs need attention after {config[name]:g}{args.command[0]} {what}")
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
        config["ignored"] = [k for k in config["ignored"] if k.split("#")[0] != repo_key(match)]
        print(f"Removed {repo_key(match)}")
    if len(missing) < len(args.repo):
        save_config(config)
    if missing:
        sys.exit("Not tracked: " + ", ".join(missing))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        PRIdleApp().run()
