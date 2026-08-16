"""Integration tests for the MS-DOS 4.00 boot from DOS4/OPERATI3.IMG.

DOS 4.00's SYSINIT probes the INT 11h equipment word for bit 0 (floppy
drives installed); with the bit clear it fakes drives A:/B: by zeroing the
CDS DPB pointers, and every path open then fails with "path not found"
("Bad or missing Command Interpreter").  These tests pin the boot contract
on the historical OEM image kept in ``DOS4/``.

Slow: each test boots MS-DOS 4.00.  Run with:
    pytest tests/test_dos4_boot.py -v -m slow
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dosharness import DOSHarness, REPO_ROOT

DOS4_OPERATI3 = os.path.join(REPO_ROOT, 'DOS4', 'OPERATI3.IMG')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(DOS4_OPERATI3),
                       reason='DOS4/OPERATI3.IMG not present'),
]


class TestDOS4Boot:
    """MS-DOS 4.00 boot from the historical OPERATI3 system disk."""

    def boot_to_prompt(self):
        h = DOSHarness(image_path=DOS4_OPERATI3)
        h.wait_for('Enter new date')
        h.inject_string('\r')
        h.wait_for('Enter new time')
        h.inject_string('\r')
        h.wait_for('A>')
        return h

    def test_boot_reaches_a_prompt(self):
        h = self.boot_to_prompt()
        screen = h.vga_str()
        assert 'MS-DOS' in screen
        assert 'Version 4.00' in screen
        assert 'Bad or missing Command Interpreter' not in screen
        assert 'A>' in screen

    def test_dir_lists_system_disk(self):
        h = self.boot_to_prompt()
        result = h.run_command('DIR', max_steps=12_000_000)
        # OPERATI3 root: IO.SYS, MSDOS.SYS, COMMAND.COM plus drivers.
        assert 'COMMAND' in result.output
        assert 'File(s)' in result.output
        assert 'File not found' not in result.output

    def test_echo_and_bad_command(self):
        h = self.boot_to_prompt()
        assert 'TestPassed' in h.run_command('ECHO TestPassed')
        bad = h.run_command('ZZZXYZ')
        assert 'Bad command' in bad.output

    def test_external_program_runs_from_disk(self):
        """GRAFTABL.COM (present on OPERATI3) loads and executes."""
        h = self.boot_to_prompt()
        result = h.run_command('GRAFTABL', max_steps=12_000_000)
        # DOS 4 GRAFTABL reports "Graphics Characters Loaded" (or already
        # loaded) once the program runs; a failed load says "Bad command".
        assert 'Bad command' not in result.output
