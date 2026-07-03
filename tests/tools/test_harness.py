"""Phase A harness tests.

Verify the extracted :class:`dosharness.DOSHarness` and the ``dos`` / ``dos_rw``
fixtures: boot-to-prompt, scrollback capture, the errorlevel probe (both the
zero and nonzero branches), ``COPY CON`` file creation (host-side verified),
and the rule that shipped repo images are never mutated.
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fat12 import FAT12  # noqa: E402

IMG_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'DOS3_3_525'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def _sha256_images():
    out = {}
    for name in sorted(os.listdir(IMG_DIR)):
        if name.upper().endswith('.IMG'):
            h = hashlib.sha256()
            with open(os.path.join(IMG_DIR, name), 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            out[name] = h.hexdigest()
    return out


def test_boot_to_prompt_disk01(dos):
    """DISK01 boots through DATE/TIME to the A> prompt."""
    assert 'A>' in dos.vga_str()


def test_scrollback_capture(dos):
    """DIR of the 34-file disk scrolls past 25 rows; .output holds it all."""
    r = dos.run_command('DIR', max_steps=10_000_000)
    lines = r.output.split('\n')
    # More than one screen proves the scrollback hook captured scrolled rows.
    assert len(lines) > 25
    assert 'File(s)' in r.output          # the trailing summary line
    assert 'COM' in r.output              # a recognised file extension


def test_errorlevel_zero_and_nonzero(dos_rw):
    """errorlevel() reports 0 for a succeeding command and 1 for a failing one.

    VER (internal, always succeeds) → 0.  A hand-made EXIT1.COM that terminates
    with INT 21h AH=4Ch/AL=1 → 1, exercising the probe's FAIL-marker branch.
    """
    good = dos_rw.run_command('VER')
    assert not good.timed_out
    assert good.errorlevel == 0
    assert 'Version 3.30' in good.screen

    # A bad command must still return to the prompt and let the probe run.
    bad = dos_rw.run_command('ZZZXYZ')
    assert not bad.timed_out
    assert bad.errorlevel is not None

    # EXIT1.COM: MOV AH,4Ch; MOV AL,1; INT 21h  (no NUL/CR/Ctrl-Z bytes).
    dos_rw.create_file_bytes('EXIT1.COM', bytes([0xB4, 0x4C, 0xB0, 0x01, 0xCD, 0x21]))
    fail = dos_rw.run_command('EXIT1', max_steps=6_000_000)
    assert not fail.timed_out
    assert fail.errorlevel == 1


def test_create_file_copy_con(dos_rw):
    """create_file('T.TXT','hello') round-trips through TYPE and host-side FAT12."""
    dos_rw.create_file('T.TXT', 'hello')
    r = dos_rw.run_command('TYPE T.TXT')
    assert not r.timed_out
    assert 'hello' in r.screen

    # Host-side truth check: the guest write (INT 13h AH=03) must be visible
    # to a fresh FAT12 mount of the in-memory disk.
    fat = FAT12(dos_rw.emu.disk)
    fat.mount()
    entry = fat.find_file('T.TXT')
    assert entry is not None, 'T.TXT not visible to host-side FAT12'
    assert entry.size == len('hello')
    assert fat.read_file(entry.first_cluster, entry.size) == b'hello'


def test_repo_images_untouched(dos_rw):
    """A writable session writes the temp copy, never the shipped images."""
    before = _sha256_images()
    # Force a real guest write through the temp-copy disk.
    dos_rw.create_file('GUARD.TXT', 'untouched-check')
    after = _sha256_images()
    assert before == after
