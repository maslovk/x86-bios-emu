"""Fast tests for the extended-memory map, the A20 gate, and the
keyboard-controller / port-92h control wiring (286 DPMI prerequisites)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cpu import CPU
from tests.conftest import Mem


class TestA20Gate:
    def _cpu(self, size=0x200000):
        mem = Mem()
        mem.ram = bytearray(size)
        cpu = CPU(mem, None)
        cpu.cs = 0
        cpu.ss = 0
        cpu.sp = 0x100
        return cpu, mem

    def test_a20_enabled_reaches_hma(self):
        cpu, mem = self._cpu()
        cpu.set_a20(True)
        assert cpu._gate(0x100002) == 0x100002
        mem.ram[0x100002] = 0x5A
        assert cpu._readb(0x100002) == 0x5A

    def test_a20_disabled_wraps_to_zero(self):
        cpu, mem = self._cpu()
        cpu.set_a20(False)
        mem.ram[0x00002] = 0xA5
        mem.ram[0x100002] = 0x5A
        assert cpu._gate(0x100002) == 0x00002
        assert cpu._readb(0x100002) == 0xA5

    def test_real_mode_hma_fetch_wrap(self):
        # 0xFFFF:0xFFFF -> 0x10FFEF: HMA with A20 on, 0x0FFEF wrapped off.
        cpu, mem = self._cpu()
        cpu.set_a20(True)
        cpu.cs = 0xFFFF
        cpu.ip = 0x0000
        assert cpu._gate(0xFFFF0 + 0xFFFF) == 0x10FFEF
        cpu.set_a20(False)
        assert cpu._gate(0xFFFF0 + 0xFFFF) == 0x0FFEF

    def test_non_power_of_two_backing_clamps(self):
        cpu, _ = self._cpu(size=0x110000)
        assert cpu._phys_mask == 0xFFFFF   # prefix only


class TestEmulatorWiring:
    def test_emulator_backs_eight_megabytes(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        assert len(emu.mem.ram) == 0x800000
        assert emu.cpu._phys_mask == 0x7FFFFF

    def test_keyboard_output_port_controls_a20(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        assert emu.cpu._a20
        # 0xD1 command, then output byte with A20 bit clear -> gate off.
        emu.io.outb(0x64, 0xD1)
        emu.io.outb(0x60, 0x00)
        assert not emu.cpu._a20
        emu.io.outb(0x64, 0xD1)
        emu.io.outb(0x60, 0x03)              # A20 on, reset released
        assert emu.cpu._a20

    def test_port92_controls_a20(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        emu.io.outb(0x92, 0x02)
        assert emu.cpu._a20
        emu.io.outb(0x92, 0x00)
        assert not emu.cpu._a20
        assert emu.io.inb(0x92) == 0x00

    def test_reset_pulses_are_recorded(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        assert emu.reset_requests == []
        emu.io.outb(0x64, 0xFE)              # pulse output port, reset
        assert emu.reset_requests == ['keyboard-pulse']
        emu.io.outb(0x64, 0xD1)
        emu.io.outb(0x60, 0x02)              # A20 on, reset bit low
        assert emu.reset_requests[-1] == 'keyboard-output'

    def test_int15h_extended_memory_claim_is_backed(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        # INT 15h AH=88h claims 3840 KB above 1 MiB; the RAM must cover it.
        claimed_end = 0x100000 + 3840 * 1024
        assert len(emu.mem.ram) >= claimed_end
