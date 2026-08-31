"""Unit tests for cpu.py — 286 protected mode, milestone 2.

Covers CPL tracking and IOPL enforcement (CLI/STI/IN/OUT/PUSHF/POPF/IRET
gating), segment-limit faults including expand-down data, and ring
transitions: call gates (with parameter copying), interrupt-gate entry to
an inner ring with the TSS stack switch, and outer-ring RETF/IRET returns.
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


def desc(base, limit, access):
    return bytes((limit & 0xFF, (limit >> 8) & 0xFF, base & 0xFF,
                  (base >> 8) & 0xFF, (base >> 16) & 0xFF, access, 0, 0))


def write_code(mem, addr, code):
    for i, b in enumerate(code):
        mem.write_byte(addr + i, b)


# Selector layout (GDT @ 0x800): null, ring0 code 0x08, ring0 data 0x10,
# ring0 stack 0x18, ring3 code 0x20, ring3 data 0x28, call gate 0x30,
# TSS 0x38.
R0_CODE, R0_DATA, R0_STACK = 0x08, 0x10, 0x18
R3_CODE, R3_DATA = 0x20, 0x28
CALL_GATE, TSS_SEL = 0x30, 0x38
R0_CODE_BASE, R0_DATA_BASE = 0x00500, 0x07000
R0_STACK_BASE, R3_CODE_BASE, R3_DATA_BASE = 0x09000, 0x0B000, 0x0D000
TSS_BASE = 0x0E000
GDT, IDT = 0x00800, 0x00A00


def build_machine(cpu, mem, *, r3_data_limit=0xFFFF):
    """GDT/IDT/TSS with ring-0 and ring-3 segments, gates, and a TSS."""
    gdt_entries = [
        bytes(8),
        desc(R0_CODE_BASE, 0xFFFF, 0x9A),        # 0x08 ring0 code
        desc(R0_DATA_BASE, 0xFFFF, 0x92),        # 0x10 ring0 data
        desc(R0_STACK_BASE, 0xFFFF, 0x92),       # 0x18 ring0 stack
        desc(R3_CODE_BASE, 0xFFFF, 0xFA),        # 0x20 ring3 code
        desc(R3_DATA_BASE, r3_data_limit, 0xF2),  # 0x28 ring3 data
        # 0x30 call gate: target 0x08:0x0040, 2 param words, DPL 3
        bytes((0x40, 0x00, R0_CODE, 0x00, 0x02, 0xE4, 0, 0)),
        desc(TSS_BASE, 0x0067, 0x81),            # 0x38 TSS
    ]
    for i, d in enumerate(gdt_entries):
        mem.ram[GDT + i * 8:GDT + i * 8 + 8] = d
    cpu.gdt_base = GDT
    cpu.gdt_limit = len(gdt_entries) * 8 - 1
    cpu.idt_base = IDT
    cpu.idt_limit = 0x40 * 8 - 1
    # TSS: SS0:SP0 for the ring-0 stack.
    mem.write_word(TSS_BASE + 2, 0x9F00)         # SP0
    mem.write_word(TSS_BASE + 4, R0_STACK)       # SS0
    cpu.tr_selector = TSS_SEL
    cpu._desc_cache[TSS_SEL] = (TSS_BASE, 0x67, 0x81, GDT + 7 * 8)
    # Switch to PM directly at ring 3.
    cpu._set_msw(1)
    cpu._set_cs(R3_CODE | 3)                     # CPL 3
    cpu.ip = 0
    cpu._set_ss(R3_DATA | 3)
    cpu.sp = 0xFF00
    cpu._set_ds(R3_DATA | 3)
    return cpu, mem


class TestIOPL:
    def test_cli_at_outer_ring_faults(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        assert cpu._cpl == 3
        write_code(mem, R3_CODE_BASE, [0xFA])       # CLI
        cpu.execute()
        assert cpu.halted                          # #GP, no gate 13

    def test_cli_allowed_when_iopl_covers_cpl(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        cpu.flags = (cpu.flags & ~0x3000) | 0x3000  # IOPL 3
        write_code(mem, R3_CODE_BASE, [0xFA, 0xEB, 0xFE])   # CLI; JMP $
        cpu.execute()                              # CLI
        assert not cpu.halted
        assert not cpu.if_flag

    def test_out_at_outer_ring_faults(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        write_code(mem, R3_CODE_BASE, [0xE6, 0x61])  # OUT 61h, AL
        cpu.execute()
        assert cpu.halted

    def test_pushf_from_outer_ring_clears_iopl(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        cpu.flags = (cpu.flags & ~0x3000) | 0x1000  # IOPL 1, CPL 3
        write_code(mem, R3_CODE_BASE, [0x9C])       # PUSHF
        cpu.execute()
        pushed = mem.read_word(R3_DATA_BASE + 0xFEFE)
        assert pushed == 0x0002      # IOPL field cleared for the outer ring

    def test_popf_outer_ring_cannot_change_iopl_or_if(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        cpu.flags = 0x0202                          # IOPL 0, IF set
        mem.write_word(R3_DATA_BASE + 0x0200, 0x3202)  # wants IOPL 3, IF on
        write_code(mem, R3_CODE_BASE,
                   [0xFF, 0x36, 0x00, 0x02,         # PUSH [0x0200]
                    0x9D])                          # POPF
        cpu.execute(); cpu.execute()
        assert not cpu.halted
        assert ((cpu.flags >> 12) & 3) == 0         # IOPL unchanged
        assert cpu.if_flag                          # IF unchanged

    def test_popf_ring0_loads_full_flags(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        cpu._set_cs(R0_CODE)          # enter ring 0
        assert cpu._cpl == 0
        cpu.if_flag = True
        cpu._pop_flags(0x3202)
        assert cpu.flags == 0x3202


class TestCPL:
    def test_cpl_follows_cs_selector_rpl(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        assert cpu._cpl == 3

    def test_conforming_code_inherits_cpl(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # GDT slot 6 is the call gate; add a conforming code descriptor
        # in the (unused) slot after the TSS.
        mem.ram[GDT + 8 * 8:GDT + 9 * 8] = desc(0x0F000, 0xFFFF, 0xFE)
        cpu.gdt_limit = 9 * 8 - 1
        cpu._set_cs(8 * 8 | 3)                      # conforming, RPL 3
        assert cpu._cpl == 3
        cpu._set_cs(8 * 8)                          # same descriptor, RPL 0
        assert cpu._cpl == 3                        # inherited, not RPL 0


class TestSegmentLimits:
    def test_data_access_beyond_limit_faults(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem, r3_data_limit=0x00FF)
        write_code(mem, R3_CODE_BASE,
                   [0xA0, 0x00, 0x01])              # MOV AL, [0x0100]
        cpu.execute()
        assert cpu.halted                           # #GP, no gate

    def test_data_access_at_limit_succeeds(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem, r3_data_limit=0x0100)
        mem.write_byte(R3_DATA_BASE + 0x0100, 0x5A)
        write_code(mem, R3_CODE_BASE,
                   [0xA0, 0x00, 0x01, 0xEB, 0xFE])  # MOV AL, [0x0100]; JMP $
        cpu.execute()
        assert not cpu.halted
        assert cpu.al == 0x5A

    def test_expand_down_segment_rejects_low_offsets(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # Make the ring-3 data descriptor expand-down with limit 0x00FF:
        # valid offsets are 0x0100..0xFFFF.
        mem.ram[GDT + 5 * 8:GDT + 5 * 8 + 6] = bytes(
            (0xFF, 0x00)) + mem.ram[GDT + 5 * 8:GDT + 5 * 8 + 2] + b''
        mem.ram[GDT + 5 * 8 + 0] = 0xFF
        mem.ram[GDT + 5 * 8 + 1] = 0x00
        mem.ram[GDT + 5 * 8 + 5] = 0xF6             # P DPL3 S=1 ED RW
        cpu._set_ds(R3_DATA | 3)                    # reload the cache
        write_code(mem, R3_CODE_BASE,
                   [0xA0, 0x80, 0x00])              # MOV AL, [0x0080]
        cpu.execute()
        assert cpu.halted

    def test_expand_down_segment_allows_high_offsets(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        mem.ram[GDT + 5 * 8 + 0] = 0xFF             # limit 0x00FF
        mem.ram[GDT + 5 * 8 + 1] = 0x00
        mem.ram[GDT + 5 * 8 + 5] = 0xF6
        cpu._set_ds(R3_DATA | 3)
        mem.write_byte(R3_DATA_BASE + 0x0100, 0xA5)
        write_code(mem, R3_CODE_BASE,
                   [0xA0, 0x00, 0x01, 0xEB, 0xFE])  # MOV AL, [0x0100]; JMP $
        cpu.execute()
        assert not cpu.halted
        assert cpu.al == 0xA5

    def test_stack_push_below_ss_limit_faults(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # Ring-3 stack descriptor (GDT slot 5 shared with data): give the
        # SS cache a tighter limit by reloading from a dedicated entry.
        mem.ram[GDT + 8 * 8:GDT + 9 * 8] = desc(R3_DATA_BASE, 0xFF00, 0xF2)
        cpu.gdt_limit = 9 * 8 - 1
        cpu._set_ss(8 * 8 | 3)
        cpu.sp = 0xFFF2
        write_code(mem, R3_CODE_BASE, [0x50])       # PUSH AX
        cpu.execute()
        assert cpu.halted                           # #SS


class TestCallGates:
    def test_call_gate_ring3_to_ring0_with_params(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # Callee (ring 0, base 0x500 + 0x40): read param0 into AX, store
        # it at ring0-data+0x100, RETF 4 (discard the 2 param words).
        write_code(mem, R0_CODE_BASE + 0x40, [
            0x89, 0xE5,                 # MOV BP, SP
            0x8B, 0x46, 0x08,           # MOV AX, [BP+8]  (param 0)
            0x50,                       # PUSH AX
            0xB8, R0_DATA, 0x00,        # MOV AX, ring0 data sel
            0x8E, 0xD8,                 # MOV DS, AX
            0x58,                       # POP AX
            0xA3, 0x00, 0x01,           # MOV [0x0100], AX
            0xCA, 0x04, 0x00,           # RETF 4
        ])
        # Caller (ring 3): push params, CALL FAR to the gate.
        write_code(mem, R3_CODE_BASE, [
            0x68, 0x33, 0x33,           # PUSH 0x3333 (param 1)
            0x68, 0x22, 0x22,           # PUSH 0x2222 (param 0)
            0x9A, 0x00, 0x00, CALL_GATE, 0x00,   # CALL FAR 0:0x30
            0xEB, 0xFE,                 # JMP $
        ])
        cpu.execute(); cpu.execute(); cpu.execute()   # pushes + call gate
        assert cpu._cpl == 0
        assert cpu.cs == R0_CODE
        # Run the callee through its store (7 instructions).
        for _ in range(7):
            cpu.execute()
        assert mem.read_word(R0_DATA_BASE + 0x0100) == 0x2222
        cpu.execute()                                  # RETF 4
        assert cpu._cpl == 3
        assert cpu.cs == (R3_CODE | 3)
        assert cpu.ip == 0x0B                          # after CALL FAR
        # Params discarded: SP back past both words.
        assert cpu.sp == 0xFF00

    def test_gate_dpl_too_inner_faults(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        mem.ram[GDT + 6 * 8 + 5] = 0x84               # gate DPL 0
        write_code(mem, R3_CODE_BASE,
                   [0x9A, 0x00, 0x00, CALL_GATE, 0x00])
        cpu.execute()
        assert cpu.halted

    def test_jmp_through_gate_same_ring(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # Retarget the gate to ring-3 code, offset 0x20.
        mem.ram[GDT + 6 * 8 + 0:GDT + 6 * 8 + 4] = bytes(
            (0x20, 0x00, R3_CODE, 0x00))
        write_code(mem, R3_CODE_BASE + 0x20, [0xEB, 0xFE])   # JMP $
        write_code(mem, R3_CODE_BASE,
                   [0xEA, 0x00, 0x00, CALL_GATE, 0x00])   # JMP FAR gate
        cpu.execute()
        assert not cpu.halted
        assert cpu._cpl == 3
        assert cpu.ip == 0x20


class TestInterruptRingSwitch:
    def build_int3_machine(self):
        cpu, mem = make_cpu()
        build_machine(cpu, mem)
        # IDT gate 3 (trap, DPL 3) → ring0 code at offset 0x80.
        gate = bytes((0x80, 0x00, R0_CODE, 0x00, 0x00, 0xE7, 0, 0))
        mem.ram[IDT + 3 * 8:IDT + 3 * 8 + 8] = gate
        # Handler: switch to ring-0 data, store the marker, IRET.
        write_code(mem, R0_CODE_BASE + 0x80, [
            0xBB, R0_DATA, 0x00,        # MOV BX, ring0 data sel
            0x8E, 0xDB,                 # MOV DS, BX
            0xA3, 0x00, 0x02,           # MOV [0x0200], AX  (marker)
            0xCF,                       # IRET
        ])
        return cpu, mem

    def test_int_from_ring3_switches_stacks_and_returns(self):
        cpu, mem = self.build_int3_machine()
        outer_ss, outer_sp = cpu.ss, cpu.sp
        write_code(mem, R3_CODE_BASE, [
            0xB8, 0x99, 0x99,           # MOV AX, 0x9999
            0xCC,                       # INT3
            0xEB, 0xFE,                 # JMP $
        ])
        cpu.execute()                   # MOV AX
        cpu.execute()                   # INT3 → gate + stack switch
        assert cpu._cpl == 0
        assert cpu.cs == R0_CODE
        assert cpu.ss == R0_STACK       # TSS SS0 loaded
        assert cpu.sp == 0x9F00 - 10    # ip+cs+flags+saved SS:SP
        for _ in range(4):
            cpu.execute()               # DS load, store, IRET
        assert cpu._cpl == 3
        assert cpu.cs == (R3_CODE | 3)
        assert cpu.ss == outer_ss
        assert cpu.sp == outer_sp
        assert mem.read_word(R0_DATA_BASE + 0x0200) == 0x9999

    def test_saved_outer_stack_is_below_return_frame(self):
        cpu, mem = self.build_int3_machine()
        outer_ss, outer_sp = cpu.ss, cpu.sp
        write_code(mem, R3_CODE_BASE, [0xCC, 0xEB, 0xFE])
        cpu.execute()                   # INT3
        # Stack (grows down): ip, cs, flags, saved_sp, saved_ss
        base = R0_STACK_BASE + cpu.sp
        assert mem.read_word(base + 0) == 0x0001   # INT3 is 1 byte
        assert mem.read_word(base + 2) == (R3_CODE | 3)
        assert mem.read_word(base + 6) == outer_sp
        assert mem.read_word(base + 8) == outer_ss


class TestDifferentialRingTransitions:
    """PM execution + trap-gate INT round-trips, our CPU vs Unicorn (QEMU
    TCG), register- and memory-exact.

    Unicorn's 16-bit translator cannot decode ``LTR``, programming TR
    out-of-band crashes QEMU when a ring-0 stack switch consults it, and
    its same-ring call gates push a 32-bit far pointer that its own 16-bit
    RETF cannot consume — so the differential stays within plain ring-0
    execution and interrupt gates; privilege transitions and call gates
    are covered byte-exactly by the unit tests above.

    Normalised bytes: the descriptor Accessed bit (QEMU skips it on some
    CS loads).
    """

    def _build(self):
        ram = bytearray(0x100000)
        gdt = [
            bytes(8),
            desc(R0_CODE_BASE, 0xFFFF, 0x9A),        # 0x08
            desc(R0_DATA_BASE, 0xFFFF, 0x92),        # 0x10
            desc(R0_STACK_BASE, 0xFFFF, 0x92),       # 0x18
            desc(R3_CODE_BASE, 0xFFFF, 0xFA),        # 0x20
            desc(R3_DATA_BASE, 0xFFFF, 0xF2),        # 0x28
            bytes((0x40, 0x00, R0_CODE, 0x00, 0x02, 0x8C, 0, 0)),  # gate
            desc(TSS_BASE, 0x0067, 0x81),            # 0x38
        ]
        for i, d in enumerate(gdt):
            ram[GDT + i * 8:GDT + i * 8 + 8] = d
        gdt_limit = len(gdt) * 8 - 1
        ram[0x2000:0x2006] = bytes((gdt_limit & 0xFF, gdt_limit >> 8,
                                   GDT & 0xFF, (GDT >> 8) & 0xFF, 0, 0))
        idt_limit = 0x40 * 8 - 1
        ram[0x2100:0x2106] = bytes((idt_limit & 0xFF, idt_limit >> 8,
                                   IDT & 0xFF, (IDT >> 8) & 0xFF, 0, 0))
        # IDT gate 0x21 → ring0 code +0x80, trap gate DPL 0.
        ram[IDT + 0x21 * 8:IDT + 0x21 * 8 + 8] = bytes(
            (0x80, 0x00, R0_CODE, 0x00, 0x00, 0x87, 0, 0))
        # Real-mode entry: LGDT, LIDT, PE, far jump into ring 0.
        entry = bytes([
            0x0F, 0x01, 0x16, 0x00, 0x20,      # LGDT [0x2000]
            0x0F, 0x01, 0x1E, 0x00, 0x21,      # LIDT [0x2100]
            0xB8, 0x01, 0x00,                  # MOV AX, 1
            0x0F, 0x01, 0xF0,                  # LMSW AX
            0xEA, 0x20, 0x00, R0_CODE, 0x00,   # JMP FAR ring0+0x20
        ])
        ram[0x0100:0x0100 + len(entry)] = entry
        # Ring-0 main: stack/data setup, then a mix of arithmetic,
        # memory, string, and stack operations under descriptor-based
        # addressing (Unicorn-16 cannot dispatch IDT gates or LTR, so
        # interrupts and gates stay unit-tested).
        bootstrap = bytes([
            0xB8, R0_STACK, 0x00,              # MOV AX, ring0 stack sel
            0x8E, 0xD0,                        # MOV SS, AX
            0xBC, 0x00, 0x9F,                  # MOV SP, 0x9F00
            0xB8, R0_DATA, 0x00,               # MOV AX, ring0 data sel
            0x8E, 0xD8,                        # MOV DS, AX
            0xB8, R0_DATA, 0x00,               # MOV AX, data sel (ES)
            0x8E, 0xC0,                        # MOV ES, AX
            0xC7, 0x06, 0x00, 0x01, 0xEF, 0xBE,  # MOV [0x0100], 0xBEEF
            0xC7, 0x06, 0x02, 0x01, 0x78, 0x56,  # MOV [0x0102], 0x5678
            0xBE, 0x00, 0x01,                  # MOV SI, 0x0100
            0xBF, 0x10, 0x01,                  # MOV DI, 0x0110
            0xB9, 0x02, 0x00,                  # MOV CX, 2
            0xFC,                              # CLD
            0xF3, 0xA5,                        # REP MOVSW
            0xA1, 0x10, 0x01,                  # MOV AX, [0x0110]
            0x2D, 0x34, 0x12,                  # SUB AX, 0x1234
            0x89, 0x06, 0x20, 0x01,            # MOV [0x0120], AX
            0x68, 0xCD, 0xAB,                  # PUSH 0xABCD
            0x5B,                              # POP BX
            0xEB, 0xFE,                        # JMP $
        ])
        ram[R0_CODE_BASE + 0x20:R0_CODE_BASE + 0x20 + len(bootstrap)] = bootstrap
        return ram

    def test_pm_execution_matches_unicorn(self):
        pytest.importorskip('unicorn')
        from unicorn import Uc, UC_ARCH_X86, UC_MODE_16
        from unicorn.x86_const import (
            UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
            UC_X86_REG_SI, UC_X86_REG_DI, UC_X86_REG_SP, UC_X86_REG_BP,
            UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS,
            UC_X86_REG_EFLAGS, UC_X86_REG_IP)
        ram = self._build()

        uc = Uc(UC_ARCH_X86, UC_MODE_16)
        uc.mem_map(0, 0x100000)
        uc.mem_write(0, bytes(ram))
        uc.reg_write(UC_X86_REG_CS, 0)
        uc.reg_write(UC_X86_REG_IP, 0x0100)
        uc.reg_write(UC_X86_REG_SS, 0)
        uc.reg_write(UC_X86_REG_SP, 0xFFFE)
        uc.emu_start(0x0100, 0, count=200)
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
        while not cpu.halted and steps < 200:
            cpu.execute()
            steps += 1

        assert not cpu.halted
        for name, want in expect.items():
            got = getattr(cpu, name)
            assert got == want, f'{name}: mine={got:#06x} unicorn={want:#06x}'
        # Observable effects: string copy, arithmetic, stack round-trip.
        assert mem.read_word(R0_DATA_BASE + 0x0110) == 0xBEEF
        assert mem.read_word(R0_DATA_BASE + 0x0112) == 0x5678
        assert mem.read_word(R0_DATA_BASE + 0x0120) == (0xBEEF - 0x1234) & 0xFFFF
        assert cpu.bx == 0xABCD
        assert cpu.sp == 0x9F00
        assert cpu._cpl == 0

        # Memory comparison with the descriptor A-bit normalised.
        def norm(data, i):
            b = data[i]
            if i in (GDT + 8 + 5, GDT + 16 + 5, GDT + 32 + 5, GDT + 40 + 5):
                b &= 0xFE
            return b
        diffs = [i for i in range(0x100000)
                 if norm(mem.ram, i) != norm(uc_mem, i)]
        assert diffs == []
