"""Fast tests for legacy BIOS hard-disk support (Phase G)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bios import BIOS
from main import Emulator
from tests.test_two_drives import FakeCPU
from video import Disk, DiskView


CYLINDERS = 306
HEADS = 4
SECTORS_PER_TRACK = 17
TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK


def _hard_disk():
    return Disk(
        TOTAL_SECTORS, cylinders=CYLINDERS, heads=HEADS,
        sectors_per_track=SECTORS_PER_TRACK, hard_disk=True)


def _bios(memory, video, kbd, floppy, hard_disk):
    bios = BIOS(memory, video, kbd, floppy, hard_disk=hard_disk)
    bios.initialize()
    return bios


def test_bda_and_ah08_report_fixed_disk(memory, video, kbd, disk):
    hard_disk = _hard_disk()
    bios = _bios(memory, video, kbd, disk, hard_disk)
    assert memory.read_byte(0x475) == 1

    cpu = FakeCPU(ax=0x0800, dx=0x0080)
    bios.handlers[0x13](cpu)
    assert not cpu.flags & 0x01
    assert cpu.ah == 0
    assert cpu.ch == (CYLINDERS - 1) & 0xFF
    assert cpu.cl & 0x3F == SECTORS_PER_TRACK
    assert cpu.dh == HEADS - 1
    assert cpu.dl == 1


def test_ah15_reports_hard_disk_sector_count(memory, video, kbd, disk):
    bios = _bios(memory, video, kbd, disk, _hard_disk())
    cpu = FakeCPU(ax=0x1500, dx=0x0080)
    bios.handlers[0x13](cpu)
    assert not cpu.flags & 0x01
    assert cpu.ah == 3
    assert (cpu.cx << 16) | cpu.dx == TOTAL_SECTORS


def test_ah03_and_ah02_roundtrip_drive_80h(memory, video, kbd, disk):
    hard_disk = _hard_disk()
    bios = _bios(memory, video, kbd, disk, hard_disk)
    source = 0x7000
    for i in range(512):
        memory.write_byte(source + i, (i * 11 + 5) & 0xFF)

    # C/H/S 0/0/1 is the MBR sector on BIOS drive 80h.
    cpu = FakeCPU(ax=0x0301, cx=0x0001, dx=0x0080,
                  es=source >> 4, bx=0)
    bios.handlers[0x13](cpu)
    assert not cpu.flags & 0x01
    assert cpu.ah == 0 and cpu.al == 1

    cpu = FakeCPU(ax=0x0201, cx=0x0001, dx=0x0080,
                  es=0x8000, bx=0)
    bios.handlers[0x13](cpu)
    assert not cpu.flags & 0x01
    assert bytes(memory.ram[0x80000:0x80200]) == bytes(
        (i * 11 + 5) & 0xFF for i in range(512))
    assert disk.sectors[0] == bytearray(512)  # floppy A: stayed untouched


def test_emulator_loads_exact_c_4_17_image(tmp_path):
    path = tmp_path / 'HD.IMG'
    path.write_bytes(bytes(TOTAL_SECTORS * 512))
    emulator = Emulator(floppy_image=None, hard_disk=str(path))
    assert emulator.hard_disk is emulator.bios.hard_disk
    assert len(emulator.hard_disk.sectors) == TOTAL_SECTORS
    assert emulator.hard_disk.cylinders == CYLINDERS
    assert emulator.hard_disk.heads == HEADS
    assert emulator.hard_disk.sectors_per_track == SECTORS_PER_TRACK


def test_hard_disk_persistence_is_opt_in(tmp_path):
    path = tmp_path / 'HD.IMG'
    path.write_bytes(bytes(TOTAL_SECTORS * 512))
    emulator = Emulator(hard_disk=str(path), persist=True)
    marker = bytearray(512)
    marker[446:450] = b'MBR!'
    assert emulator.hard_disk.write_sector(0, marker)

    emulator._persist_hard_disk()

    with path.open('rb') as image:
        sector = image.read(512)
    assert sector[446:450] == b'MBR!'
    assert not emulator.hard_disk.dirty


def test_disk_view_offsets_io_and_preserves_bounds():
    disk = Disk(100)
    view = DiskView(disk, 17, 40)
    marker = bytearray([0xA5] * 512)
    assert view.write_sector(0, marker)
    assert disk.sectors[17] == marker
    assert disk.dirty

    result = bytearray(512)
    assert view.read_sector(0, result)
    assert result == marker
    assert not view.read_sector(40, result)
    assert not view.write_sector(40, marker)
