#!/usr/bin/env python3
"""Generate menu bar template icons for FocusTracker."""

from AppKit import (
    NSImage, NSBitmapImageRep, NSGraphicsContext,
    NSCompositingOperationSourceOver, NSFontAttributeName,
    NSForegroundColorAttributeName, NSFont, NSColor, NSString,
    NSMakeRect, NSMakeSize, NSMakePoint, NSBezierPath,
    NSPNGFileType,
)
from Foundation import NSDictionary
import math
import os

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
os.makedirs(ICON_DIR, exist_ok=True)

SIZE = 22  # standard menu bar size
RETINA = 44  # @2x


def create_icon(filename, draw_func, scale=1):
    """Create a template image PNG."""
    s = SIZE * scale
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, s, s, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.setCurrentContext_(ctx)

    if scale > 1:
        ctx.CGContext()

    draw_func(s)

    ctx.flushGraphics()

    data = rep.representationUsingType_properties_(NSPNGFileType, None)
    path = os.path.join(ICON_DIR, filename)
    data.writeToFile_atomically_(path, True)
    print(f"  Created {path}")


def draw_bolt(s):
    """Lightning bolt — energy/focus."""
    NSColor.blackColor().set()
    path = NSBezierPath.bezierPath()

    # Scale relative to icon size
    cx, cy = s * 0.5, s * 0.5
    u = s / 22.0  # unit scale

    # Lightning bolt shape
    path.moveToPoint_(NSMakePoint(cx + 1*u, cy - 9*u))
    path.lineToPoint_(NSMakePoint(cx - 3*u, cy + 1*u))
    path.lineToPoint_(NSMakePoint(cx + 0*u, cy + 1*u))
    path.lineToPoint_(NSMakePoint(cx - 1*u, cy + 9*u))
    path.lineToPoint_(NSMakePoint(cx + 3*u, cy - 1*u))
    path.lineToPoint_(NSMakePoint(cx + 0*u, cy - 1*u))
    path.closePath()
    path.fill()


def draw_target(s):
    """Concentric circles — focus/target."""
    NSColor.blackColor().set()
    cx, cy = s * 0.5, s * 0.5
    u = s / 22.0

    # Outer ring
    r1 = NSMakeRect(cx - 9*u, cy - 9*u, 18*u, 18*u)
    p1 = NSBezierPath.bezierPathWithOvalInRect_(r1)
    p1.setLineWidth_(1.5 * u)
    p1.stroke()

    # Inner ring
    r2 = NSMakeRect(cx - 5*u, cy - 5*u, 10*u, 10*u)
    p2 = NSBezierPath.bezierPathWithOvalInRect_(r2)
    p2.setLineWidth_(1.5 * u)
    p2.stroke()

    # Center dot
    r3 = NSMakeRect(cx - 2*u, cy - 2*u, 4*u, 4*u)
    NSBezierPath.bezierPathWithOvalInRect_(r3).fill()


def draw_flame(s):
    """Stylized flame — streak/fire."""
    NSColor.blackColor().set()
    path = NSBezierPath.bezierPath()
    cx, cy = s * 0.5, s * 0.5
    u = s / 22.0

    # Flame shape using curves
    path.moveToPoint_(NSMakePoint(cx, cy - 9*u))
    path.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(cx + 6*u, cy + 2*u),
        NSMakePoint(cx + 7*u, cy - 6*u),
        NSMakePoint(cx + 8*u, cy - 1*u),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(cx, cy + 9*u),
        NSMakePoint(cx + 5*u, cy + 6*u),
        NSMakePoint(cx + 2*u, cy + 9*u),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(cx - 6*u, cy + 2*u),
        NSMakePoint(cx - 2*u, cy + 9*u),
        NSMakePoint(cx - 5*u, cy + 6*u),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(cx, cy - 9*u),
        NSMakePoint(cx - 8*u, cy - 1*u),
        NSMakePoint(cx - 7*u, cy - 6*u),
    )
    path.fill()

    # Solid flame — no cutout needed, looks cleaner at 22px


def draw_timer(s):
    """Stopwatch — clean timer icon."""
    NSColor.blackColor().set()
    cx, cy = s * 0.5, s * 0.52
    u = s / 22.0

    # Main circle
    r = 7.5 * u
    circle = NSMakeRect(cx - r, cy - r, r*2, r*2)
    p = NSBezierPath.bezierPathWithOvalInRect_(circle)
    p.setLineWidth_(1.6 * u)
    p.stroke()

    # Top button (stem)
    stem = NSBezierPath.bezierPath()
    stem.moveToPoint_(NSMakePoint(cx, cy + r))
    stem.lineToPoint_(NSMakePoint(cx, cy + r + 2.5*u))
    stem.setLineWidth_(1.8 * u)
    stem.setLineCapStyle_(1)  # round
    stem.stroke()

    # Small top bar
    bar = NSBezierPath.bezierPath()
    bar.moveToPoint_(NSMakePoint(cx - 2*u, cy + r + 2.5*u))
    bar.lineToPoint_(NSMakePoint(cx + 2*u, cy + r + 2.5*u))
    bar.setLineWidth_(1.4 * u)
    bar.setLineCapStyle_(1)
    bar.stroke()

    # Minute hand
    hand = NSBezierPath.bezierPath()
    hand.moveToPoint_(NSMakePoint(cx, cy))
    angle = math.radians(60)
    hand.lineToPoint_(NSMakePoint(cx + math.sin(angle) * 4.5*u, cy + math.cos(angle) * 4.5*u))
    hand.setLineWidth_(1.5 * u)
    hand.setLineCapStyle_(1)
    hand.stroke()

    # Center dot
    dot = NSMakeRect(cx - 1*u, cy - 1*u, 2*u, 2*u)
    NSBezierPath.bezierPathWithOvalInRect_(dot).fill()


if __name__ == "__main__":
    print("Generating FocusTracker icons...")

    for scale, suffix in [(1, ""), (2, "@2x")]:
        create_icon(f"bolt{suffix}.png", draw_bolt, scale)
        create_icon(f"target{suffix}.png", draw_target, scale)
        create_icon(f"flame{suffix}.png", draw_flame, scale)
        create_icon(f"timer{suffix}.png", draw_timer, scale)

    print("\nDone! Icons in ./icons/")
    print("Set in menubar.py: self.icon = 'icons/bolt'  (without .png)")
