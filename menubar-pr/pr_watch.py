import argparse
import json
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
from functools import partial

import rumps
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSGradient,
    NSImage,
    NSMutableAttributedString,
    NSPasteboard,
    NSPasteboardTypeString,
    NSTextAttachment,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSRunLoop, NSRunLoopCommonModes
from PyObjCTools import AppHelper

POLL_SECONDS = 60
API_URL = "https://api.github.com/graphql"
PR_RE = re.compile(r"(?:https?://github\.com/)?([\w.-]+)/([\w.-]+)(?:/pull/|#)(\d+)(?:[/?#]\S*)?")
CONFIG_PATH = os.path.join(rumps.application_support("menubar-pr"), "config.json")

RED = (1.00, 0.23, 0.19)
YELLOW = (1.00, 0.76, 0.03)
BLUE = (0.04, 0.52, 1.00)
GREEN = (0.19, 0.82, 0.35)
PURPLE = (0.69, 0.32, 0.87)
GRAY = (0.56, 0.56, 0.58)

STATES = {
    "conflict": (RED, "Has conflicts"),
    "failing": (RED, "Checks failing"),
    "comments": (YELLOW, "Unresolved comments"),
    "waiting": (BLUE, "Not mergeable yet"),
    "ready": (GREEN, "Ready to merge"),
    "merged": (PURPLE, "Merged"),
    "closed": (GRAY, "Closed"),
    "pending": (GRAY, "Checking…"),
    "error": (GRAY, "Not found or no access"),
}

