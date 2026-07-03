"""Phase D tool test: DEBUG.COM — assemble, trace (-T), dump, enter, regs, quit.

The single most demanding consumer of CPU correctness: exercises the
assembler, the trap-flag single-step (INT 1 -> DEBUG's hook), and the
software-interrupt dispatch that now transfers INT 1/3 to an app-hooked
IVT entry.  DEBUG runs from B: (DISK02).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _bottom_line(h):
    lines = [ln for ln in h.vga_text() if ln.strip()]
    return lines[-1].rstrip() if lines else ''


def _wait_dash(h, max_steps=2_000_000):
    """Run until DEBUG's '-' prompt is the bottom line."""
    step = 0
    last_ip = None
    stuck = 0
    while step < max_steps:
        if not h.cpu.halted:
            if not h.cpu.execute():
                return False
            step += 1
        if step % 1500 == 0 and _bottom_line(h) == '-':
            return True
        if step % 500 == 0 and h.emu.pit:
            h.emu.io.tick(1.0 / 18.2)
        h._pump()
        cur = (h.cpu.cs << 4) + h.cpu.ip
        if cur == last_ip:
            stuck += 1
            if stuck > 500000:
                return False
        else:
            stuck = 0
        last_ip = cur
    return False


def _dbg(h, cmd, settle=0):
    h.inject_string(cmd + '\r')
    return _wait_dash(h)


def test_debug_asm_trace_dump_enter_quit(dos_b):
    h = dos_b
    # Load DEBUG from B: -> '-' prompt.
    h.inject_string('B:DEBUG\r')
    assert _wait_dash(h), 'DEBUG did not reach the - prompt'

    # -R register dump.
    assert _dbg(h, 'R')
    assert 'AX=' in h.vga_str() and 'IP=' in h.vga_str()

    # -D dump, -E enter bytes, -D verify them.
    assert _dbg(h, 'D 100 10F')
    assert _dbg(h, 'E 100 41 42')
    assert _dbg(h, 'D 100 102')
    assert '41 42' in h.vga_str()

    # Assemble MOV AX,1234 at CS:0100, then single-step it (-T).
    h.inject_string('A 100\r')
    h.run_steps(80000)
    h.inject_string('MOV AX,1234\r')
    h.run_steps(60000)
    h.inject_string('\r')          # blank line ends assemble mode
    assert _wait_dash(h)
    assert _dbg(h, 'T')            # trace one instruction -> AX=1234
    assert 'AX=1234' in h.vga_str()

    # -Q returns to the A> prompt (not '-', so don't use _dbg here).
    h.inject_string('Q\r')
    h.wait_for('A>', max_steps=2_000_000)
    assert 'A>' in h.vga_str()
