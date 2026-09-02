#!/usr/bin/env python3
"""DPMI (Ergo/Borland 286) host investigation probe.

Boots MS-DOS 6.22 from the prepared hard disk with the TASM tools
mounted as D: and runs DPMIINST.EXE (the interactive installer for
DPMI16BI.OVL), then reports how far the host got:

  * INT 15h/21h traffic, port I/O, LMSW (protected-mode entry),
    reset pulses, and the final CS:IP / halt state
  * whether the host module's code materialised in memory

Usage: python3 probe_dpmi.py [command]   (default: DPMIINST.EXE)

Status (2026-09): the host's real-mode prologue runs — banner, interrupt
reflector install, timer-interrupt chain — but stalls inside DPMIINST's
long machine-probe scan and never reaches LMSW.  Known-good adjacent
pieces: A20 gate, 8 MiB backing, warm-reset continuation, PM-aware INT
routing, and 286 PUSHF/POPF CPU identity all pass their unit tests.
"""
import collections
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from dosharness import DOSHarness  # noqa: E402

TASM_DIR = 'DOS_sources/TASM'
HDD = 'dos622-new.hdd'
DOS6_DISK = 'DOS6_22/Disk1.img'


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'DPMIINST.EXE'
    h = DOSHarness(image_path=DOS6_DISK, hard_disk=HDD, boot_drive=0x80,
                   writable=True, host_mounts={'D': TASM_DIR},
                   host_dir_write=True, cpu_backend='python')
    cpu, emu = h.cpu, h.emu

    events = collections.Counter()
    ports_out = collections.Counter()
    last_lmsw = []
    orig_int = emu.bios.handle_interrupt
    orig_out = emu.io.outb
    orig_msw = cpu._set_msw

    def hi(c, n):
        ah = (c.ax >> 8) & 0xFF
        events[f'INT {n:02X}h (AH={ah:02X})'] += 1
        return orig_int(c, n)

    def outb(port, val):
        if port in (0x64, 0x60, 0x92, 0x70, 0x71):
            ports_out[(port, val)] += 1
        return orig_out(port, val)

    def msw(value):
        last_lmsw.append((value, cpu.cs, cpu.ip))
        return orig_msw(value)

    emu.bios.handle_interrupt = hi
    emu.io.outb = outb
    cpu._set_msw = msw

    h.run_command('D:', max_steps=30_000_000)
    result = h.run_command(command, max_steps=3_000_000)

    print(f'=== {command} ===')
    print(str(result)[-400:])
    print('--- key traffic ---')
    for name, count in events.most_common(8):
        print(f'  {name}: {count}')
    for (port, val), count in ports_out.most_common(6):
        print(f'  OUT {port:#04x} <- {val:#04x}: {count}')
    print(f'  LMSW calls: {len(last_lmsw)}', last_lmsw[-3:])
    print(f'  reset pulses: {emu.reset_requests}')
    print(f'  final: {"HALTED" if cpu.halted else "running"} at '
          f'{cpu.cs:04X}:{cpu.ip:04X} pm={cpu._pm}')
    # Did the host module's code survive in memory?
    data = open(os.path.join(TASM_DIR, 'DPMI16BI.OVL'), 'rb').read()
    needle = data[0x200 + 0x3264:0x200 + 0x3274]
    found = bytes(emu.mem.ram).find(needle)
    print(f'  module code @0x3264 present in RAM: '
          f'{"yes @ " + hex(found) if found >= 0 else "NO"}')


if __name__ == '__main__':
    main()
