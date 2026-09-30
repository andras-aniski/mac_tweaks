"""Screenshot a rumps menu bar app on a solid background.

Runs the app in-process, puts a dark panel behind the menu bar area it captures (so the
wallpaper and other windows stay out of the shot), takes one screenshot, then quits.

    <app>/.venv/bin/python scripts/shoot.py APP_DIR MODULE:CLASS OUT.png [options]

Options:
    --menu          capture with the menu open (default: just the menu bar item)
    --setup CODE    Python run before the app is created; the app module is `m`
    --exec CODE     Python run once the app is up; the app instance is `app`
    --wait SECONDS  delay after --exec before capturing (default 1)

The output is at Retina 2x; show it at half width in READMEs.
Needs Screen Recording permission for the terminal.
"""
import argparse
import importlib
import os
import subprocess
import sys
import threading
import time

import rumps
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSPanel,
    NSScreen,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSRunLoopCommonModes
from PyObjCTools import AppHelper

BACKGROUND = (0.12, 0.12, 0.14)
MARGIN = 4  # around the menu bar item; more would catch the neighbouring icons
MENU_MARGIN = 20


class Backdrop(NSPanel):
    def constrainFrameRect_toScreen_(self, rect, screen):
        return rect  # otherwise macOS pushes the panel below the menu bar


def primary_height():
    return NSScreen.screens()[0].frame().size.height


def show_backdrop(x, top, w, h):
    """Region in screencapture coordinates: global points, origin at the primary screen's top left."""
    panel = Backdrop.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, primary_height() - top - h, w, h),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*BACKGROUND, 1))
    panel.setLevel_(23)  # just below the menu bar (24)
    panel.setIgnoresMouseEvents_(True)
    panel.setHasShadow_(False)
    panel.orderFrontRegardless()
    return panel


def capture(out, x, top, w, h):
    subprocess.run(["screencapture", "-x", "-R", f"{x},{top},{w},{h}", out], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app_dir")
    parser.add_argument("target", metavar="MODULE:CLASS")
    parser.add_argument("out")
    parser.add_argument("--menu", action="store_true")
    parser.add_argument("--setup", default="")
    parser.add_argument("--exec", dest="exec_code", default="")
    parser.add_argument("--wait", type=float, default=1.0)
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    module_name, class_name = args.target.split(":")
    os.chdir(args.app_dir)
    sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    exec(args.setup, {"m": module})
    app = getattr(module, class_name)()

    def shoot():
        status_item = app._nsapp.nsstatusitem
        button = status_item.button()
        window = button.window()
        frame = window.frame()
        screen = window.screen()
        screen_top = primary_height() - (screen.frame().origin.y + screen.frame().size.height)
        visible = screen.visibleFrame()
        bar_height = screen.frame().origin.y + screen.frame().size.height - (visible.origin.y + visible.size.height)
        x, w = frame.origin.x, frame.size.width
        right_of_notch = screen.auxiliaryTopRightArea()
        if right_of_notch and x < right_of_notch.origin.x:
            print("error: the menu bar item is hidden behind the notch; quit some menu bar apps and retry",
                  file=sys.stderr, flush=True)
            os._exit(1)
        print(f"capturing on a {screen.backingScaleFactor():g}x screen", flush=True)

        if not args.menu:
            region = (x - MARGIN, screen_top, w + 2 * MARGIN, bar_height)
            app._backdrop = show_backdrop(*region)

            def capture_then_quit():
                capture(out, *region)
                rumps.quit_application()

            AppHelper.callLater(0.5, capture_then_quit)  # let the panel draw first
            return

        menu = status_item.menu()
        size = menu.size()
        region = (x - MENU_MARGIN, screen_top, max(size.width, w) + 2 * MENU_MARGIN, bar_height + size.height + MENU_MARGIN)
        app._backdrop = show_backdrop(*region)

        def capture_then_close():
            time.sleep(1.5)
            capture(out, *region)
            menu.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
                "cancelTracking", None, False, [NSRunLoopCommonModes]
            )

        threading.Thread(target=capture_then_close, daemon=True).start()
        button.performClick_(None)  # blocks while the menu is open
        rumps.quit_application()

    def start():
        exec(args.exec_code, {"app": app, "m": module})
        AppHelper.callLater(args.wait, shoot)

    rumps.events.before_start.register(lambda: AppHelper.callLater(0.5, start))
    app.run()


if __name__ == "__main__":
    main()
