"""Fast tests for the user-facing command-line interface."""

import os

import pytest

from main import (BUNDLED_DOS_IMAGE, Emulator, build_argument_parser,
                  create_hard_disk_image, parse_args,
                  sanitize_snap_gtk_environment)
from gtdisplay import CURSOR_BLINK_INTERVAL_MS, _GWBASIC_FUNCTION_KEYS
from hostbridge import build_host_directory_disk
from fat12 import FAT12


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


def test_cursor_blink_interval_matches_cga_16_field_rate():
    assert CURSOR_BLINK_INTERVAL_MS == 267


def test_dirty_media_warning_only_applies_to_nonpersistent_sessions():
    emulator = Emulator(enable_hardware=False, persist=False)
    assert emulator._close_warning() is None
    emulator.disk.dirty = True
    assert 'A:' in emulator._close_warning()

    persistent = Emulator(enable_hardware=False, persist=True)
    persistent.disk.dirty = True
    assert persistent._close_warning() is None




def test_create_hard_disk_image_uses_exact_legacy_geometry(tmp_path):
    image = tmp_path / 'blank-hd.img'

    sectors, size = create_hard_disk_image(str(image), cylinders=615)

    assert sectors == 615 * 4 * 17
    assert size == sectors * 512
    assert image.stat().st_size == size


def test_create_hard_disk_image_refuses_overwrite_and_bad_geometry(tmp_path):
    image = tmp_path / 'blank-hd.img'
    image.write_bytes(b'existing')

    with pytest.raises(FileExistsError):
        create_hard_disk_image(str(image))
    with pytest.raises(ValueError, match='1 to 1024'):
        create_hard_disk_image(str(tmp_path / 'bad.img'), cylinders=1025)


def test_create_hard_disk_cli_is_create_only(capsys):
    with pytest.raises(SystemExit) as error:
        parse_args(['--create-hard-disk', 'hd.img', '--gtk'])

    assert error.value.code == 2
    assert 'create-only command' in capsys.readouterr().err


def test_host_directory_bridge_is_read_only_fat12(tmp_path):
    (tmp_path / 'HELLO.TXT').write_bytes(b'hello from host')

    disk = build_host_directory_disk(tmp_path)
    fat = FAT12(disk)
    fat.mount()

    assert fat.read_file_by_name('HELLO.TXT') == b'hello from host'
    assert disk.read_only
    assert not disk.write_sector(20, bytearray(512))


def test_host_directory_bridge_supports_subdirectories_and_rejects_bad_names(tmp_path):
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'INNER.TXT').write_bytes(b'inner')
    disk = build_host_directory_disk(tmp_path)
    fat = FAT12(disk)
    fat.mount()
    entry = fat.find_file('NESTED')
    assert entry is not None and entry.is_dir
    assert any(item.full_name == 'INNER.TXT'
               for item in fat.read_dir(entry.first_cluster))
    assert fat.read_file_by_name('INNER.TXT') is None

    (tmp_path / 'this-name-is-too-long.txt').write_bytes(b'x')
    with pytest.raises(ValueError, match='8.3'):
        build_host_directory_disk(tmp_path)


def test_host_directory_refresh_rebuilds_drive_b(tmp_path):
    (tmp_path / 'OLD.TXT').write_bytes(b'old')
    emulator = Emulator(enable_hardware=False, host_dir=str(tmp_path))
    initial_fat = FAT12(emulator.disk_b)
    initial_fat.mount()
    (tmp_path / 'NEW.TXT').write_bytes(b'new')
    (tmp_path / 'OLD.TXT').unlink()

    assert emulator.refresh_host_dir()
    fat = FAT12(emulator.disk_b)
    fat.mount()
    assert fat.read_file_by_name('NEW.TXT') == b'new'
    assert fat.read_file_by_name('OLD.TXT') is None


def test_host_directory_eject_detaches_drive_b(tmp_path):
    emulator = Emulator(enable_hardware=False, host_dir=str(tmp_path))
    assert emulator.disk_b is not None
    assert emulator.eject_host_dir()
    assert emulator.disk_b is None
    assert emulator.bios.disk_b is None
    assert emulator.refresh_host_dir()
    assert emulator.disk_b is not None


def test_host_dir_cli_rejects_writes_and_second_floppy(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--persist'])
    assert error.value.code == 2
    assert 'read-only' in capsys.readouterr().err

    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--floppy-b', 'other.img'])
    assert error.value.code == 2
    assert 'cannot be combined' in capsys.readouterr().err
