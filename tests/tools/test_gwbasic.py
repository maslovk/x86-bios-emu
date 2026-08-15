"""Phase F GWBASIC (Tier 2) startup coverage.

GWBASIC hooks INT 1Ch and chains to the previous timer callback.  This test
therefore also guards the BIOS invariant that every chainable interrupt has a
valid IRET-compatible default vector.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_gwbasic_loads_to_ok_prompt(dos_b):
    """``B:GWBASIC`` reaches the 'Ok' prompt without halting the CPU."""
    h = dos_b
    h.inject_string('B:GWBASIC\r')
    h.wait_for('Ok', max_steps=15_000_000)
    assert 'Ok' in h.vga_str()
    assert not h.cpu.halted
