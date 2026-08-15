"""Phase E TSR / device driver tools (Tier 2): load-without-crash + functional probes.

Each DOS 3.3 TSR loads resident and returns to the A> prompt.  PRINT remains
resident as expected for a spooler, but returns control to COMMAND.COM and the
system keeps responding.  The bar is "loads without crashing the emulator and
a follow-up command still works".

functional probes:
  * SUBST E: A:\\ then DIR E: lists drive A's files.
  * MODE configures COM1 for 9600 baud.
  * CTTY COM1 exchanges a command over serial and returns to CON.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]

# Tools that load resident and return to the prompt cleanly.  Loaded in one
# session on a fresh writable image (small TSR footprints, ~600 KB free RAM).
_RETURNING_TSRS = [
    'SUBST E: A:\\',
    'FASTOPEN A:=50',
    'APPEND /E',
    'ASSIGN',
    'JOIN',
    'GRAFTABL',
    'SHARE',
    'MODE',
]


def test_tsrs_load_without_crash(dos_rw):
    """Each resident tool returns to the prompt; a follow-up command still works."""
    for cmd in _RETURNING_TSRS:
        r = dos_rw.run_command(cmd, max_steps=4_000_000, probe_errorlevel=False)
        assert not r.timed_out, f'{cmd!r} did not return to the prompt'
    # The system must not be corrupted: a plain command still runs.
    ok = dos_rw.run_command('ECHO alive', max_steps=2_000_000,
                            probe_errorlevel=False)
    assert not ok.timed_out
    assert 'alive' in ok.output


def test_subst_functional(dos_rw):
    """SUBST E: A:\\ makes DIR E: list drive A's files."""
    r = dos_rw.run_command('SUBST E: A:\\', max_steps=3_000_000,
                           probe_errorlevel=False)
    assert not r.timed_out
    r = dos_rw.run_command('DIR E:', max_steps=4_000_000,
                           probe_errorlevel=False)
    assert not r.timed_out
    assert 'File(s)' in r.output


def test_print_resident_keeps_system_alive(dos_rw):
    """PRINT installs resident; after naming the list device the prompt returns.

    PRINT's first run prompts 'Name of list device [PRN]:'; answering it
    installs the spooler resident and returns to A>.
    """
    r = dos_rw.run_dialog('PRINT',
                          [('Name of list device', 'PRN\r')],
                          max_steps=4_000_000, probe_errorlevel=False)
    assert not r.timed_out
    # The system must still process commands (no CPU halt / emulator crash).
    ok = dos_rw.run_command('ECHO alive', max_steps=2_000_000,
                            probe_errorlevel=False)
    assert not ok.timed_out
    assert 'alive' in ok.output


def test_mode_configures_com1(dos_rw):
    """MODE sees the BIOS-advertised COM1 port and configures 9600 8N1."""
    r = dos_rw.run_command('MODE COM1:96,N,8,1', max_steps=3_000_000,
                           probe_errorlevel=False)
    assert not r.timed_out
    assert 'COM1: 9600,n,8,1' in r.output


def test_ctty_com1_roundtrip(dos_rw):
    """CTTY redirects DOS to COM1, accepts a command, and returns to VGA."""
    previous_screen = dos_rw.vga_str()
    dos_rw.inject_string('CTTY COM1\r')
    dos_rw.run_steps(100_000)  # let DOS finish switching console devices

    dos_rw.emu.serial.inject_string('ECHO serial-ok\rCTTY CON\r')
    _steps, timed_out = dos_rw._wait_prompt(previous_screen, 4_000_000)
    assert not timed_out

    serial_text = bytes(dos_rw.emu.serial.output).decode(
        'ascii', errors='replace')
    assert 'A>ECHO serial-ok' in serial_text
    assert '\r\nserial-ok\r\n' in serial_text
    assert 'A>CTTY CON' in serial_text
    assert dos_rw.vga_str().rstrip().endswith('A>')
