"""Integration tests for DOS 3.3 boot and command execution.

These boot real MS-DOS 3.3 from the DISK01.IMG floppy image, drive the
keyboard via kbd_ctrl, and assert on VGA text output.  Slow (each test
boots DOS); kept separate from the fast unit tests.

Run with:  pytest tests/test_dos_boot.py -v

The :class:`DOSHarness` lives in ``dosharness.py`` at the repo root so the
per-tool suite under ``tests/tools/`` can reuse it.
"""
import sys
import os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dosharness import DOSHarness


@pytest.mark.slow
class TestDOSBoot:
    """Slow integration tests — boots real MS-DOS 3.3."""

    def test_boot_reaches_ms_dos_banner(self):
        """DOS boots and prints the 'Microsoft MS-DOS Version 3.30' banner."""
        h = DOSHarness()
        h.boot_to_prompt()  # boot fully to A>
        screen = h.vga_str()
        assert 'MS-DOS' in screen
        assert 'Version 3.30' in screen

    def test_boot_reaches_a_prompt(self):
        """DOS reaches the A> prompt after DATE/TIME."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.vga_str()
        assert 'A>' in screen

    def test_echo_command(self):
        """ECHO prints its argument (internal command, no disk I/O)."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('ECHO TestPassed')
        assert 'TestPassed' in screen

    def test_dir_shows_volume_header(self):
        """DIR lists files from the floppy (FCB search + REPE CMPSB).
        With 34 files, the 'Volume in drive A' header scrolls off the
        25-row VGA screen, so we check for file entries and the file count."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('DIR', max_steps=10_000_000)
        # Should show file listings, not 'File not found'
        assert 'COM' in screen or 'SYS' in screen or 'EXE' in screen
        assert 'File not found' not in screen
        assert 'File(s)' in screen  # '34 File(s) ... bytes free'

    def test_bad_command_message(self):
        """An unknown command gives 'Bad command or file name'."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('ZZZXYZ')
        assert 'Bad command' in screen or 'File not found' in screen
