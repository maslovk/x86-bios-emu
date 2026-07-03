"""Fast unit tests for two-drive INT 13h support (Phase C feature 1)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bios import BIOS
from video import Disk


class FakeCPU:
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


def _bios(memory, video, kbd, disk_a, disk_b=None):
    b = BIOS(memory, video, kbd, disk_a, disk_b=disk_b)
    b.initialize()
    return b


_CX_12 = 0x0102   # CH=1(cyl), CL=2(sector)
_DX_H1_D1 = 0x0101  # DH=1(head), DL=1(drive B)


def test_ah02_reads_drive_b(memory, video, kbd, disk):
    """AH=02 with DL=1 reads from drive B's sectors."""
    disk_b = Disk()
    b = _bios(memory, video, kbd, disk, disk_b=disk_b)
    # Put a marker in B's LBA 55 (CHS cyl=1,head=1,sector=2 on 1.44MB).
    buf = bytearray(512)
    for i in range(512):
        buf[i] = (i + 7) & 0xFF
    disk_b.write_sector(55, buf)
    cpu = FakeCPU(ax=0x0201, cx=_CX_12, dx=_DX_H1_D1, es=0x9000, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00 and cpu.al == 1 and not (cpu.flags & 0x01)
    for i in range(16):
        assert memory.read_byte(0x90000 + i) == (i + 7) & 0xFF


def test_ah02_drive_b_absent_sets_carry(memory, video, kbd, disk):
    """DL=1 with no B: present -> AH=01, CF set."""
    b = _bios(memory, video, kbd, disk)            # no disk_b
    cpu = FakeCPU(ax=0x0201, cx=_CX_12, dx=_DX_H1_D1, es=0x9000, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x01
    assert cpu.flags & 0x01
    assert disk.read_sector(55, bytearray(512))    # A untouched... well, exists


def test_ah03_writes_drive_b(memory, video, kbd, disk):
    disk_b = Disk()
    b = _bios(memory, video, kbd, disk, disk_b=disk_b)
    for i in range(512):
        memory.write_byte(0x7C00 + i, (i ^ 0x5A) & 0xFF)
    cpu = FakeCPU(ax=0x0301, cx=_CX_12, dx=_DX_H1_D1, es=0x07C0, bx=0x0000)
    b.handlers[0x13](cpu)
    assert cpu.ah == 0x00 and not (cpu.flags & 0x01)
    buf = bytearray(512)
    disk_b.read_sector(55, buf)                     # CHS(1,1,2)->LBA 55
    for i in range(512):
        assert buf[i] == (i ^ 0x5A) & 0xFF


def test_ah08_reports_drive_count(memory, video, kbd, disk):
    # Without B:
    b1 = _bios(memory, video, kbd, disk)
    cpu = FakeCPU(ax=0x0800, dx=0x0000)
    b1.handlers[0x13](cpu)
    assert cpu.dl == 1
    # With B:
    b2 = _bios(memory, video, kbd, disk, disk_b=Disk())
    cpu = FakeCPU(ax=0x0800, dx=0x0000)
    b2.handlers[0x13](cpu)
    assert cpu.dl == 2


def test_int11h_equip_word_second_drive(memory, video, kbd, disk):
    b1 = _bios(memory, video, kbd, disk)
    cpu = FakeCPU()
    b1.handlers[0x11](cpu)
    assert (cpu.ax >> 6) & 3 == 0          # 1 drive
    b2 = _bios(memory, video, kbd, disk, disk_b=Disk())
    cpu = FakeCPU()
    b2.handlers[0x11](cpu)
    assert (cpu.ax >> 6) & 3 == 1          # 2 drives
