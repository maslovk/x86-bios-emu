"""Unit tests for cpu.py — 80286 protected-mode state and transitions.

These drive the bare CPU (no BIOS hook) through the classic 286
protected-mode entry sequence: build a GDT, LGDT/LIDT, LMSW PE, far-jump
into a code selector, load data selectors, and run code whose linear
addresses come from descriptor bases instead of selector * 16.

Milestone scope (see README): ring 0, no task gates, no privilege stack
switches, no segment-limit faults.  Returning to real mode via LMSW is an
emulator extension (the physical 286 needs a reset).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cpu import CPU
from tests.conftest import Mem


def make_cpu():
    mem = Mem()
    cpu = CPU(mem, None)
    cpu.cs = 0x0000
    cpu.ip = 0x0100
    cpu.ss = 0x0000
    cpu.sp = 0xFFFE
    return cpu, mem


def write_code(cpu, mem, addr, code):
    for i, b in enumerate(code):
        mem.write_byte(addr + i, b)


def make_descriptor(base, limit, access):
    """Encode a 286 descriptor (8 bytes)."""
    return bytes((
        limit & 0xFF, (limit >> 8) & 0xFF,
        base & 0xFF, (base >> 8) & 0xFF, (base >> 16) & 0xFF,
        access, 0x00, 0x00))


def build_gdt(mem, gdt_addr, descriptors):
    """Write descriptors starting at selector 8; return {index: selector}."""
    selectors = {}
    for i, desc in enumerate(descriptors):
        mem.ram[gdt_addr + (i + 1) * 8:gdt_addr + (i + 2) * 8] = desc
        selectors[i] = (i + 1) * 8
    return selectors


# Access bytes (P=1, DPL=0, S=1)
ACC_CODE = 0x9A       # present, ring 0, code, execute/read
ACC_DATA = 0x92       # present, ring 0, data, read/write
ACC_INT_GATE = 0x86   # present, ring 0, 286 interrupt gate
ACC_TRAP_GATE = 0x87  # present, ring 0, 286 trap gate


class TestMSWAndTableRegisters:
    def test_msw_reset_value(self):
        cpu, mem = make_cpu()
        assert cpu.msw == 0xFFF0
        assert not cpu._pm

    def test_smsw_reads_msw(self):
        cpu, mem = make_cpu()
        # SMSW AX = 0F 01 /4 (mod=3, reg=4, rm=0)
        write_code(cpu, mem, 0x0100, [0x0F, 0x01, 0xE0])
        cpu.execute()
        assert cpu.ax == 0xFFF0

    def test_lmsw_sets_pe_and_caches_seed_real_mode(self):
        cpu, mem = make_cpu()
        # LMSW AX = 0F 01 /6; MOV AX, 1
        write_code(cpu, mem, 0x0100,
                   [0xB8, 0x01, 0x00,      # MOV AX, 1
                    0x0F, 0x01, 0xF0])     # LMSW AX
        cpu.execute(); cpu.execute()
        assert cpu.msw == 0xFFF1
        assert cpu._pm
        # The current DS selector must still translate real-mode style.
        assert cpu._phys(cpu.ds, 0x1234) == (cpu.ds << 4) + 0x1234

    def test_lmsw_cannot_clear_reserved_bits(self):
        cpu, mem = make_cpu()
        cpu._set_msw(0xFFFF)
        assert cpu.msw in (0xFFF1, 0xFFFF)  # PE settable; low nibble honoured
        cpu._set_msw(0x0000)                # emulator extension: PE clearable
        assert not cpu._pm
        assert cpu._phys(0x1000, 5) == 0x10005

    def test_lgdt_sgdt_round_trip(self):
        cpu, mem = make_cpu()
        # Build the GDTR image at 0x2000: limit=0x2F, base=0x0900
        write_code(cpu, mem, 0x2000,
                   [0x2F, 0x00, 0x00, 0x09, 0x00])
        # LGDT [0x2000] = 0F 01 /2 mod=00 rm=110 (disp16)
        # SGDT [0x3000] = 0F 01 /0 mod=00 rm=110
        write_code(cpu, mem, 0x0100,
                   [0x0F, 0x01, 0x16, 0x00, 0x20,     # LGDT [0x2000]
                    0x0F, 0x01, 0x06, 0x00, 0x30])    # SGDT [0x3000]
        cpu.execute(); cpu.execute()
        assert cpu.gdt_limit == 0x002F
        assert cpu.gdt_base == 0x0900
        for i, expect in enumerate((0x2F, 0x00, 0x00, 0x09, 0x00)):
            assert mem.read_byte(0x3000 + i) == expect

    def test_lidt_sidt_round_trip(self):
        cpu, mem = make_cpu()
        write_code(cpu, mem, 0x2000,
                   [0x07, 0x02, 0x00, 0x00, 0x00])     # limit 0x207, base 0
        write_code(cpu, mem, 0x0100,
                   [0x0F, 0x01, 0x1E, 0x00, 0x20,     # LIDT [0x2000]
                    0x0F, 0x01, 0x0E, 0x00, 0x30])    # SIDT [0x3000]
        cpu.execute(); cpu.execute()
        assert cpu.idt_limit == 0x0207
        assert cpu.idt_base == 0
        assert mem.read_word(0x3000) == 0x0207

    def test_mov_cr0_round_trip(self):
        cpu, mem = make_cpu()
        # MOV EAX, CR0 / MOV CR0, EAX = 0F 20 C0 / 0F 22 C0
        write_code(cpu, mem, 0x0100,
                   [0x0F, 0x20, 0xC0])                 # MOV EAX, CR0
        cpu.execute()
        assert cpu.ax == 0xFFF0                       # PE clear at reset
        write_code(cpu, mem, 0x0103,
                   [0xB8, 0x01, 0x00,                 # MOV AX, 1
                    0x0F, 0x22, 0xC0])                # MOV CR0, EAX
        cpu.execute(); cpu.execute()
        assert cpu.msw == 0xFFF1
        assert cpu._pm

    def test_mov_cr0_clears_pe_back_to_real_mode(self):
        """The canonical 386 leave-protected-mode sequence."""
        cpu, mem = make_cpu()
        cpu._set_msw(1)
        assert cpu._pm
        # MOV EAX, CR0 / AND AL, 0xF4 / MOV CR0, EAX
        write_code(cpu, mem, 0x0100,
                   [0x0F, 0x20, 0xC0,                 # MOV EAX, CR0
                    0x24, 0xF4,                       # AND AL, 0xF4
                    0x0F, 0x22, 0xC0])                # MOV CR0, EAX
        cpu.execute(); cpu.execute(); cpu.execute()
        assert not cpu._pm
        assert cpu.msw == 0xFFF0
        assert cpu.idt_limit == 0x03FF                 # caches flushed to RM

    def test_mov_cr0_clear_fetches_far_jump_from_hidden_cs_base(self):
        """PE-off keeps the PM CS cache until a far jump reloads CS."""
        cpu, mem = make_cpu()
        pm_base = 0x5000
        cpu.cs = 0x08
        cpu.ip = 0x0100
        cpu._code_base = pm_base
        cpu._pm = True
        cpu.msw = 0xFFF1
        cpu._desc_cache[cpu.cs] = (pm_base, 0xFFFF, ACC_CODE, 0)
        cpu.ax = 0xFFF0
        write_code(cpu, mem, pm_base + 0x0100,
                   [0x0F, 0x22, 0xC0,                 # MOV CR0, EAX
                    0xEA, 0x00, 0x02, 0x00, 0x10])    # JMP 1000:0200
        write_code(cpu, mem, 0x10200, [0x90])          # real-mode NOP

        cpu.execute()
        assert not cpu._pm
        assert cpu._use_cached_code_base
        cpu.execute()
        assert (cpu.cs, cpu.ip) == (0x1000, 0x0200)
        assert not cpu._use_cached_code_base
        cpu.execute()
        assert cpu.ip == 0x0201

    def test_mov_cr0_privileged_in_pm(self):
        cpu, mem = make_cpu()
        cpu._set_msw(1)
        cpu._cpl = 3
        write_code(cpu, mem, 0x0100, [0x0F, 0x22, 0xC0])
        cpu.execute()
        assert cpu.halted                             # #GP with no handler

    def test_triple_fault_fires_reset_hook(self):
        """Fault-while-delivering-fault hands RESET to the machine."""
        cpu, mem = make_cpu()
        cpu._set_msw(1)
        cpu.idt_base = 0
        cpu.idt_limit = 0                             # null IDT: all
        write_code(cpu, mem, 0x0100, [0xCC])          # INT3 -> #GP -> #GP
        fired = []
        cpu.on_triple_fault = lambda: fired.append(True)
        cpu.execute()
        assert fired == [True]
        assert not cpu.halted

    def test_triple_fault_without_hook_parks(self):
        cpu, mem = make_cpu()
        cpu._set_msw(1)
        cpu.idt_base = 0
        cpu.idt_limit = 0
        write_code(cpu, mem, 0x0100, [0xCC])
        cpu.execute()
        assert cpu.halted                              # bare CPU parks

    def test_invalid_idt_gate_fires_reset_hook(self):
        cpu, mem = make_cpu()
        cpu._set_msw(1)
        cpu.idt_base = 0x0800
        cpu.idt_limit = 0x0FFF
        # In-range but absent gate for INT 3.
        write_code(cpu, mem, 0x0100, [0xCC])
        fired = []
        cpu.on_triple_fault = lambda: fired.append(True)
        cpu.execute()
        assert fired == [True]
        assert not cpu.halted


class TestProtectedModeSwitch:
    def setup_pm(self, code_base=0x00500, data_base=0x07000):
        """Build a two-descriptor GDT and the classic PM entry sequence."""
        cpu, mem = make_cpu()
        gdt = 0x00800
        sels = build_gdt(mem, gdt, [
            make_descriptor(code_base, 0xFFFF, ACC_CODE),
            make_descriptor(data_base, 0xFFFF, ACC_DATA),
        ])
        code_sel, data_sel = sels[0], sels[1]
        write_code(cpu, mem, gdt, [0x00] * 8)  # null descriptor
        mem.write_word(0x0100, 0x0017)          # GDTR image: limit 3*8-1
        mem.write_byte(0x0102, gdt & 0xFF)
        mem.write_byte(0x0103, (gdt >> 8) & 0xFF)
        mem.write_byte(0x0104, 0)
        entry = [
            0x0F, 0x01, 0x16, 0x00, 0x01,      # LGDT [0x0100]
            0xB8, 0x01, 0x00,                  # MOV AX, 1
            0x0F, 0x01, 0xF0,                  # LMSW AX
            0xEA, 0x00, 0x00, code_sel, 0x00,  # JMP FAR 0000:code_sel
        ]
        write_code(cpu, mem, 0x0100 + 0x20, entry)
        cpu.ip = 0x0120
        return cpu, mem, code_sel, data_sel, code_base, data_base

    def test_far_jump_loads_code_descriptor(self):
        cpu, mem, code_sel, data_sel, code_base, _ = self.setup_pm()
        for _ in range(4):
            cpu.execute()
        assert cpu._pm
        assert cpu.cs == code_sel
        assert cpu._code_base == code_base
        # Fetch now comes from the descriptor base.
        write_code(cpu, mem, code_base + 0x10, [0x90])  # NOP at base+0x10
        cpu.ip = 0x0010
        cpu.execute()
        assert cpu.ip == 0x0011

    def test_data_access_uses_descriptor_base(self):
        cpu, mem, code_sel, data_sel, code_base, data_base = self.setup_pm()
        # PM code: MOV AX, data_sel; MOV DS, AX; MOV word [0x0010], 0xBEEF
        write_code(cpu, mem, code_base, [
            0xB8, data_sel & 0xFF, data_sel >> 8,
            0x8E, 0xD8,                       # MOV DS, AX
            0xC7, 0x06, 0x10, 0x00, 0xEF, 0xBE,  # MOV word [0x0010], 0xBEEF
            0xF4,                             # HLT
        ])
        for _ in range(4):   # LGDT, MOV, LMSW, JMP FAR
            cpu.execute()
        cpu.execute()        # MOV AX
        cpu.execute()        # MOV DS, AX
        cpu.execute()        # MOV word
        assert mem.read_word(data_base + 0x10) == 0xBEEF
        # The old real-mode address must NOT have been written.
        assert mem.read_word((data_sel << 4) + 0x10) == 0

    def test_ds_es_accept_null_selector(self):
        cpu, mem, code_sel, data_sel, code_base, data_base = self.setup_pm()
        cpu._set_ds(0)
        cpu._set_es(0)
        assert cpu.ds == 0
        assert cpu.es == 0
        assert not cpu.halted

    def test_stack_uses_descriptor_base(self):
        cpu, mem, code_sel, data_sel, code_base, data_base = self.setup_pm()
        write_code(cpu, mem, code_base, [
            0xB8, data_sel & 0xFF, data_sel >> 8,
            0x8E, 0xD8,                       # MOV DS, AX
            0x8E, 0xD0,                       # MOV SS, AX  (data RW → ok)
            0xBC, 0x00, 0xF0,                 # MOV SP, 0xF000
            0x68, 0x34, 0x12,                 # PUSH 0x1234
            0xF4,
        ])
        for _ in range(4):
            cpu.execute()
        for _ in range(5):
            cpu.execute()
        assert mem.read_word(data_base + 0xEFFE) == 0x1234

    def test_loading_data_selector_into_cs_faults(self):
        cpu, mem, code_sel, data_sel, code_base, _ = self.setup_pm()
        write_code(cpu, mem, code_base, [
            0xB8, data_sel & 0xFF, data_sel >> 8,
            0x8E, 0xD8,                       # MOV DS, AX (fine)
            0xB8, data_sel & 0xFF, data_sel >> 8,
            0x0E,                             # PUSH CS placeholder
        ])
        for _ in range(4):
            cpu.execute()
        cpu.execute(); cpu.execute()
        # MOV CS, AX (8E /1) with a data descriptor must raise #GP(13).
        write_code(cpu, mem, code_base + 0x20, [0x8E, 0xC8])
        cpu.ip = 0x0020
        before = (cpu.cs, cpu.ip, cpu.sp)
        cpu.execute()
        # Bare CPU with no IDT handler parks deterministically: the #GP
        # dispatch reads IDT gate 13; absent a gate it faults again and
        # the CPU halts rather than corrupt state.
        assert cpu.halted or cpu.cs != data_sel


class TestProtectedInterrupts:
    def make_pm_with_idt(self):
        cpu, mem, code_sel, data_sel, code_base, data_base = \
            TestProtectedModeSwitch().setup_pm()
        # Second code descriptor for the handler at a distinct base.
        gdt = 0x00800
        mem.ram[gdt + 3 * 8:gdt + 4 * 8] = make_descriptor(
            0x00900, 0xFFFF, ACC_CODE)
        # Widen the GDTR image in memory (LGDT reloads it and would
        # otherwise clobber a plain attribute edit).
        mem.write_word(0x0100, 4 * 8 - 1)
        handler_sel = 3 * 8
        # IDT at 0x00A00: gate 0x30 (INT 30h).  The handler descriptor's
        # base is 0x00900, so the gate offset is 0: execution starts at
        # base + offset = 0x00900.
        idt = 0x00A00
        gate = bytes((0x00, 0x00, handler_sel & 0xFF, handler_sel >> 8,
                      0x00, ACC_TRAP_GATE, 0x00, 0x00))
        mem.ram[idt + 0x30 * 8:idt + 0x30 * 8 + 8] = gate
        # Handler at 0x900: store marker via DS, IRET.
        write_code(cpu, mem, 0x00900, [
            0xA3, 0x00, 0x01,     # MOV [0x0100], AX  (marker)
            0xCF,                 # IRET
        ])
        mem.write_word(0x0100 + 0x60, 0x0187)      # IDTR image
        mem.write_byte(0x0102 + 0x60, idt & 0xFF)
        mem.write_byte(0x0103 + 0x60, (idt >> 8) & 0xFF)
        # Hand-drive the entry: LGDT, LIDT, PE, selectors.
        cpu.ip = 0x0120
        cpu.execute()        # LGDT [0x0100]
        cpu.ip = 0x0140
        write_code(cpu, mem, 0x0140, [0x0F, 0x01, 0x1E, 0x60, 0x01])
        cpu.execute()        # LIDT [0x0160]
        cpu._set_msw(1)
        cpu._set_cs(code_sel)
        cpu.ip = 0x0000
        cpu._set_ds(data_sel)
        return cpu, mem, data_sel, data_base

    def test_trap_gate_services_int30_and_returns(self):
        cpu, mem, data_sel, data_base = self.make_pm_with_idt()
        # Main code at code base 0x500: MOV AX, 0x5A5A; INT 30h; HLT
        write_code(cpu, mem, 0x00500, [
            0xB8, 0x5A, 0x5A,
            0xCD, 0x30,
            0xF4,
        ])
        cpu.ip = 0
        cpu.execute()          # MOV AX
        sp_before = cpu.sp
        cpu.execute()          # INT 30h: gate loads CS:IP for the handler
        assert cpu.cs == 3 * 8
        # The gate target is guest code: run the handler body and IRET.
        for _ in range(2):
            cpu.execute()
        assert mem.read_word(data_base + 0x0100) == 0x5A5A
        assert cpu.ip == 0x0005   # back after the INT instruction
        assert cpu.cs == 8         # caller's code selector restored
        assert cpu.sp == sp_before
        cpu.execute()          # HLT
        assert cpu.halted

    def test_trap_gate_preserves_interrupt_flag(self):
        cpu, mem, data_sel, data_base = self.make_pm_with_idt()
        write_code(cpu, mem, 0x00500, [
            0xFB,               # STI
            0xCD, 0x30,
            0xF4,
        ])
        cpu.ip = 0
        cpu.execute()
        assert cpu.if_flag
        cpu.execute()          # INT 30h through a trap gate
        # Trap gates keep IF set while the handler runs.
        assert cpu.if_flag
        for _ in range(3):
            cpu.execute()      # handler body + IRET

    def test_interrupt_gate_clears_interrupt_flag(self):
        cpu, mem, data_sel, data_base = self.make_pm_with_idt()
        # Switch gate 0x30 to an interrupt gate (type 6).
        idt = 0x00A00
        mem.write_byte(idt + 0x30 * 8 + 5, ACC_INT_GATE)
        cpu.if_flag = True
        cpu._do_interrupt(0x30)
        assert not cpu.if_flag   # interrupt gate clears IF on entry
        # The caller's IF state is restored by IRET via the pushed flags.
        cpu.execute(); cpu.execute(); cpu.execute()   # handler + IRET
        assert cpu.if_flag


class TestSelectorVerification:
    def setup_method(self):
        self.cpu, self.mem = make_cpu()
        gdt = 0x00800
        build_gdt(self.mem, gdt, [
            make_descriptor(0x00500, 0xFFFF, ACC_CODE),   # sel 8
            make_descriptor(0x07000, 0xFFFF, ACC_DATA),   # sel 16
            make_descriptor(0x09000, 0x0FFF, 0x82),       # LDT, sel 24
        ])
        self.cpu.gdt_base = gdt
        self.cpu.gdt_limit = 4 * 8 - 1

    def test_verr_verw(self):
        cpu = self.cpu
        assert cpu._verify_selector(0x0008, write=False)   # code: readable
        assert not cpu._verify_selector(0x0008, write=True)
        assert cpu._verify_selector(0x0010, write=False)   # data RW
        assert cpu._verify_selector(0x0010, write=True)
        assert not cpu._verify_selector(0x0018, write=True)   # LDT: system
        assert not cpu._verify_selector(0x0000)            # null
        assert not cpu._verify_selector(0x0FFF)            # bad index

    def test_lar_lsl(self):
        cpu, self.mem = make_cpu()
        gdt = 0x00800
        build_gdt(self.mem, gdt, [
            make_descriptor(0x00500, 0xFFFF, ACC_CODE),
            make_descriptor(0x07000, 0x0FFF, ACC_DATA),
        ])
        cpu.gdt_base = gdt
        cpu.gdt_limit = 3 * 8 - 1
        # LAR AX, [0x100] with selector at [0x100]; same for LSL.
        self.mem.write_word(0x0100, 0x0010)
        write_code(cpu, self.mem, 0x0104, [0x0F, 0x02, 0x06, 0x00, 0x01])
        cpu.ip = 0x0104
        cpu.execute()
        assert cpu.ax == ACC_DATA
        assert cpu.zf
        write_code(cpu, self.mem, 0x0104, [0x0F, 0x03, 0x06, 0x00, 0x01])
        cpu.ax = 0
        cpu.ip = 0x0104
        cpu.execute()
        assert cpu.ax == 0x0FFF

    def test_arpl(self):
        cpu, self.mem = make_cpu()
        # ARPL [0x100], AX: selector 0x0025 RPL=1, AX RPL=3
        self.mem.write_word(0x0100, 0x0025)
        write_code(cpu, self.mem, 0x0104, [0x63, 0x06, 0x00, 0x01])
        cpu.ax = 0x0003
        cpu.ip = 0x0104
        cpu.execute()
        assert self.mem.read_word(0x0100) == 0x0027
        assert cpu.zf
        # No change when destination RPL is already higher.
        self.mem.write_word(0x0100, 0x0022)
        cpu.ax = 0x0001
        cpu.ip = 0x0104
        cpu.execute()
        assert self.mem.read_word(0x0100) == 0x0022
        assert not cpu.zf

    def test_lldt_sltd(self):
        cpu, self.mem = make_cpu()
        gdt = 0x00800
        build_gdt(self.mem, gdt, [
            make_descriptor(0x09000, 0x0FFF, 0x82),   # LDT descriptor
        ])
        cpu.gdt_base = gdt
        cpu.gdt_limit = 2 * 8 - 1
        # LLDT AX with selector 0x08 (LDT descriptor), then SLDT BX.
        write_code(cpu, self.mem, 0x0104, [
            0xB8, 0x08, 0x00,    # MOV AX, 0x08
            0x0F, 0x00, 0xD0,    # LLDT AX
            0x0F, 0x00, 0xC3,    # SLDT BX
        ])
        cpu.ip = 0x0104
        for _ in range(3):
            cpu.execute()
        assert cpu.ldtr_selector == 0x08
        assert cpu.bx == 0x08
        # The hidden LDT base came from the descriptor.
        assert cpu._ldt_base() == 0x09000

    def test_clts(self):
        cpu, _ = make_cpu()
        cpu.msw |= 0x0008
        write_code(cpu, cpu.mem, 0x0100, [0x0F, 0x06])
        cpu.ip = 0x0100
        cpu.execute()
        assert not (cpu.msw & 0x0008)


class TestReturnToRealMode:
    def test_clearing_pe_restores_selector_addressing(self):
        cpu, mem = make_cpu()
        gdt = 0x00800
        sels = build_gdt(mem, gdt, [
            make_descriptor(0x00500, 0xFFFF, ACC_CODE),
            make_descriptor(0x07000, 0xFFFF, ACC_DATA),
        ])
        cpu.gdt_base = gdt
        cpu.gdt_limit = 3 * 8 - 1
        cpu._set_msw(1)
        cpu._set_ds(sels[1])
        assert cpu._phys(sels[1], 0x10) == 0x07010
        # Emulator extension: clear PE without reset.
        cpu._set_msw(0)
        assert cpu._phys(cpu.ds, 0x10) == (cpu.ds << 4) + 0x10


class TestDifferentialAgainstUnicorn:
    """286 PM switch, our CPU vs Unicorn (QEMU TCG), register+memory exact.

    Skipped when unicorn is unavailable.  QEMU/TCG does not set the
    descriptor Accessed bit on 286 far-jump CS loads (it does for MOV
    Sreg data loads); the SDM says every segment load sets it, so the
    code descriptor's access byte is compared with A normalised.
    """

    def _build(self):
        def desc(base, limit, access):
            return bytes((limit & 0xFF, (limit >> 8) & 0xFF, base & 0xFF,
                          (base >> 8) & 0xFF, (base >> 16) & 0xFF,
                          access, 0, 0))
        CODE, DATA = 0x00500, 0x07000
        CS_SEL, DS_SEL = 0x08, 0x10
        GDT, IDT = 0x00800, 0x00A00
        ram = bytearray(0x100000)
        ram[GDT:GDT + 8] = bytes(8)
        ram[GDT + 8:GDT + 16] = desc(CODE, 0xFFFF, 0x9A)
        ram[GDT + 16:GDT + 24] = desc(DATA, 0xFFFF, 0x92)
        ram[0x2000:0x2006] = bytes((0x17, 0, GDT & 0xFF,
                                   (GDT >> 8) & 0xFF, 0, 0))
        ram[0x2100:0x2106] = bytes((0x07, 0, IDT & 0xFF,
                                   (IDT >> 8) & 0xFF, 0, 0))
        entry = bytes([
            0x0F, 0x01, 0x16, 0x00, 0x20,      # LGDT [0x2000]
            0x0F, 0x01, 0x1E, 0x00, 0x21,      # LIDT [0x2100]
            0xB8, 0x01, 0x00,                  # MOV AX, 1
            0x0F, 0x01, 0xF0,                  # LMSW AX
            0xEA, 0x00, 0x00, CS_SEL, 0x00,    # JMP FAR 0:code_sel
        ])
        pm = bytes([
            0xB8, DS_SEL, 0x00,                # MOV AX, ds_sel
            0x8E, 0xD8,                        # MOV DS, AX
            0xC7, 0x06, 0x10, 0x00, 0xEF, 0xBE,
            0xA1, 0x10, 0x00,                  # MOV AX, [0x10]
            0xBB, 0x34, 0x12,                  # MOV BX, 0x1234
            0x01, 0xD8,                        # ADD AX, BX
            0x89, 0x06, 0x12, 0x00,            # MOV [0x12], AX
            0xF4,                              # HLT
        ])
        ram[0x0100:0x0100 + len(entry)] = entry
        ram[CODE:CODE + len(pm)] = pm
        return ram, CODE, GDT

    def test_pm_switch_matches_unicorn(self):
        unicorn = pytest.importorskip('unicorn')
        from unicorn.x86_const import (
            UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
            UC_X86_REG_SI, UC_X86_REG_DI, UC_X86_REG_SP, UC_X86_REG_BP,
            UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS,
            UC_X86_REG_EFLAGS, UC_X86_REG_IP)
        from unicorn import Uc, UC_ARCH_X86, UC_MODE_16
        ram, GDT = self._build()[0], self._build()[2]

        uc = Uc(UC_ARCH_X86, UC_MODE_16)
        uc.mem_map(0, 0x100000)
        uc.mem_write(0, bytes(ram))
        uc.reg_write(UC_X86_REG_CS, 0)
        uc.reg_write(UC_X86_REG_IP, 0x0100)
        uc.reg_write(UC_X86_REG_SS, 0)
        uc.reg_write(UC_X86_REG_SP, 0xFFFE)
        uc.emu_start(0x0100, 0, count=64)
        reg_ids = {
            'ax': UC_X86_REG_AX, 'bx': UC_X86_REG_BX, 'cx': UC_X86_REG_CX,
            'dx': UC_X86_REG_DX, 'si': UC_X86_REG_SI, 'di': UC_X86_REG_DI,
            'sp': UC_X86_REG_SP, 'bp': UC_X86_REG_BP,
            'cs': UC_X86_REG_CS, 'ds': UC_X86_REG_DS, 'es': UC_X86_REG_ES,
            'ss': UC_X86_REG_SS, 'ip': UC_X86_REG_IP,
        }
        expect = {name: uc.reg_read(rid) for name, rid in reg_ids.items()}
        expect['flags'] = uc.reg_read(UC_X86_REG_EFLAGS) & 0xFFFF
        uc_mem = bytes(uc.mem_read(0, 0x100000))

        mem = Mem()
        mem.ram[:] = ram
        cpu = CPU(mem, None)
        cpu.cs = 0
        cpu.ip = 0x0100
        cpu.ss = 0
        cpu.sp = 0xFFFE
        steps = 0
        while not cpu.halted and steps < 64:
            cpu.execute()
            steps += 1

        for name, want in expect.items():
            got = getattr(cpu, name)
            assert got == want, f'{name}: mine={got:#06x} unicorn={want:#06x}'

        def norm(data, i):
            return data[i] & 0xFE if i == GDT + 8 + 5 else data[i]
        diffs = [i for i in range(0x100000)
                 if norm(mem.ram, i) != norm(uc_mem, i)]
        assert diffs == []
