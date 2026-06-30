#!/usr/bin/env python3
"""Boot DOS to the DATE prompt via pty, type '1', then dump the first
few bytes of VRAM (0xB8000) to see what COMMAND.COM actually wrote.
"""
import os, pty, time, select, sys, re

EMU = [sys.executable, 'main.py', '--floppy', 'DOS3_3_525/DISK01.IMG', '--interactive']

def read_avail(fd, timeout=0.1):
    out = b''; end = time.time() + timeout
    while time.time() < end:
        r,_,_ = select.select([fd],[],[],max(0,end-time.time()))
        if r:
            try: chunk = os.read(fd, 4096)
            except OSError: break
            if not chunk: break
            out += chunk; end = time.time() + 0.05
        else: break
    return out

# Instead of pty, use a separate snapshot approach: run the emulator with a
# patched main that dumps VRAM after a key injection. Simpler: just inject via
# the emulator's own auto-feed and inspect via a small Python harness using
# the Emulator class directly.
sys.path.insert(0, '.')
from main import Emulator

emu = Emulator(boot_file=None, step_mode=False, floppy_image='DOS3_3_525/DISK01.IMG')
emu.bios.initialize()
if emu.pic: emu.pic.initialize()
emu._setup_ivt_irq_handlers()
buf = bytearray(512); emu.disk.read_sector(0, buf)
for i in range(512): emu.mem.write_byte(0x7C00+i, buf[i])
cpu = emu.cpu; cpu.cs=0; cpu.ip=0x7C00; cpu.ds=0; cpu.es=0; cpu.ss=0; cpu.sp=0x7C00

# Do NOT auto-feed; we'll inject the date keys ourselves once DOS is ready.
emu._install_bios_interrupt_hook()

# Run until the DATE prompt string appears in VRAM.
step = 0; pit = 0; last = None; stuck = 0
ready = False
while step < 9_500_000:
    if not cpu.halted:
        if not cpu.execute(): break
        step += 1
    pit += 1
    if pit >= 500 and emu.pit:
        pit = 0; emu.io.tick(1/18.2)
    if emu.pic: emu._check_and_dispatch_irq()
    if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
        emu.kbd_ctrl.irq_pending = True
        if emu.pic: emu.pic.raise_irq(1)
    # Scan VRAM for "Enter new date" once every ~50k steps
    if step % 50000 == 0:
        vga = bytes(emu.mem.read_byte(0xB8000 + 2*i) for i in range(80*5))
        if b'Enter new date' in vga:
            ready = True
            break
    cur = (cpu.cs<<4)+cpu.ip
    if cur == last:
        stuck += 1
        if stuck > 80000: break
    else: stuck = 0
    last = cur

if not ready:
    print("never reached DATE prompt")
    sys.exit(1)
print(f"[reached DATE prompt at step {step}]")

# Inject the string '01-01-1980\r' one char at a time, stepping between
# each so DOS's INT 16h AH=00 picks them up in order.
for ch in '01-01-1980\r':
    emu.kbd_ctrl.inject_key(ord(ch))
    # step until the key buffer drains
    for _ in range(200000):
        if not cpu.halted:
            if not cpu.execute(): break
            step += 1
        if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
            emu.kbd_ctrl.irq_pending=True
            if emu.pic: emu.pic.raise_irq(1)
        if emu.pic: emu._check_and_dispatch_irq()
        pit += 1
        if pit>=500 and emu.pit: pit=0; emu.io.tick(1/18.2)
        if not (emu.kbd_ctrl.has_data() or emu.kbd.key_pressed()):
            break

# Dump the VGA screen rows so we can see what COMMAND.COM echoed.
print("=== VGA screen after typing '01-01-1980\\r' ===")
for y in range(8, 14):
    row = ''
    for x in range(80):
        ch = emu.mem.read_byte(0xB8000 + (y*80+x)*2)
        attr = emu.mem.read_byte(0xB8000 + (y*80+x)*2 + 1)
        row += chr(ch) if 0x20 <= ch <= 0x7E else f'[{ch:02X}]'
    if row.strip():
        print(f"  row {y:2d}: {row!r}")
