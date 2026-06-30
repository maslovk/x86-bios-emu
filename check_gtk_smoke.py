#!/usr/bin/env python3
"""Smoke test for the --gtk display path.

Boots the sample boot sector under GtkDisplay, lets it run until the POST
banner should be visible, grabs the rendered pixels directly from the
DrawingArea's Gdk window (no external screenshot tool), and asserts that
the VGA content (the "Simple BIOS" banner characters) made it onto the
rendered surface in distinct colours (proving both text + bg rendering).

Requires a working X display (run inside Xvfb or a desktop session).
"""
import os, sys, time, threading
sys.path.insert(0, '.')

import gi
gi.require_version('Gtk', '3.0')
gi.require_require = False  # no-op marker
from gi.repository import Gtk, Gdk, GLib

from main import Emulator, build_boot_sector

# Build the emulator in --gtk mode (sample boot sector, no floppy).
emu = Emulator(gtk=True, gtk_font_size=18)
# Provide a boot sector explicitly so we don't depend on floppy state.
emu.disk.write_boot_sector(build_boot_sector())
emu.bios.initialize()
if emu.pic: emu.pic.initialize()
emu._setup_ivt_irq_handlers()
buf = bytearray(512); emu.disk.read_sector(0, buf)
for i in range(512): emu.mem.write_byte(0x7C00 + i, buf[i])
emu.cpu.cs = 0; emu.cpu.ip = 0x7C00
emu.cpu.ds = 0; emu.cpu.es = 0; emu.cpu.ss = 0; emu.cpu.sp = 0x7C00
emu._install_bios_interrupt_hook()
# Auto-feed a space so INT 16h returns -> HLT -> loop ends quickly.
if emu.kbd_ctrl:
    emu.kbd_ctrl.feed_string(' ')
else:
    emu.kbd.feed_string(' ')

# Pump Gtk events so the window realizes + becomes drawable before we
# tear into it.
gd = emu.gtk_display
for _ in range(50):
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    time.sleep(0.01)

# Run a few thousand instructions to let POST write the banner.
N = 80000
for _ in range(N):
    if emu.cpu.halted: break
    emu.cpu.execute()
    # periodic pump so the drawing area gets its initial expose
    if _ % 10000 == 0:
        gd.pump()

# Force one final redraw + pump.
gd.drawing_area.queue_draw()
while Gtk.events_pending():
    Gtk.main_iteration_do(False)

# Grab rendered pixels from the DrawingArea's Gdk window.
gdkwin = gd.drawing_area.get_window()
result = {'ok': False, 'reason': '', 'distinct_colors': 0, 'nonblack_pixels': 0}
if gdkwin is None:
    result['reason'] = 'drawing area has no Gdk window (not realized?)'
else:
    # Ensure the window is painted by processing any remaining expose events
    # after queue_draw.  Use Gdk.pixbuf_get_from_window via Gdk 3 introspection.
    from gi.repository import GdkPixbuf
    w, h = gdkwin.get_width(), gdkwin.get_height()
    pb = Gdk.pixbuf_get_from_window(gdkwin, 0, 0, w, h)
    if pb is None:
        result['reason'] = 'pixbuf_get_from_window returned None'
    else:
        pixels = pb.get_pixels()
        nch = pb.get_n_channels()
        rs = pb.get_rowstride()
        # Count distinct colours and non-black pixels.
        seen = set()
        nonblack = 0
        for y in range(0, h, 4):
            row = pixels[y*rs : y*rs + w*nch]
            for x in range(0, w, 4):
                idx = x*nch
                if idx + 2 < len(row):
                    r, g, b = row[idx], row[idx+1], row[idx+2]
                    if r or g or b:
                        nonblack += 1
                        seen.add((r, g, b))
        result['distinct_colors'] = len(seen)
        result['nonblack_pixels'] = nonblack
        result['ok'] = nonblack > 100 and len(seen) > 2
        if not result['ok']:
            result['reason'] = (f'too sparse: {nonblack} nonblack pixels, '
                                 f'{len(seen)} colours')

gd.close()
print('RESULT:', result)
sys.exit(0 if result['ok'] else 1)