# reviewThreads(first: 100) undercounts unresolved threads on PRs with more than 100 of them.
PR_FRAGMENT = """
fragment F on PullRequest {
  title url state isDraft mergeable mergeStateStatus reviewDecision
  reviewThreads(first: 100) { nodes { isResolved } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""


# --- LED icons ---------------------------------------------------------------

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


def fetch_prs(prs, token):
    """Fetch all PRs in one GraphQL request; returns a list aligned with `prs` (None = not found)."""
    parts = [
        f'p{i}: repository(owner: {json.dumps(p["owner"])}, name: {json.dumps(p["repo"])}) '
        f'{{ pullRequest(number: {p["number"]}) {{ ...F }} }}'
        for i, p in enumerate(prs)
    ]
    query = "query { " + " ".join(parts) + " }" + PR_FRAGMENT
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    data = body.get("data")
    if data is None:
        raise RuntimeError("; ".join(e.get("message", "?") for e in body.get("errors", [])) or "empty response")
    return [(data.get(f"p{i}") or {}).get("pullRequest") for i in range(len(prs))]


def http_error_text(e):
    try:
        message = json.load(e)["message"]
    except (ValueError, KeyError, TypeError, OSError):
        message = e.reason
    return f"HTTP {e.code} {message}"


def unresolved_count(pr):
    return sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])


def classify(pr):
    """Map a GraphQL PR to a state key; None means GitHub is still computing mergeability."""
    if pr["state"] == "MERGED":
        return "merged"
    if pr["state"] == "CLOSED":
        return "closed"
    if pr["mergeable"] == "CONFLICTING":
        return "conflict"
    commits = pr["commits"]["nodes"]
    rollup = commits and commits[0]["commit"]["statusCheckRollup"]
    if rollup and rollup["state"] in ("FAILURE", "ERROR"):
        return "failing"
    if unresolved_count(pr) or pr["reviewDecision"] == "CHANGES_REQUESTED":
        return "comments"
    if pr["mergeable"] == "UNKNOWN" or pr["mergeStateStatus"] == "UNKNOWN":
        return None
    if rollup and rollup["state"] in ("PENDING", "EXPECTED"):
        return "waiting"  # UNSTABLE also covers running non-required checks
    if not pr["isDraft"] and pr["mergeStateStatus"] in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return "ready"
    return "waiting"


def pr_key(p):
    return f'{p["owner"]}/{p["repo"]}#{p["number"]}'


def parse_pr(text):
    """A PR URL or owner/repo#123, optionally followed by a label; None if there's no PR in it."""
    match = PR_RE.search(text)
    if not match:
        return None
    number = int(match.group(3))
    label = text[match.end():].strip() or f"#{number}"
    return {"owner": match.group(1), "repo": match.group(2), "number": number, "label": label}


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
    """Raises ValueError if the file isn't a valid watchlist."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.setdefault("prs", []), list):
        raise ValueError('expected an object with a "prs" list')
    for p in config["prs"]:
        if not (isinstance(p, dict) and isinstance(p.get("owner"), str) and isinstance(p.get("repo"), str)
                and isinstance(p.get("number"), int)):
            raise ValueError(f"invalid PR entry: {json.dumps(p)}")
        if not isinstance(p.get("label"), str) or not p["label"].strip():
            p["label"] = f'#{p["number"]}'
    config.setdefault("show_labels", True)
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


# --- App ---------------------------------------------------------------------

class PRWatchApp(rumps.App):
    def __init__(self):
        super().__init__("PR Watch", title="PRs", quit_button=None)
        # Menu bar only: no Dock icon.
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        try:
            self.config = load_config()
        except ValueError:
            self.config = {"prs": [], "show_labels": True}
        self.config_mtime = config_mtime()
        self.info = {}  # key -> {"state", "title", "url", "unresolved"}
        self.token = None
        self.busy = False
        self.error = None
        self.updated_at = None
        self.next_poll_at = 0
        self.menu_structure = None
        self.pr_items = {}  # key -> MenuItem
        self.labels_item = self.status_item = None
        self.timer = rumps.Timer(self.tick, 1)
        rumps.events.before_start.register(self._on_start)

    @property
    def prs(self):
        return self.config["prs"]

    def _on_start(self):
        self._render()
        self.timer.start()
        # keep ticking while the menu is open, so the countdown stays live
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer._nstimer, NSRunLoopCommonModes)

    # config

    def _save_config(self):
        save_config(self.config)
        self.config_mtime = config_mtime()

    def _check_config(self):
        """Pick up edits made outside the app, e.g. by `pr_watch.py add`."""
        mtime = config_mtime()
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            config = load_config()
        except ValueError:
            return  # broken hand edit: keep what we have
        before = {pr_key(p) for p in self.prs}
        self.config = config
        keys = {pr_key(p) for p in self.prs}
        self.info = {k: v for k, v in self.info.items() if k in keys}
        if keys - before:
            self.next_poll_at = 0  # fetch new PRs on the next tick
        self._render()

    # polling

    def tick(self, _):
        self._check_config()
        if time.time() >= self.next_poll_at:
            self.poll()
        self._render_status()

    def poll(self, _=None):
        if self.busy or not self.prs:
            return
        self.busy = True
        self.next_poll_at = time.time() + POLL_SECONDS
        snapshot = list(self.prs)
        threading.Thread(target=self._poll_worker, args=(snapshot,), daemon=True).start()

    def _poll_worker(self, snapshot):
        try:
            if not self.token:
                self.token = gh_token()
            results = fetch_prs(snapshot, self.token)
            AppHelper.callAfter(self._apply, snapshot, results, None)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.token = None  # refetch from gh next time
            AppHelper.callAfter(self._apply, snapshot, None, http_error_text(e))
        except Exception as e:
            AppHelper.callAfter(self._apply, snapshot, None, str(e))

    def _apply(self, snapshot, results, error):
        self.busy = False
        self.error = error
        if results is not None:
            self.updated_at = time.strftime("%H:%M")
            current = {pr_key(p) for p in self.prs}
            for p, pr in zip(snapshot, results):
                key = pr_key(p)
                if key in current:
                    self._update_pr(p, pr)
        self._render()

    def _update_pr(self, p, pr):
        key = pr_key(p)
        old = self.info.get(key)
        if pr is None:
            self.info[key] = {"state": "error", "title": "", "url": self._url(p), "unresolved": 0}
            return
        state = classify(pr) or (old["state"] if old else "pending")
        unresolved = unresolved_count(pr)
        self.info[key] = {"state": state, "title": pr["title"], "url": pr["url"], "unresolved": unresolved}
        if not old or old["state"] in ("pending", "error"):
            return
        if state != old["state"]:
            notify(f'{p["label"]}: {STATES[state][1]}', pr["title"])
        elif state == "comments" and unresolved > old["unresolved"]:
            notify(f'{p["label"]}: new review comment', pr["title"])

    @staticmethod
    def _url(p):
        return f'https://github.com/{p["owner"]}/{p["repo"]}/pull/{p["number"]}'

    # rendering

    def _state(self, p):
        return self.info.get(pr_key(p), {}).get("state", "pending")

    def _render(self):
        self._render_title()
        self._render_menu()

    def _render_title(self):
        font = NSFont.menuBarFontOfSize_(0)
        attrs = {NSFontAttributeName: font}
        title = NSMutableAttributedString.alloc().init()

        def text(s):
            title.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(s, attrs))

        if not self.prs:
            text("PRs")
        for i, p in enumerate(self.prs):
            if i:
                text("  " if self.config["show_labels"] else " ")
            attachment = NSTextAttachment.alloc().init()
            attachment.setImage_(led_image(STATES[self._state(p)][0]))
            attachment.setBounds_(NSMakeRect(0, (font.capHeight() - LED_SIZE) / 2, LED_SIZE, LED_SIZE))
            title.appendAttributedString_(NSAttributedString.attributedStringWithAttachment_(attachment))
            if self.config["show_labels"]:
                text(" " + p["label"])
        if self.error:
            text(" ⚠︎")
        self._nsapp.nsstatusitem.button().setAttributedTitle_(title)

    def _render_menu(self):
        structure = (
            [(pr_key(p), p["label"]) for p in self.prs],
            any(self._state(p) in ("merged", "closed") for p in self.prs),
        )
        if structure != self.menu_structure:
            self.menu_structure = structure
            self._build_menu()
        for p in self.prs:
            self._fill_pr_item(self.pr_items[pr_key(p)], p)
        self.labels_item.state = self.config["show_labels"]
        self._render_status()

    def _build_menu(self):
        # rumps registers every MenuItem in a class-level dict and clear() doesn't drop them
        self._forget_items(self.menu)
        self.menu.clear()
        # placeholder titles: rumps keys items by title and skips duplicates
        self.pr_items = {pr_key(p): rumps.MenuItem(pr_key(p)) for p in self.prs}
        items = list(self.pr_items.values())
        if items:
            items.append(None)

        items.append(rumps.MenuItem("Add PR…", callback=self.add_pr))
        if self.prs:
            rename = rumps.MenuItem("Rename")
            remove = rumps.MenuItem("Remove")
            for p in self.prs:
                rename.add(rumps.MenuItem(f'{p["label"]}  ({pr_key(p)})', callback=partial(self.rename_pr, pr_key(p))))
                remove.add(rumps.MenuItem(f'{p["label"]}  ({pr_key(p)})', callback=partial(self.remove_pr, pr_key(p))))
            items += [rename, remove]
            if self.menu_structure[1]:
                items.append(rumps.MenuItem("Remove merged/closed", callback=self.remove_finished))
            items.append(rumps.MenuItem("Refresh now", callback=self.poll))
        self.labels_item = rumps.MenuItem("Show labels", callback=self.toggle_labels)
        items += [self.labels_item, None]

        self.status_item = rumps.MenuItem("")
        items.append(self.status_item)
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))
        self.menu.update(items)

    @classmethod
    def _forget_items(cls, menu):
        for item in menu.values():
            if isinstance(item, rumps.MenuItem):
                rumps.rumps.NSApp._ns_to_py_and_callback.pop(item._menuitem, None)
                cls._forget_items(item)

    def _fill_pr_item(self, item, p):
        info = self.info.get(pr_key(p), {})
        state = self._state(p)
        detail = STATES[state][1]
        if state == "comments" and info.get("unresolved"):
            detail += f' ({info["unresolved"]})'
        name = info.get("title") or pr_key(p)
        if len(name) > 60:
            name = name[:59] + "…"
        item.title = f'{p["label"]} — {name}'
        item.set_callback(partial(self._open, info.get("url") or self._url(p)))
        item._menuitem.setImage_(led_image(STATES[state][0]))
        item._menuitem.setSubtitle_(f"{pr_key(p)} · {detail}")

    def _render_status(self):
        if self.status_item is None:
            return
        if not self.prs:
            text = "No PRs watched"
        elif self.busy or not (self.updated_at or self.error):
            text = "Updating…"
        else:
            left = max(0, round(self.next_poll_at - time.time()))
            if self.error:
                text = f"Error: {self.error[:80]} · retry in {left}s"
            else:
                text = f"Updated {self.updated_at} · next in {left}s"
        self.status_item.title = text

    # actions

    @staticmethod
    def _open(url, _):
        webbrowser.open(url)

    def add_pr(self, _):
        clip = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or ""
        response = rumps.Window(
            message="Paste a PR URL (or owner/repo#123), optionally followed by a short label.",
            title="Watch pull request",
            default_text=clip.strip() if PR_RE.search(clip) else "",
            ok="Add",
            cancel="Cancel",
            dimensions=(360, 24),
        ).run()
        if not response.clicked:
            return
        p = parse_pr(response.text.strip())
        if not p:
            rumps.alert("Not a PR", "Expected e.g. https://github.com/owner/repo/pull/123")
            return
        self._check_config()
        if pr_key(p) in {pr_key(x) for x in self.prs}:
            return
        self.prs.append(p)
        self._save_config()
        self._render()
        self.next_poll_at = 0  # a poll may be running with the old list

    def rename_pr(self, key, _):
        p = next(x for x in self.prs if pr_key(x) == key)
        response = rumps.Window(
            message=f"Label for {key}", title="Rename", default_text=p["label"],
            ok="Save", cancel="Cancel", dimensions=(200, 24),
        ).run()
        # the config may have changed while the dialog was open
        self._check_config()
        p = next((x for x in self.prs if pr_key(x) == key), None)
        if p and response.clicked and response.text.strip():
            p["label"] = response.text.strip()
            self._save_config()
            self._render()

    def remove_pr(self, key, _):
        self._check_config()
        self.config["prs"] = [x for x in self.prs if pr_key(x) != key]
        self.info.pop(key, None)
        self._save_config()
        self._render()

    def remove_finished(self, _):
        self._check_config()
        finished = {pr_key(x) for x in self.prs if self._state(x) in ("merged", "closed")}
        self.config["prs"] = [x for x in self.prs if pr_key(x) not in finished]
        self.info = {k: v for k, v in self.info.items() if k not in finished}
        self._save_config()
        self._render()

    def toggle_labels(self, _):
        self._check_config()
        self.config["show_labels"] = not self.config["show_labels"]
        self._save_config()
        self._render()


# --- CLI ---------------------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(
        prog="pr_watch",
        description="Watches GitHub PRs from the macOS menu bar. Run without a command to start the app; "
        "the commands edit its watchlist, and a running app picks up changes within a second.",
        epilog=f"Watchlist: {CONFIG_PATH}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add", help="watch a PR",
        description="Watch a PR. The label is shown next to its LED; without one, #<number> is used.\n"
        "Adding a PR that is already watched does nothing.",
        epilog="examples:\n  pr_watch add https://github.com/owner/repo/pull/123 auth\n  pr_watch add owner/repo#123",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("pr", metavar="PR", help="PR URL or owner/repo#123")
    add.add_argument("label", nargs="*", metavar="LABEL", help="short label, may contain spaces (default: #<number>)")
    remove = commands.add_parser(
        "remove", help="stop watching PRs",
        description="Stop watching one or more PRs.\nExits with status 1 if any of them isn't watched.",
        epilog='examples:\n  pr_watch remove owner/repo#123\n  pr_watch remove auth "other label"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    remove.add_argument("pr", nargs="+", metavar="PR",
                        help="PR URL, owner/repo#123, or label (quote labels with spaces)")
    commands.add_parser("list", help="show watched PRs",
                        description="Print one line per watched PR: owner/repo#123 and its label.")
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
    prs = config["prs"]

    if args.command == "list":
        for p in prs:
            print(f'{pr_key(p)}  {p["label"]}')
        return

    if args.command == "add":
        p = parse_pr(" ".join([args.pr, *args.label]))
        if not p:
            sys.exit("Not a PR. Expected e.g. https://github.com/owner/repo/pull/123")
        if pr_key(p) in {pr_key(x) for x in prs}:
            print(f"Already watching {pr_key(p)}")
            return
        prs.append(p)
        save_config(config)
        print(f'Watching {pr_key(p)} as {p["label"]}')
        return

    missing = []
    for ref in args.pr:
        p = parse_pr(ref)
        matches = [x for x in prs if (p and pr_key(x) == pr_key(p)) or x["label"] == ref]
        if len(matches) > 1:
            sys.exit(f"{ref} matches several PRs: " + ", ".join(pr_key(x) for x in matches))
        if not matches:
            missing.append(ref)
            continue
        prs.remove(matches[0])
        print(f'Removed {pr_key(matches[0])} ({matches[0]["label"]})')
    if len(missing) < len(args.pr):
        save_config(config)
    if missing:
        sys.exit("Not watched: " + ", ".join(missing))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        PRWatchApp().run()
