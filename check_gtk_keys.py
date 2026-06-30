#!/usr/bin/env python3
"""Verify GtkDisplay's key-press handler injects the correct ASCII byte
into the keyboard controller (no scan-code remap), and that Enter yields
0x0D (CR), not 0x0A (LF).  This is the bug we just hit in terminal mode.
"""
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

def synthesize_key(keyval, state=0):
    """Build a Gdk.EventKey and dispatch it as a key-press."""
    ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    ev.keyval = keyval
    ev.state = state
    ev.window = gd.window.get_window()
    ev.time = 0
    gd.window.emit('key-press-event', ev)

# Inject '1'..'0' + Enter and verify the bytes that landed in kbd_ctrl.
import time as _t
received = []
orig_inject = emu.kbd_ctrl.inject_key
def captured_inject(b):
    received.append(b)
    orig_inject(b)
emu.kbd_ctrl.inject_key = captured_inject

# Re-bind the on_key callback to use the patched inject.
gd.on_key = lambda b: emu.kbd_ctrl.inject_key(b)

# Press 1234567890 + Enter
keyvals = [ord('1'), ord('2'), ord('3'), ord('4'), ord('5'),
           ord('6'), ord('7'), ord('8'), ord('9'), ord('0'),
           Gdk.KEY_Return]
for kv in keyvals:
    synthesize_key(kv)
    while Gtk.events_pending(): Gtk.main_iteration_do(False)

expected = [ord('1'), ord('2'), ord('3'), ord('4'), ord('5'),
            ord('6'), ord('7'), ord('8'), ord('9'), ord('0'),
            0x0D]
result = received == expected
gd.close()
print(f'received = {[hex(b) for b in received]}')
print(f'expected = {[hex(b) for b in expected]}')
print(f'RESULT: {"PASS" if result else "FAIL"}')
sys.exit(0 if result else 1)
