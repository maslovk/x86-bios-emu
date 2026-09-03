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
        assert cpu._phys(0xFFFF, 0xFFFF) == 0x10FFEF
        cpu.set_a20(False)
        assert cpu._phys(0xFFFF, 0xFFFF) == 0x0FFEF

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
        assert not emu.cpu._a20               # boots disabled
        emu.io.outb(0x64, 0xD1)
        emu.io.outb(0x60, 0xDF)               # A20 on, reset released
        assert emu.cpu._a20
        emu.io.outb(0x64, 0xD1)
        emu.io.outb(0x60, 0x00)
        assert not emu.cpu._a20

    def test_port92_controls_a20(self):
        from main import Emulator
        emu = Emulator(enable_hardware=True)
        assert not emu.cpu._a20               # boots disabled
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


class TestWarmReset:
    """286 warm-reset continuation: shutdown code + vector 0040:0067."""

    def _emu(self):
        from main import Emulator
        return Emulator(enable_hardware=True)

    def _arm_continuation(self, emu, code, seg, off):
        emu.cmos._data[0x0F] = code
        emu.mem.write_word(0x0467, off)
        emu.mem.write_word(0x0469, seg)

    def test_shutdown_code_5_resumes_at_vector(self):
        emu = self._emu()
        self._arm_continuation(emu, 0x05, 0x1000, 0x0040)
        emu.io.outb(0x64, 0xFE)              # keyboard reset pulse
        assert emu.cpu.cs == 0x1000
        assert emu.cpu.ip == 0x0040
        assert not emu.cpu._pm
        assert emu.cmos._data[0x0F] == 0x00  # code consumed
        assert emu.reset_requests == ['keyboard-pulse']

    def test_zero_shutdown_code_does_not_resume(self):
        emu = self._emu()
        self._arm_continuation(emu, 0x00, 0x1000, 0x0040)
        before = (emu.cpu.cs, emu.cpu.ip)
        emu.io.outb(0x64, 0xFE)
        assert (emu.cpu.cs, emu.cpu.ip) == before
        assert emu.reset_requests == ['keyboard-pulse']

    def test_warm_reset_clears_protected_mode_state(self):
        emu = self._emu()
        # Enter protected mode with tables and caches populated.
        cpu = emu.cpu
        cpu._set_msw(1)
        cpu.gdt_base = 0x800
        cpu.gdt_limit = 0x3F
        cpu.idt_base = 0xA00
        cpu.idt_limit = 0x3FF
        cpu.tr_selector = 0x30
        cpu._desc_cache[0x08] = (0x500, 0xFFFF, 0x9A, 0x808)
        self._arm_continuation(emu, 0x09, 0x2000, 0x0010)
        emu.io.outb(0x92, 0x00)              # port 92h reset bit low
        assert not cpu._pm
        assert cpu.gdt_base == 0 and cpu.idt_base == 0
        assert cpu.idt_limit == 0x3FF
        assert cpu.tr_selector == 0
        assert cpu._desc_cache == {}
        assert (cpu.cs, cpu.ip) == (0x2000, 0x0010)

    def test_guest_driven_warm_reset_from_code(self):
        """End to end: guest code arms CMOS/vector and pulses the line."""
        emu = self._emu()
        cpu = emu.cpu
        # Continuation target: NOP; JMP $ at 2000:0000
        emu.mem.write_byte(0x20000, 0x90)
        emu.mem.write_word(0x20002, 0xFEEB)
        emu.mem.write_word(0x0467, 0x0000)
        emu.mem.write_word(0x0469, 0x2000)
        # Arming code at 0100:0000: write CMOS 0x0F=5, then OUT 0x64,0xFE
        code = [
            0xB0, 0x0F,                      # MOV AL, 0Fh
            0xE6, 0x70,                      # OUT 70h, AL
            0xB0, 0x05,                      # MOV AL, 5
            0xE6, 0x71,                      # OUT 71h, AL
            0xB0, 0xFE,                      # MOV AL, FEh
            0xE6, 0x64,                      # OUT 64h, AL (reset pulse)
            0xEB, 0xFE,                      # (never reached)
        ]
        for i, b in enumerate(code):
            emu.mem.write_byte(0x01000 + i, b)
        cpu.cs = 0x0100
        cpu.ip = 0x0000
        for _ in range(len(code)):
            cpu.execute()
            if emu.reset_requests:
                break
        assert emu.reset_requests == ['keyboard-pulse']
        assert (cpu.cs, cpu.ip) == (0x2000, 0x0000)
        cpu.execute()                        # NOP at the continuation
        assert cpu.ip == 0x0001

    def test_triple_fault_resumes_through_shutdown_vector(self):
        """A null-IDT fault asserts RESET and takes the BIOS continuation."""
        emu = self._emu()
        cpu = emu.cpu
        self._arm_continuation(emu, 0x0A, 0x2000, 0x0040)
        emu.mem.write_byte((cpu.cs << 4) + cpu.ip, 0xCC)  # INT3
        cpu._set_msw(1)
        cpu.idt_base = 0
        cpu.idt_limit = 0

        cpu.execute()

        assert emu.reset_requests == ['triple-fault']
        assert (cpu.cs, cpu.ip) == (0x2000, 0x0040)
        assert not cpu._pm
        assert not cpu.halted
        assert not cpu.if_flag
        assert emu.cmos._data[0x0F] == 0

    def test_triple_fault_without_shutdown_code_cold_boots(self):
        emu = self._emu()
        cpu = emu.cpu
        cpu._set_msw(1)
        cpu.idt_base = 0
        cpu.idt_limit = 0
        emu.mem.write_byte((cpu.cs << 4) + cpu.ip, 0xCC)

        cpu.execute()

        assert emu.reset_requests == ['triple-fault']
        assert (cpu.cs, cpu.ip) == (0x0000, 0x7C00)
        assert not cpu._pm
        assert not cpu.halted
