"""Fast unit tests for INT 13h disk write/verify/format subfunctions (Phase B).

Exercises the BIOS handlers directly with a stub CPU: AH=03 write + AH=02
read-back round-trip, AH=04 verify (ok + out-of-range), AH=05 format track,
and the AH=0D/15h/16h/18h register contracts.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from bios import BIOS


class FakeCPU:
    """Minimal CPU struct with the 8/16-bit register properties the disk
    handlers read/write (mirrors tests/test_bios.py's FakeCPU)."""

    def __init__(self, ax=0, bx=0, cx=0, dx=0, es=0, di=0):
        self.ax = ax; self.bx = bx; self.cx = cx; self.dx = dx
        self.es = es; self.di = di
        self.ds = self.ss = self.cs = self.ip = self.bp = self.si = 0
        self.flags = 0

    @property
    def al(self): return self.ax & 0xFF
    @al.setter
    def al(self, v): self.ax = (self.ax & 0xFF00) | (v & 0xFF)
    @property
    def ah(self): return (self.ax >> 8) & 0xFF
    @ah.setter
    def ah(self, v): self.ax = (self.ax & 0x00FF) | ((v & 0xFF) << 8)
    @property
    def bl(self): return self.bx & 0xFF
    @bl.setter
    def bl(self, v): self.bx = (self.bx & 0xFF00) | (v & 0xFF)
    @property
    def bh(self): return (self.bx >> 8) & 0xFF
    @bh.setter
    def bh(self, v): self.bx = (self.bx & 0x00FF) | ((v & 0xFF) << 8)
    @property
    def cl(self): return self.cx & 0xFF
    @cl.setter
    def cl(self, v): self.cx = (self.cx & 0xFF00) | (v & 0xFF)
    @property
    def ch(self): return (self.cx >> 8) & 0xFF
    @ch.setter
    def ch(self, v): self.cx = (self.cx & 0x00FF) | ((v & 0xFF) << 8)
    @property
    def dl(self): return self.dx & 0xFF
    @dl.setter
    def dl(self, v): self.dx = (self.dx & 0xFF00) | (v & 0xFF)
    @property
    def dh(self): return (self.dx >> 8) & 0xFF
    @dh.setter
    def dh(self, v): self.dx = (self.dx & 0x00FF) | ((v & 0xFF) << 8)


# CHS (cyl=1, head=1, sector=2) on a 1.44MB image -> LBA 55, for 2 sectors.
_CX_12 = 0x0102   # CH=1(cyl), CL=2(sector)
_DX_H1 = 0x0100   # DH=1(head), DL=0(drive)


def _bios(memory, video, kbd, disk):
    b = BIOS(memory, video, kbd, disk)
    b.initialize()
    return b


def test_ah03_write_then_ah02_read(memory, video, kbd, disk):
    """Write 2 sectors via AH=03, read them back via AH=02, compare."""
    b = _bios(memory, video, kbd, disk)
    src = 0x7C00
    for i in range(1024):
        memory.write_byte(src + i, (i * 7 + 3) & 0xFF)
    # Write ES:BX(=0x07C0:0) -> disk
    cpu = FakeCPU(ax=0x0302, cx=_CX_12, dx=_DX_H1, es=0x07C0, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ax == 0x0002 and not (cpu.flags & 0x01)
    # Read disk -> ES:BX(=0x9000:0)
    cpu = FakeCPU(ax=0x0202, cx=_CX_12, dx=_DX_H1, es=0x9000, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ax == 0x0002 and not (cpu.flags & 0x01)
    dst = 0x90000
    for i in range(1024):
        assert memory.read_byte(dst + i) == (i * 7 + 3) & 0xFF


def test_ah02_and_ah03_bulk_transfer_wraps_es_bx_at_64k(
        memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    pattern = bytes((i * 11 + 5) & 0xFF for i in range(512))
    disk.write_sector(55, pattern)

    cpu = FakeCPU(
        ax=0x0201, cx=_CX_12, dx=_DX_H1, es=0x2000, bx=0xFF00)
    b.handlers[0x13](cpu)

    assert cpu.ax == 0x0001 and not (cpu.flags & 0x01)
    assert memory.ram[0x2FF00:0x30000] == pattern[:256]
    assert memory.ram[0x20000:0x20100] == pattern[256:]

    replacement = bytes((255 - i) & 0xFF for i in range(512))
    memory.ram[0x2FF00:0x30000] = replacement[:256]
    memory.ram[0x20000:0x20100] = replacement[256:]
    cpu = FakeCPU(
        ax=0x0301, cx=_CX_12, dx=_DX_H1, es=0x2000, bx=0xFF00)
    b.handlers[0x13](cpu)

    result = bytearray(512)
    assert disk.read_sector(55, result)
    assert result == replacement


def test_bulk_segment_transfer_wraps_at_20_bit_address_space(
        memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    pattern = bytes(range(32))

    b._write_segment_buffer(0xFFFF, 0x0008, pattern)

    assert memory.ram[0xFFFF8:0x100000] == pattern[:8]
    assert memory.ram[:24] == pattern[8:]
    assert b._read_segment_buffer(0xFFFF, 0x0008, len(pattern)) == pattern


def test_ah03_out_of_range_sets_carry(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    # sector 30 > spt 18 (1.44MB) -> invalid
    cpu = FakeCPU(ax=0x0301, cx=0x001E, dx=_DX_H1, es=0x07C0, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ax == 0x0400
    assert cpu.flags & 0x01


def test_ah04_verify_ok_and_out_of_range(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    cpu = FakeCPU(ax=0x0401, cx=_CX_12, dx=_DX_H1, es=0x07C0, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00 and cpu.al == 1 and not (cpu.flags & 0x01)
    # out of range
    cpu = FakeCPU(ax=0x0401, cx=0x001E, dx=_DX_H1, es=0x07C0, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ax == 0x0400 and (cpu.flags & 0x01)


def test_ah05_format_track_zeros_sectors(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    disk.media_type = 0xFD                       # 360KB: spt=9
    # Put non-zero data in sectors 0..8 (C=0,H=0,R=1..9 -> LBA 0..8).
    nz = bytearray([0xAA] * 512)
    for lba in range(9):
        disk.write_sector(lba, nz)
    # Address-field table at ES:BX: 4 bytes/sector (C,H,R,N), 9 entries.
    table_off = 0x4000
    for r in range(1, 10):
        base = table_off + (r - 1) * 4
        memory.write_byte(base + 0, 0)   # C
        memory.write_byte(base + 1, 0)   # H
        memory.write_byte(base + 2, r)   # R
        memory.write_byte(base + 3, 2)   # N (512 bytes)
    cpu = FakeCPU(ax=0x0509, cx=0x0001, dx=0x0000, es=0x0000, bx=table_off)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00 and cpu.al == 9 and not (cpu.flags & 0x01)
    buf = bytearray(512)
    for lba in range(9):
        disk.read_sector(lba, buf)
        assert buf == bytearray(512)    # formatted = zero-filled


def test_ah15_get_disk_type(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    cpu = FakeCPU(ax=0x1500)
    b.handlers[0x13](cpu)
    assert cpu.ah == 2                   # floppy with change-line
    assert not (cpu.flags & 0x01)


def test_ah16_media_change_flag(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    disk.media_changed = True
    cpu = FakeCPU(ax=0x1600)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x06                 # media changed
    assert not disk.media_changed         # flag cleared after read
    # Second call now reports no change.
    cpu = FakeCPU(ax=0x1600)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00


def test_ah18_returns_diskette_param_table(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    cpu = FakeCPU(ax=0x1800)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00
    assert cpu.di == 0xEFC7              # INT 1Eh diskette param table
    assert cpu.es == 0xF000
    assert not (cpu.flags & 0x01)


def test_ah0d_invalid(memory, video, kbd, disk):
    b = _bios(memory, video, kbd, disk)
    cpu = FakeCPU(ax=0x0D00)
    b.handlers[0x13](cpu)
    assert cpu.ax == 0x0100              # AH=01 (bad command), AL=0
    assert cpu.flags & 0x01
