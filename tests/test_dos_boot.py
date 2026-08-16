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
        h.wait_for('Enter new date')
        h.emu.video._sync_from_memory()
        prompt = 'Enter new date'
        prompt_cells = None
        for row in h.emu.video.buffer:
            text = ''.join(chr(ch) if 0x20 <= ch <= 0x7E else ' '
                           for ch, _attr in row)
            start = text.find(prompt)
            if start >= 0:
                assert start == 0
                prompt_cells = row[start:start + len(prompt)]
                break
        assert prompt_cells is not None
        assert all(attr & 0x0F for _ch, attr in prompt_cells)
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

    def test_host_folder_bridge_is_visible_as_drive_b(self, tmp_path):
        """A real DOS session can list and read a host-folder file on B:."""
        (tmp_path / 'HOST.TXT').write_bytes(b'Hello from host bridge')
        nested = tmp_path / 'SUBDIR'
        nested.mkdir()
        (nested / 'INNER.TXT').write_bytes(b'Inner host file')
        h = DOSHarness(host_dir=str(tmp_path))
        h.boot_to_prompt()

        listing = h.run_command('DIR B:', max_steps=10_000_000)
        assert 'HOST' in listing.output
        assert 'File not found' not in listing.output

        content = h.run_command('TYPE B:HOST.TXT', max_steps=10_000_000)
        assert 'Hello from host bridge' in content.output

        nested_listing = h.run_command('DIR B:\\SUBDIR', max_steps=10_000_000)
        assert 'INNER' in nested_listing.output
        nested_content = h.run_command('TYPE B:\\SUBDIR\\INNER.TXT',
                                       max_steps=10_000_000)
        assert 'Inner host file' in nested_content.output

    def test_host_folder_bridge_executes_com_program(self, tmp_path):
        """A .COM copied from the host folder executes from drive B:."""
        # COM program at offset 0100h: print the string at 0109h, then exit.
        program = bytes.fromhex('BA0901B409CD21CD20') + b'HOST COM OK$'
        (tmp_path / 'HELLO.COM').write_bytes(program)
        h = DOSHarness(host_dir=str(tmp_path))
        h.boot_to_prompt()

        result = h.run_command('B:HELLO.COM', max_steps=10_000_000)
        assert 'HOST COM OK' in result.output

    def test_host_folder_bridge_rejects_guest_writes(self, tmp_path):
        """DOS writes to the host bridge fail and never create host files."""
        (tmp_path / 'ORIGINAL.TXT').write_bytes(b'original')
        h = DOSHarness(host_dir=str(tmp_path))
        h.boot_to_prompt()

        result = h.run_command('COPY COMMAND.COM B:', max_steps=2_000_000,
                               probe_errorlevel=False)
        assert ('Write protect' in result.output or
                'Access denied' in result.output or
                'Error' in result.output)
        assert not (tmp_path / 'COMMAND.COM').exists()
        assert (tmp_path / 'ORIGINAL.TXT').read_bytes() == b'original'
