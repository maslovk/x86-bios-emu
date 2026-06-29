"""Unit tests for bios.py — BIOS ROM interrupt handlers."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bios import BIOS
from tests.conftest import Mem


class FakeCPU:
    """Minimal CPU struct for BIOS handler tests."""
    def __init__(self, ax=0, bx=0, cx=0, dx=0, es=0, bp=0, di=0):
        self.ax = ax; self.bx = bx; self.cx = cx; self.dx = dx
        self.es = es; self.bp = bp; self.di = di
        self.ds = 0; self.ss = 0; self.cs = 0; self.ip = 0; self.sp = 0
        self.si = 0; self.tf = 0; self.if_flag = True
        self.flags = 0; self.halted = False
        self.int_no_return = False

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


# ── BIOS Initialization ─────────────────────────────────────────

class TestBIOSInit:
    def test_leaves_unknown_ivt_entry_zeroed(self, bios_env):
        bios_env.initialize()
        assert bios_env.mem.read_word(0x05 * 4) == 0
        assert bios_env.mem.read_word(0x05 * 4 + 2) == 0

    def test_sets_bda(self, bios_env):
        bios_env.initialize()
        assert bios_env.mem.read_word(0x00406) == 80
        assert bios_env.mem.read_byte(0x00408) == 3
        assert bios_env.mem.read_word(0x00410) == 640

    def test_registers_handlers(self, bios_env):
        bios_env.initialize()
        for n in [0x10, 0x13, 0x16, 0x19, 0x20]:
            assert n in bios_env.handlers

    def test_installs_ivt_stubs_for_known_handlers(self, bios_env):
        bios_env.initialize()

        ip = bios_env.mem.read_word(0x13 * 4)
        cs = bios_env.mem.read_word(0x13 * 4 + 2)

        assert cs == 0xF000
        assert ip != 0
        base = 0xF0000 + ip
        assert bios_env.mem.read_byte(base) == 0xCD
        assert bios_env.mem.read_byte(base + 1) == 0x13
        assert bios_env.mem.read_byte(base + 2) == 0xCB

    def test_sets_int1e_diskette_table(self, bios_env):
        bios_env.initialize()
        assert bios_env.mem.read_word(0x1E * 4) == 0xEFC7
        assert bios_env.mem.read_word(0x1E * 4 + 2) == 0xF000
        assert bios_env.mem.read_byte(0xF0000 + 0xEFC7 + 4) == 9

    def test_unknown_interrupt_dispatches_via_ivt(self, bios_env):
        bios_env.mem.write_word(0x21 * 4, 0x1234)
        bios_env.mem.write_word(0x21 * 4 + 2, 0x5678)
        cpu = FakeCPU()

        bios_env.handle_interrupt(cpu, 0x21)

        assert cpu.cs == 0x5678
        assert cpu.ip == 0x1234
        assert cpu.int_no_return is True

    def test_post_clears_video(self, bios_env):
        bios_env.video.write(0, 0, ord('X'))
        bios_env.initialize()
        ch, _ = bios_env.video.buffer[0][0]
        assert ch != ord('X')


# ── INT 10h: Video ──────────────────────────────────────────────

class TestINT10h:
    def _call(self, bios_env, **regs):
        cpu = FakeCPU(**regs)
        bios_env.handlers[0x10](cpu)
        return cpu

    def test_set_video_mode(self, bios_env):
        bios_env.initialize()
        self._call(bios_env, ax=0x0003)
        assert bios_env.video.mode == 3

    def test_write_string(self, bios_env):
        bios_env.initialize()
        bios_env.mem.write_byte(0x7C00, ord('H'))
        bios_env.mem.write_byte(0x7C01, ord('i'))
        bios_env.mem.write_byte(0x7C02, 0)
        self._call(bios_env, ax=0x1301, bx=0x0007, cx=2, dx=0x0000,
                   es=0x07C0, bp=0)
        ch, _ = bios_env.video.buffer[0][0]
        assert ch == ord('H')
        ch, _ = bios_env.video.buffer[0][1]
        assert ch == ord('i')

    def test_teletype(self, bios_env):
        bios_env.initialize()
        bios_env.video.cur_x = 10; bios_env.video.cur_y = 5
        self._call(bios_env, ax=0x0E48, bx=0x0007)
        ch, _ = bios_env.video.buffer[5][10]
        assert ch == ord('H')
        assert bios_env.video.cur_x == 11

    def test_set_cursor(self, bios_env):
        bios_env.initialize()
        # INT 10h AH=02: row = dx & 0xFF, col = (dx >> 8) & 0xFF
        # dx=0x0A05 → row=5, col=10
        self._call(bios_env, ax=0x0200, dx=0x0A05)
        assert bios_env.video.cur_x == 10
        assert bios_env.video.cur_y == 5

    def test_get_cursor(self, bios_env):
        bios_env.initialize()
        bios_env.video.cur_x = 20; bios_env.video.cur_y = 10
        cpu = self._call(bios_env, ax=0x0300)
        assert (cpu.ax >> 8) & 0xFF == 10
        assert cpu.ax & 0xFF == 20

    def test_get_text_mode(self, bios_env):
        bios_env.initialize()
        bios_env.video.mode = 3
        cpu = self._call(bios_env, ax=0x0F00)
        assert cpu.ax & 0xFF == 3
        assert (cpu.ax >> 8) & 0xFF == 80

    def test_scroll_up(self, bios_env):
        bios_env.initialize()
        bios_env.video.write(0, 2, ord('X'))
        self._call(bios_env, ax=0x0601, bx=0x0007)
        ch, _ = bios_env.video.buffer[1][0]
        assert ch == ord('X')

    def test_write_char_attr(self, bios_env):
        bios_env.initialize()
        bios_env.video.cur_x = 5; bios_env.video.cur_y = 3
        self._call(bios_env, ax=0x0941, bx=0x000C, cx=1)
        ch, attr = bios_env.video.buffer[3][5]
        assert ch == ord('A')

    def test_read_char_attr(self, bios_env):
        bios_env.initialize()
        bios_env.video.write(0, 0, ord('Z'), 0x0A)
        bios_env.video.cur_x = 0; bios_env.video.cur_y = 0
        cpu = self._call(bios_env, ax=0x0800)
        assert cpu.ax & 0xFF == ord('Z')
        assert (cpu.ax >> 8) & 0xFF == 0x0A


# ── INT 11h: Equipment List ────────────────────────────────────

class TestINT11h:
    def test_equipment_list(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x11](cpu)
        assert cpu.ax == 0x0410  # 1 floppy, color VGA, no math


# ── INT 12h: Memory Size ───────────────────────────────────────

class TestINT12h:
    def test_memory_size(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x12](cpu)
        assert cpu.ax == 640


# ── INT 13h: Disk Services ─────────────────────────────────────

class TestINT13h:
    def test_reset_disk(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x0000)
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0 and (cpu.flags & 0x01) == 0

    def test_get_disk_params(self, bios_env):
        bios_env.initialize()
        bios_env.disk.media_type = 0xFD
        cpu = FakeCPU(ax=0x0800)
        bios_env.handlers[0x13](cpu)
        assert cpu.ah == 0
        assert cpu.al == 0
        assert cpu.bl == 0xFD
        assert cpu.ch == 39
        assert cpu.cl == 9
        assert cpu.dh == 1
        assert cpu.dl == 1
        assert (cpu.flags & 0x01) == 0

    def test_extended_check(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x4100, bx=0x55AA)
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0x0001 and (cpu.flags & 0x01) == 0


# ── INT 14h: Serial Services ───────────────────────────────────

class TestINT14h:
    def test_serial_init_returns_ready_status(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x00A3, dx=0x0000)
        bios_env.handlers[0x14](cpu)
        assert cpu.ah == 0x60
        assert cpu.al == 0xA3

    def test_serial_status_returns_line_and_modem_status(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x0300, dx=0x0001)
        bios_env.handlers[0x14](cpu)
        assert cpu.ah == 0x60
        assert cpu.al == 0x00


# ── INT 17h: Printer Services ──────────────────────────────────

class TestINT17h:
    def test_printer_init_returns_ready_status(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x0102, dx=0x0002)
        bios_env.handlers[0x17](cpu)
        assert cpu.ah == 0x90
        assert cpu.al == 0x02


# ── INT 16h: Keyboard ──────────────────────────────────────────

class TestINT16h:
    def test_wait_for_key(self, bios_env):
        bios_env.initialize()
        bios_env.kbd.feed_string("A")
        cpu = FakeCPU(ax=0x0000)
        bios_env.handlers[0x16](cpu)
        assert cpu.ax & 0xFF == ord('A')

    def test_check_key_empty(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x0100)
        bios_env.handlers[0x16](cpu)
        assert cpu.flags & 0x40

    def test_shift_state(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU(ax=0x0200)
        bios_env.handlers[0x16](cpu)
        assert cpu.ax == 0


# ── INT 19h: Boot Loader ───────────────────────────────────────

class TestINT19h:
    def test_boot_success(self, bios_env):
        bios_env.initialize()
        bios_env.disk.sectors[0][510] = 0x55
        bios_env.disk.sectors[0][511] = 0xAA
        cpu = FakeCPU()
        bios_env.handlers[0x19](cpu)
        assert cpu.cs == 0 and cpu.ip == 0x7C00
        assert cpu.int_no_return is True

    def test_boot_no_signature(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x19](cpu)
        assert cpu.halted is True


# ── INT 20h: Terminate ─────────────────────────────────────────

class TestINT20h:
    def test_terminate(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x20](cpu)
        assert cpu.halted is True


# ── INT 2Ah/2Bh: Time/Date ─────────────────────────────────────

class TestINT2Ah:
    def test_system_time(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x2A](cpu)
        assert cpu.cx >= 0

    def test_system_date(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x2B](cpu)
        assert cpu.ax >= 2024
        assert 1 <= cpu.cx <= 12
        assert 1 <= cpu.dx <= 31


class TestIRQ0:
    def test_irq0_default_int1c_stub_returns_normally(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        cpu.cs = 0x1111
        cpu.ip = 0x4444

        bios_env.handle_interrupt(cpu, 0x08)

        assert cpu.cs == 0x1111
        assert cpu.ip == 0x4444
        assert cpu.int_no_return is False

    def test_irq0_transfers_to_int1c_handler(self, bios_env):
        bios_env.initialize()
        bios_env.mem.write_word(0x1C * 4, 0x2222)
        bios_env.mem.write_word(0x1C * 4 + 2, 0x3333)
        cpu = FakeCPU()
        cpu.cs = 0x1111
        cpu.ip = 0x4444

        bios_env.handle_interrupt(cpu, 0x08)

        assert cpu.cs == 0x3333
        assert cpu.ip == 0x2222
        assert cpu.int_no_return is True


# ── Exception Handlers ─────────────────────────────────────────

class TestExceptions:
    def test_div_zero(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x00](cpu)
        assert cpu.halted

    def test_nmi(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x02](cpu)
        assert cpu.halted

    def test_breakpoint(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        bios_env.handlers[0x03](cpu)
        # Breakpoint doesn't halt, just prints
        assert cpu.halted is False


class TestINT13hMultiSector:
    """Test INT 13h AH=02 multi-sector read fix."""

    def test_read_single_sector(self, bios_env):
        bios_env.initialize()
        buf = bytearray(512)
        for i in range(512):
            buf[i] = i & 0xFF
        bios_env.disk.write_sector(0, buf)
        cpu = FakeCPU()
        cpu.ax = 0x0201
        cpu.cx = 0x0001
        cpu.dx = 0x0000
        cpu.es = 0x07C0
        cpu.bx = 0x0000
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0x0100

    def test_read_multi_sector(self, bios_env):
        """Read 3 sectors and verify each has correct data."""
        bios_env.initialize()
        for sector in range(3):
            buf = bytearray(512)
            for i in range(512):
                buf[i] = sector & 0xFF
            bios_env.disk.write_sector(sector, buf)
        cpu = FakeCPU()
        cpu.ax = 0x0203
        cpu.cx = 0x0001
        cpu.dx = 0x0000
        cpu.es = 0x07C0
        cpu.bx = 0x0000
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0x0100
        for sector in range(3):
            offset = sector * 512
            for i in range(10):
                assert bios_env.mem.read_byte(0x7C00 + offset + i) == sector

    def test_read_multi_sector_distinct(self, bios_env):
        """Verify sectors 0 and 2 have different data after multi-read."""
        bios_env.initialize()
        buf0 = bytearray([0xAA] * 512)
        buf2 = bytearray([0xBB] * 512)
        bios_env.disk.write_sector(0, buf0)
        bios_env.disk.write_sector(2, buf2)
        cpu = FakeCPU()
        cpu.ax = 0x0203
        cpu.cx = 0x0001
        cpu.dx = 0x0000
        cpu.es = 0x07C0
        cpu.bx = 0x0000
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0x0100
        assert bios_env.mem.read_byte(0x7C00) == 0xAA
        assert bios_env.mem.read_byte(0x7C00 + 1024) == 0xBB

    def test_read_invalid_chs_sets_carry(self, bios_env):
        bios_env.initialize()
        bios_env.disk.media_type = 0xFD
        cpu = FakeCPU()
        cpu.ax = 0x0201
        cpu.cx = 0x000A
        cpu.dx = 0x0000
        cpu.es = 0x07C0
        cpu.bx = 0x0000
        bios_env.handlers[0x13](cpu)
        assert cpu.ax == 0x0400
        assert cpu.flags & 0x01


class TestINT15h:
    """Test INT 15h extended functions."""

    def test_ext_memory_size(self, bios_env):
        """INT 15h AH=88: Get extended memory size."""
        bios_env.initialize()
        cpu = FakeCPU()
        cpu.ax = 0x8800
        bios_env.handlers[0x15](cpu)
        assert cpu.ax == 3840
        assert not cpu.flags & 0x01

    def test_move_block_to_es_di(self, bios_env):
        """INT 15h AH=87 BL=0: Move DS:SI -> ES:DI."""
        bios_env.initialize()
        for i in range(20):
            bios_env.mem.write_byte(0x0800 + i, i & 0xFF)
        cpu = FakeCPU()
        cpu.ax = 0x8700
        cpu.ds = 0x0000
        cpu.si = 0x0800
        cpu.es = 0x0000
        cpu.di = 0x0900
        cpu.cx = 10
        bios_env.handlers[0x15](cpu)
        assert (cpu.flags & 0x01) == 0
        for i in range(20):
            assert bios_env.mem.read_byte(0x0900 + i) == (i & 0xFF)
        assert cpu.si == 0x0814
        assert cpu.di == 0x0914

    def test_crc32_init(self, bios_env):
        """INT 15h AH=CA DX=0: Initialize CRC-32."""
        bios_env.initialize()
        cpu = FakeCPU()
        cpu.ax = 0xCA00
        cpu.dx = 0x0000
        bios_env.handlers[0x15](cpu)
        # Returns DX:AX = 0xFFFFFFFF (32-bit initial CRC value)
        assert cpu.ax == 0xFFFF
        assert cpu.dx == 0xFFFF
        assert (cpu.flags & 0x01) == 0

    def test_crc32_compute(self, bios_env):
        """INT 15h AH=CA DX=1: Compute CRC-32 over data."""
        bios_env.initialize()
        bios_env.mem.write_byte(0x0800, 0x41)
        bios_env.mem.write_byte(0x0801, 0x42)
        cpu = FakeCPU()
        cpu.ax = 0xCA00
        cpu.dx = 0x0001
        cpu.es = 0x0000
        cpu.bx = 0x0800
        cpu.cx = 2
        bios_env.handlers[0x15](cpu)
        assert (cpu.flags & 0x01) == 0
        assert (cpu.ax != 0 or cpu.dx != 0)

    def test_unsupported_function_sets_carry(self, bios_env):
        bios_env.initialize()
        cpu = FakeCPU()
        cpu.ax = 0x4100
        bios_env.handlers[0x15](cpu)
        assert cpu.ah == 0x86
        assert cpu.flags & 0x01
