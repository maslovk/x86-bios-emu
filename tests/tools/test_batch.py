"""Phase E batch file processing: @ECHO OFF, IF EXIST, GOTO, FOR, %1 args, PAUSE.

Batch files are created via ``COPY CON`` (the harness ``create_file`` helper)
and run by name.  ``IF NOT ERRORLEVEL`` is deliberately avoided: this DOS 3.3
build rejects that syntax with "Syntax error" (the empty-errorlevel branch is
tested instead by relying on the true/false of ``IF EXIST``).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

pytestmark = [pytest.mark.slow, pytest.mark.tools]


def test_if_exist_goto_echo_off(dos_rw):
    """@ECHO OFF + IF EXIST + GOTO + :label: 'found' branch fires when file exists."""
    dos_rw.create_file('G.TXT', 'x')
    bat = ('@ECHO OFF\r\n'
           'echo start\r\n'
           'if exist G.TXT goto yes\r\n'
           'echo nofile\r\n'
           'goto end\r\n'
           ':yes\r\n'
           'echo found\r\n'
           ':end\r\n'
           'echo done\r\n')
    dos_rw.create_file('B1.BAT', bat)
    r = dos_rw.run_command('B1', probe_errorlevel=False, max_steps=4_000_000)
    assert not r.timed_out
    # Consider only the command's own output: everything after the 'A>B1' prompt
    # that launched it, excluding the COPY CON scrollback (which legitimately
    # echoes the batch source we typed).
    run_out = r.output.split('A>B1')[-1]
    # With ECHO OFF, commands are not echoed; only their printed output appears.
    assert 'start' in run_out
    assert 'found' in run_out
    assert 'done' in run_out
    assert 'nofile' not in run_out          # the false branch was skipped
    # The raw 'if exist...' command line must NOT appear (ECHO OFF worked).
    assert 'if exist' not in run_out
    dos_rw.run_command('DEL G.TXT', probe_errorlevel=False)
    dos_rw.run_command('DEL B1.BAT', probe_errorlevel=False)


def test_for_loop(dos_rw):
    """FOR %%f IN (a b c) DO ECHO iterates each token."""
    bat = ('@ECHO OFF\r\n'
           'for %%f in (a b c) do echo item %%f\r\n')
    dos_rw.create_file('B2.BAT', bat)
    r = dos_rw.run_command('B2', probe_errorlevel=False, max_steps=4_000_000)
    assert not r.timed_out
    assert 'item a' in r.output
    assert 'item b' in r.output
    assert 'item c' in r.output
    dos_rw.run_command('DEL B2.BAT', probe_errorlevel=False)


def test_batch_replacement_args(dos_rw):
    """%1 substitution passes a command-line argument into the batch."""
    bat = ('@echo off\r\n'
           'echo arg1=%1\r\n')
    dos_rw.create_file('B3.BAT', bat)
    r = dos_rw.run_command('B3 hello', probe_errorlevel=False, max_steps=4_000_000)
    assert not r.timed_out
    assert 'arg1=hello' in r.output
    dos_rw.run_command('DEL B3.BAT', probe_errorlevel=False)


def test_batch_remark_and_pause(dos_rw):
    """REM comments silently and PAUSE resumes on a keystroke."""
    bat = ('rem this is a comment\r\n'
           'echo before-pause\r\n'
           'pause\r\n'
           'echo after-pause\r\n')
    dos_rw.create_file('B4.BAT', bat)
    # PAUSE prints 'Strike a key when ready . . .' and waits; one Enter resumes.
    r = dos_rw.run_dialog('B4', [('Strike a key when ready', '\r')],
                          max_steps=4_000_000, probe_errorlevel=False)
    assert not r.timed_out
    run_out = r.output.split('A>B4')[-1]
    assert 'before-pause' in run_out
    assert 'after-pause' in run_out
    dos_rw.run_command('DEL B4.BAT', probe_errorlevel=False)
