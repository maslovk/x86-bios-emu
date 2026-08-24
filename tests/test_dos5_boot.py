"""Integration tests for the MS-DOS 5.00 boot from DOS5/Disk01.img.

MS-DOS 5.00 is distributed as an interactive Setup boot diskette (720 KB
3.5", media 0xF9).  Booting it exercises two emulator contracts that older
DOS versions did not:

* INT 13h geometry: 0xF9 is ambiguous (1.44 MB 18-spt and 720 KB 9-spt both
  use it), so the boot sector's own 9-spt CHS arithmetic only works when the
  image's geometry is pinned from its sector count.
* INT 16h semantics: the Setup UI drains the type-ahead buffer (AH=01/AH=00)
  before issuing the real blocking read (AH=00/AH=10h), so AH=00 must wait
  rather than return a phantom NUL key.

Slow: each test boots MS-DOS 5.00.  Run with:
    pytest tests/test_dos5_boot.py -v -m slow
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dosharness import DOSHarness, REPO_ROOT

DOS5_DISK1 = os.path.join(REPO_ROOT, 'DOS5', 'Disk01.img')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.exists(DOS5_DISK1),
                       reason='DOS5/Disk01.img not present'),
]


class TestDOS5Boot:
    """MS-DOS 5.00 boot from the historical Setup system disk."""

    def boot_to_welcome(self):
        h = DOSHarness(image_path=DOS5_DISK1)
        h.wait_for('Welcome to Setup', max_steps=6_000_000)
        return h

    def test_boot_reaches_setup_welcome(self):
        h = self.boot_to_welcome()
        screen = h.vga_str()
        assert 'MS-DOS' in screen
        assert 'Version 5.00' in screen
        assert 'Welcome to Setup' in screen
        assert 'To continue Setup, press ENTER' in screen

    def test_enter_advances_to_configuration(self):
        h = self.boot_to_welcome()
        target = 'Setup has determined'
        h.inject_background(
            '\r', interval=0.05, repeat=30,
            stop_when_absent='Welcome to Setup')
        h.wait_for(target, max_steps=6_000_000)
        assert target in h.vga_str()

    def test_setup_flows_to_install_screen(self):
        """Drive Setup past the configuration phase into the install setup.

        The second ENTER must be injected from a background thread: Setup
        drains the type-ahead buffer before its blocking read, so a key
        queued ahead of the drain is discarded (see
        DOSHarness.inject_background).
        """
        h = self.boot_to_welcome()
        config_target = 'Setup has determined'
        h.inject_background(
            '\r', interval=0.05, repeat=30,
            stop_when_absent='Welcome to Setup')
        h.wait_for(config_target, max_steps=6_000_000)
        install_target = 'now being set up'
        h.inject_background(
            '\r', interval=0.05, repeat=30,
            stop_when=install_target)
        h.wait_for(install_target, max_steps=10_000_000)
        screen = h.vga_str()
        assert 'now being set up' in screen
        assert 'Bad or missing Command Interpreter' not in screen
