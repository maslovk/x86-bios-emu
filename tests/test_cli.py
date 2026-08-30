"""Fast tests for the user-facing command-line interface."""

import os

import pytest

from main import (BUNDLED_DOS_IMAGE, Emulator, build_argument_parser,
                  create_hard_disk_image, parse_args,
                  sanitize_snap_gtk_environment, schedule_pit_ticks)
from machine_profiles import MACHINE_PROFILES
from gtdisplay import (CURSOR_BLINK_INTERVAL_MS, _FUNCTION_KEY_SCANS,
                       _set1_scan_for_char)
from hostbridge import (audit_host_directory_deletions,
                        build_host_directory_disk, snapshot_host_directory,
                        sync_host_directory_disk)
from hostbridge import delete_missing_host_files
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


def test_cpu_backend_defaults_to_reference_python():
    _parser, args = parse_args([])
    assert args.cpu_backend == 'python'


def test_cpu_backend_c_is_an_explicit_optional_choice():
    _parser, args = parse_args(['--cpu-backend', 'c'])
    assert args.cpu_backend == 'c'


def test_machine_profile_is_selectable():
    _parser, args = parse_args(['--machine', '486dx2-66'])
    assert args.machine == '486dx2-66'
    assert set(MACHINE_PROFILES) >= {'ibm-pc-5150', 'ibm-pc-xt', '486dx2-66'}


def test_machine_profile_configures_emulator_pit():
    emulator = Emulator(enable_hardware=True, machine='486dx2-66')
    assert emulator.machine_profile.id == '486dx2-66'
    assert emulator.pit.input_clk == 1_193_180
    assert emulator.cpu.cpu_clock_hz == 66_000_000
    assert emulator.cpu.cycles_per_instruction == 2.0


def test_ibm_pc_xt_profile_uses_calibrated_cpi():
    emulator = Emulator(enable_hardware=True, machine='ibm-pc-xt')
    assert emulator.cpu.cycles_per_instruction == 16.0
    assert emulator.cpu.vram_wait_cycles == 21


def test_list_machine_profiles_is_configurable():
    _parser, args = parse_args(['--list-machines'])
    assert args.list_machines


def test_pit_speed_multiplier_is_configurable():
    _parser, args = parse_args(['--pit-speed', '2'])
    assert args.pit_speed == 2.0


def test_pole_timing_trace_is_configurable():
    _parser, args = parse_args(['--trace-pole-timing'])
    assert args.trace_pole_timing is True


def test_emulator_python_backend_is_explicit_and_resettable():
    emulator = Emulator(enable_hardware=False, cpu_backend='python')
    assert emulator.cpu.__class__.__module__ == 'cpu'
    emulator.reset_guest()
    assert emulator.cpu.__class__.__module__ == 'cpu'


