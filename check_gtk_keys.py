#!/usr/bin/env python3
"""Verify GtkDisplay routes real set-1 key transitions through the 8042."""
import sys, time
sys.path.insert(0, '.')

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk

from main import Emulator, build_boot_sector

emu = Emulator(gtk=True)
emu.disk.write_boot_sector(build_boot_sector())
emu.bios.initialize()
emu._install_bios_interrupt_hook()
buf = bytearray(512); emu.disk.read_sector(0, buf)
for i in range(512): emu.mem.write_byte(0x7C00 + i, buf[i])
emu.cpu.cs=0; emu.cpu.ip=0x7C00; emu.cpu.ds=0; emu.cpu.es=0; emu.cpu.ss=0; emu.cpu.sp=0x7C00

gd = emu.gtk_display
for _ in range(20):
    while Gtk.events_pending(): Gtk.main_iteration_do(False)
    time.sleep(0.005)

def synthesize_key(event_type, signal, keyval, state=0):
    """Build a Gdk.EventKey and dispatch it to the GTK window."""
    ev = Gdk.Event.new(event_type)
    ev.keyval = keyval
    ev.state = state
    ev.window = gd.window.get_window()
    ev.time = 0
    gd.window.emit(signal, ev)

# Capture the physical scan bytes while still passing them to the controller.
received_scans = []
orig_inject = emu.kbd_ctrl.inject_scan_code
def captured_inject(scan):
    received_scans.append(scan)
    orig_inject(scan)
emu.kbd_ctrl.inject_scan_code = captured_inject

# Press 1234567890 + Enter
keyvals = [ord('1'), ord('2'), ord('3'), ord('4'), ord('5'),
           ord('6'), ord('7'), ord('8'), ord('9'), ord('0'),
           Gdk.KEY_Return]
for kv in keyvals:
    synthesize_key(Gdk.EventType.KEY_PRESS, 'key-press-event', kv)
    synthesize_key(Gdk.EventType.KEY_RELEASE, 'key-release-event', kv)
    while Gtk.events_pending(): Gtk.main_iteration_do(False)

make_scans = [0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
              0x08, 0x09, 0x0A, 0x0B, 0x1C]
expected_scans = [scan for make in make_scans
                  for scan in (make, make | 0x80)]
expected_ascii = [ord(ch) for ch in '1234567890'] + [0x0D]
received_ascii = []
while emu.kbd_ctrl.has_data():
    _scan, ascii_value = emu.kbd_ctrl.read_key_event()
    received_ascii.append(ascii_value)
result = (received_scans == expected_scans
          and received_ascii == expected_ascii)
gd.close()
print(f'scans = {[hex(b) for b in received_scans]}')
print(f'ascii = {[hex(b) for b in received_ascii]}')
print(f'RESULT: {"PASS" if result else "FAIL"}')
sys.exit(0 if result else 1)
