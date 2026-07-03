"""Phase D/E GWBASIC (Tier 2) — currently xfail.

GWBASIC.EXE loads (~243K emulated instructions) and reaches the point of
initialising its environment, then halts the CPU at ``0000:9611`` with IF=0,
having jumped into a data table (the bytes there are a port/address-value
table, not code).  The stack is left in the DOS data segment (ss=0C06), which
indicates a corrupted control-flow return — almost certainly a CPU emulation
gap in a segment-manipulating instruction or an interrupt-return path, the
kind of bug ``snapshot_capture.py`` + ``diff_trace.py`` (Phase F) exist to
localise against Unicorn.

Keeping this as ``strict`` xfail documents the known gap without flaking the
suite; flip it once the diverging instruction is found and fixed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


@pytest.mark.xfail(strict=True, reason='GWBASIC control-flow corruption: '
                                       'halts at 0000:9611 (data table) after '
                                       'load; pending Phase F differential fix')
def test_gwbasic_loads_to_ok_prompt(dos_b):
    """``B:GWBASIC`` reaches the 'Ok' prompt without halting the CPU."""
    h = dos_b
    h.inject_string('B:GWBASIC\r')
    h.wait_for('Ok', max_steps=15_000_000)
    assert 'Ok' in h.vga_str()
    assert not h.cpu.halted