@pytest.mark.parametrize('argv,message', [
    (['--boot-hard-disk'], '--boot-hard-disk requires --hard-disk IMG'),
    (['--gtk-font-size', '5'], '--gtk-font-size must be between 6 and 72'),
    (['--gtk-font-size', '73'], '--gtk-font-size must be between 6 and 72'),
    (['--pit-speed', '9'], '--pit-speed must be between 0.25 and 8'),
    (['--dos', '--persist'], '--dos protects the bundled image'),
    (['--host-dir-dos-text'], '--host-dir-dos-text requires --host-dir DIR'),
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


def test_max_instructions_is_configurable_and_validated(capsys):
    _parser, args = parse_args(['--dos', '--max-instructions', '50000000'])
    assert args.max_instructions == 50_000_000
    with pytest.raises(SystemExit):
        parse_args(['--dos', '--max-instructions', '0'])
    assert 'max-instructions must be positive' in capsys.readouterr().err


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


def test_gtk_function_keys_use_pc_bios_scan_codes():
    assert _FUNCTION_KEY_SCANS[1] == 0x3B
    assert _FUNCTION_KEY_SCANS[5] == 0x3F
    assert _FUNCTION_KEY_SCANS[10] == 0x44
    assert _FUNCTION_KEY_SCANS[11] == 0x57
    assert _FUNCTION_KEY_SCANS[12] == 0x58


def test_gtk_modifier_chords_resolve_physical_keys():
    assert _set1_scan_for_char('f') == 0x21
    assert _set1_scan_for_char('F') == 0x21
    assert _set1_scan_for_char('!') == 0x02
    assert _set1_scan_for_char('|') == 0x2B


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


def test_host_directory_bridge_matches_bios_media_descriptor(tmp_path):
    disk = build_host_directory_disk(tmp_path)
    assert disk.media_type == disk.sectors[0][0x15] == 0xF0
    assert (disk.sectors_per_track, disk.cylinders, disk.heads) == (18, 80, 2)


def test_host_directory_dos_text_normalizes_guest_only(tmp_path):
    source = b'line one\nline two\r\n'
    (tmp_path / 'SOURCE.ASM').write_bytes(source)
    (tmp_path / 'PROGRAM.COM').write_bytes(b'A\nB')

    disk = build_host_directory_disk(tmp_path, dos_text=True)
    fat = FAT12(disk)
    fat.mount()

    assert fat.read_file_by_name('SOURCE.ASM') == b'line one\r\nline two\r\n'
    assert fat.read_file_by_name('PROGRAM.COM') == b'A\nB'
    assert (tmp_path / 'SOURCE.ASM').read_bytes() == source


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


def test_host_directory_writeback_is_explicit_and_staged(tmp_path):
    disk = build_host_directory_disk(tmp_path)
    disk.read_only = False
    fat = FAT12(disk)
    fat.mount()
    fat.write_file('NEW.TXT', b'new host content')
    changed = sync_host_directory_disk(disk, tmp_path)
    assert (tmp_path / 'NEW.TXT').read_bytes() == b'new host content'
    assert str(tmp_path / 'NEW.TXT') in changed
    assert 'NEW.TXT' not in audit_host_directory_deletions(disk, tmp_path)

    fat.delete_file('NEW.TXT')
    assert 'NEW.TXT' in audit_host_directory_deletions(disk, tmp_path)
    assert delete_missing_host_files(disk, tmp_path) == ['NEW.TXT']
    assert not (tmp_path / 'NEW.TXT').exists()


def test_host_directory_large_listing_writeback(tmp_path):
    payload = (b'listing line\r\n' * 26000)[:330000]
    disk = build_host_directory_disk(tmp_path)
    disk.read_only = False
    fat = FAT12(disk)
    fat.mount()
    fat.write_file('BUILD.LST', payload)

    changed = sync_host_directory_disk(disk, tmp_path)
    output = tmp_path / 'BUILD.LST'
    assert str(output) in changed
    assert output.stat().st_size == len(payload)
    assert output.read_bytes() == payload


def test_host_directory_writeback_skips_host_guest_conflicts(tmp_path):
    (tmp_path / 'SAME.TXT').write_bytes(b'original')
    baseline = snapshot_host_directory(tmp_path)
    disk = build_host_directory_disk(tmp_path)
    disk.read_only = False
    fat = FAT12(disk)
    fat.mount()
    fat.write_file('SAME.TXT', b'guest version')
    (tmp_path / 'SAME.TXT').write_bytes(b'host version')

    changed, conflicts = sync_host_directory_disk(disk, tmp_path, baseline)
    assert changed == []
    assert conflicts == ['SAME.TXT']
    assert (tmp_path / 'SAME.TXT').read_bytes() == b'host version'


def test_host_directory_writeback_requires_persist(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--host-dir-write'])
    assert error.value.code == 2
    assert '--persist' in capsys.readouterr().err


def test_host_directory_startup_reports_write_mode(capsys, tmp_path):
    Emulator(enable_hardware=False, host_dir=str(tmp_path),
             persist=True, host_dir_write=True)
    assert 'write-back enabled' in capsys.readouterr().err


def test_host_directory_delete_requires_writeback(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--host-dir-delete'])
    assert error.value.code == 2
    assert '--host-dir-write' in capsys.readouterr().err


def test_host_directory_delete_warns_before_persistent_close(tmp_path):
    emulator = Emulator(enable_hardware=False, host_dir=str(tmp_path),
                        persist=True, host_dir_write=True,
                        host_dir_delete=True)
    emulator.disk_b.dirty = True
    warning = emulator._close_warning()
    assert warning is not None
    assert 'deleted from the host folder' in warning


def test_pit_scheduler_uses_wall_clock_and_bounds_catchup():
    interval = 1.0 / 18.2065
    assert schedule_pit_ticks(1.0, 1.0 + interval, interval) == (0, 1.0 + interval)
    ticks, deadline = schedule_pit_ticks(10.0, 1.0, interval)
    assert ticks == 4
    assert deadline == 10.0 + interval


def test_host_dir_cli_rejects_writes_and_second_floppy(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--persist'])
    assert error.value.code == 2
    assert 'read-only' in capsys.readouterr().err

    with pytest.raises(SystemExit) as error:
        parse_args(['--host-dir', str(tmp_path), '--floppy-b', 'other.img'])
    assert error.value.code == 2
    assert 'cannot be combined' in capsys.readouterr().err
