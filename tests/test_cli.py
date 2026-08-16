"""Fast tests for the user-facing command-line interface."""

import os

import pytest

from main import (BUNDLED_DOS_IMAGE, Emulator, build_argument_parser,
                  parse_args, sanitize_snap_gtk_environment)
from gtdisplay import _GWBASIC_FUNCTION_KEYS


def test_dos_shortcut_selects_bundled_image_and_terminal_input():
    _parser, args = parse_args(['--dos'])

    assert args.floppy == BUNDLED_DOS_IMAGE
    assert os.path.isfile(args.floppy)
    assert args.interactive
    assert not args.persist


def test_dos_shortcut_allows_gtk():
    _parser, args = parse_args(['--dos', '--gtk'])

    assert args.gtk
    assert args.interactive


@pytest.mark.parametrize('argv,message', [
    (['--boot-hard-disk'], '--boot-hard-disk requires --hard-disk IMG'),
    (['--gtk-font-size', '5'], '--gtk-font-size must be between 6 and 72'),
    (['--gtk-font-size', '73'], '--gtk-font-size must be between 6 and 72'),
    (['--dos', '--persist'], '--dos protects the bundled image'),
    (['--floppy', 'does-not-exist.img'], '--floppy: file not found'),
])
def test_invalid_options_report_concise_errors(argv, message, capsys):
    with pytest.raises(SystemExit) as error:
        parse_args(argv)

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_dos_and_custom_floppy_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as error:
        parse_args(['--dos', '--floppy', BUNDLED_DOS_IMAGE])

    assert error.value.code == 2
    assert 'not allowed with argument --dos' in capsys.readouterr().err


def test_help_is_grouped_and_includes_working_examples():
    help_text = build_argument_parser().format_help()

    assert 'boot media:' in help_text
    assert 'display and input:' in help_text
    assert 'runtime:' in help_text
    assert 'python3 main.py --dos --gtk' in help_text
    assert 'use a GTK window for display and keyboard input' in help_text


def test_no_serial_disables_host_echo_but_keeps_com1_device(capsys):
    _parser, args = parse_args(['--no-serial'])
    emulator = Emulator(enable_hardware=False,
                        serial_output=args.serial_output)

    emulator.serial.outb(0, ord('X'))

    assert emulator.serial.output == [ord('X')]
    assert capsys.readouterr().err == ''


def test_native_python_removes_inherited_snap_gtk_environment():
    environment = {
        'SNAP': '/snap/code/254',
        'GTK_PATH': '/snap/code/254/usr/lib/gtk-3.0',
        'GTK_MODULES': 'gail:atk-bridge',
        'GIO_MODULE_DIR': '/home/user/snap/code/common/gio-modules',
        'SNAP_LIBRARY_PATH': '/var/lib/snapd/lib/gl',
        'LD_LIBRARY_PATH': '/snap/core20/current/lib',
        'UNRELATED': 'preserved',
    }

    removed = sanitize_snap_gtk_environment(
        environment, executable='/usr/bin/python3')

    assert set(removed) == {
        'GTK_PATH', 'GTK_MODULES', 'GIO_MODULE_DIR', 'SNAP_LIBRARY_PATH',
        'LD_LIBRARY_PATH',
    }
    assert environment == {
        'SNAP': '/snap/code/254', 'UNRELATED': 'preserved'}


def test_python_inside_snap_keeps_its_gtk_environment():
    environment = {
        'SNAP': '/snap/code/254',
        'GTK_PATH': '/snap/code/254/usr/lib/gtk-3.0',
    }

    removed = sanitize_snap_gtk_environment(
        environment, executable='/snap/code/254/usr/bin/python3')

    assert removed == ()
    assert environment['GTK_PATH'] == '/snap/code/254/usr/lib/gtk-3.0'


def test_non_snap_environment_is_untouched():
    environment = {'GTK_PATH': '/usr/lib/gtk-3.0'}

    assert sanitize_snap_gtk_environment(
        environment, executable='/usr/bin/python3') == ()
    assert environment == {'GTK_PATH': '/usr/lib/gtk-3.0'}


def test_gw_basic_function_key_macros_match_status_line():
    assert _GWBASIC_FUNCTION_KEYS[1] == 'LIST '
    assert _GWBASIC_FUNCTION_KEYS[2] == 'RUN'
    assert _GWBASIC_FUNCTION_KEYS[3] == 'LOAD "'
    assert _GWBASIC_FUNCTION_KEYS[4] == 'SAVE "'
    assert set(_GWBASIC_FUNCTION_KEYS) == set(range(1, 11))
