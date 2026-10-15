"""A notification-style banner in the top-right corner that runs a callback when clicked.

macOS only lets signed apps post notifications of their own (UNUserNotificationCenter answers "Notifications are
not allowed for this application" to the ad-hoc signed Python.app), and clicking an osascript notification opens
Script Editor. So the app draws its own. It isn't kept in Notification Center and doesn't follow Focus.
"""
import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSEdgeInsetsMake,
    NSFont,
    NSFontWeightSemibold,
    NSImage,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSPanel,
    NSScreen,
    NSSound,
    NSStatusWindowLevel,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialPopover,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSMakeSize
from PyObjCTools import AppHelper

WIDTH = 344
PADDING = 14
RADIUS = 16
MARGIN = 12  # from the screen edges, and between banners
SECONDS = 8  # until it goes away, unless the mouse is on it

shown = []  # the banners on screen, top to bottom


class BannerView(NSView):
    @objc.python_method
    def setup(self, title, message, on_click):
        self.on_click = on_click
        self.timer = 0  # bumped to cancel the pending dismissal
        text_width = WIDTH - 2 * PADDING
        self.title_label = self._label(title, NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold),
                                       NSColor.labelColor(), text_width, 1)
        self.message_label = self._label(message, NSFont.systemFontOfSize_(12), NSColor.secondaryLabelColor(),
                                         text_width, 3)
        title_height = self.title_label.frame().size.height
        message_height = self.message_label.frame().size.height if message else 0
        height = PADDING + title_height + (2 + message_height if message else 0) + PADDING
        self.setFrame_(NSMakeRect(0, 0, WIDTH, height))
        self.title_label.setFrameOrigin_((PADDING, height - PADDING - title_height))
        self.message_label.setFrameOrigin_((PADDING, PADDING))
        self.message_label.setHidden_(not message)

        # the close button macOS shows on a notification's corner while the mouse is on it
        self.close_button = NSButton.buttonWithImage_target_action_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_("xmark.circle.fill", "Close"),
            self, "closeClicked:")
        self.close_button.setBordered_(False)
        self.close_button.setContentTintColor_(NSColor.secondaryLabelColor())
        self.close_button.setFrame_(NSMakeRect(2, height - 20, 18, 18))
        self.close_button.setHidden_(True)
        self.addSubview_(self.close_button)

        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        ))

    @objc.python_method
    def _label(self, text, font, color, width, lines):
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFont_(font)
        label.setTextColor_(color)
        label.setMaximumNumberOfLines_(lines)
        label.cell().setLineBreakMode_(NSLineBreakByTruncatingTail if lines == 1 else NSLineBreakByWordWrapping)
        label.cell().setTruncatesLastVisibleLine_(True)
        label.setPreferredMaxLayoutWidth_(width)
        label.setFrameSize_(NSMakeSize(width, label.fittingSize().height))
        self.addSubview_(label)
        return label

    def acceptsFirstMouse_(self, event):
        return True  # the click counts, though the app isn't active

    def hitTest_(self, point):
        # the labels would take the click otherwise
        hit = objc.super(BannerView, self).hitTest_(point)
        return hit if hit is self.close_button else (self if hit is not None else None)

    def mouseDown_(self, event):
        pass  # acted on at mouse up, like a button

    def mouseUp_(self, event):
        if self.mouse_inRect_(self.convertPoint_fromView_(event.locationInWindow(), None), self.bounds()):
            close(self)
            self.on_click()

    def mouseEntered_(self, event):
        self.close_button.setHidden_(False)
        self.timer += 1

    def mouseExited_(self, event):
        self.close_button.setHidden_(True)
        self.dismiss_later()

    def closeClicked_(self, sender):
        close(self)

    @objc.python_method
    def dismiss_later(self):
        self.timer += 1
        AppHelper.callLater(SECONDS, self._expire, self.timer)

    @objc.python_method
    def _expire(self, timer):
        if timer == self.timer:
            close(self)


def rounded_mask(radius):
    size = 2 * radius + 1

    def draw(rect):
        NSColor.blackColor().set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(size, size), False, draw)
    image.setCapInsets_(NSEdgeInsetsMake(radius, radius, radius, radius))
    return image


def show(title, message, on_click):
    """Shows a banner below the ones already up, with the Glass sound. on_click() runs when it's clicked."""
    view = BannerView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 0))
    view.setup(title, message, on_click)
    size = view.frame().size

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, size.width, size.height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False,
    )
    panel.setReleasedWhenClosed_(False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setLevel_(NSStatusWindowLevel)
    panel.setHidesOnDeactivate_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)

    background = NSVisualEffectView.alloc().initWithFrame_(view.frame())
    background.setMaterial_(NSVisualEffectMaterialPopover)
    background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    background.setState_(NSVisualEffectStateActive)
    background.setMaskImage_(rounded_mask(RADIUS))
    background.addSubview_(view)
    panel.setContentView_(background)

    shown.append(view)
    _place()
    panel.setAlphaValue_(0)
    panel.orderFrontRegardless()
    panel.animator().setAlphaValue_(1)
    NSSound.soundNamed_("Glass").play()
    view.dismiss_later()


def close(view):
    if view not in shown:
        return
    shown.remove(view)
    panel = view.window()
    panel.animator().setAlphaValue_(0)
    AppHelper.callLater(0.3, panel.orderOut_, None)
    _place()


def _place():
    """Stacks the banners down from the top-right corner of the screen with the menu bar."""
    area = NSScreen.screens()[0].visibleFrame()
    top = area.origin.y + area.size.height - MARGIN
    for view in shown:
        frame = view.window().frame()
        top -= frame.size.height
        view.window().setFrameOrigin_((area.origin.x + area.size.width - frame.size.width - MARGIN, top))
        top -= MARGIN
