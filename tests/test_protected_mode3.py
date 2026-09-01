"""Unit tests for cpu.py — 286 protected mode, milestone 3: tasks.

Covers hardware task switching: direct JMP/CALL to a TSS selector, task
gates (GDT and IDT), the busy-bit discipline, back-link/NT nesting, and
IRET-with-NT task returns.
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


R0_CODE, R0_DATA, R0_STACK = 0x08, 0x10, 0x18
R0_CODE_BASE, R0_DATA_BASE, R0_STACK_BASE = 0x00500, 0x07000, 0x09000
TASK_GATE = 0x20
TSS_A, TSS_B = 0x28, 0x30
TSS_A_BASE, TSS_B_BASE = 0x0E000, 0x0E400
GDT, IDT = 0x00800, 0x00A00


def tss_image(mem, base, *, ip, cs=R0_CODE, ss=R0_STACK, sp=0x9F00,
              ds=R0_DATA, es=R0_DATA, ax=0, bx=0, cx=0, dx=0,
              si=0, di=0, bp=0, flags=0x0002, backlink=0):
    # Header: back-link + SP0/SS0 + SP1/SS1 + SP2/SS2 = 7 words.
    words = [backlink, 0, 0, 0, 0, 0, 0,
             ip, flags, ax, cx, dx, bx, sp, bp, si, di,
             es, cs, ss, ds, 0]                # + LDT slot
    for i, w in enumerate(words):
        mem.write_word(base + 2 * i, w)


def build_tasks(cpu, mem):
    gdt = [
        bytes(8),
        desc(R0_CODE_BASE, 0xFFFF, 0x9A),      # 0x08 code
        desc(R0_DATA_BASE, 0xFFFF, 0x92),      # 0x10 data
        desc(R0_STACK_BASE, 0xFFFF, 0x92),     # 0x18 stack
        # 0x20 task gate → TSS_B, DPL 0
        bytes((0x00, 0x00, TSS_B, 0x00, 0x00, 0x85, 0, 0)),
        desc(TSS_A_BASE, 0x0067, 0x83),        # 0x28 TSS A (busy: type 3)
        desc(TSS_B_BASE, 0x0067, 0x81),        # 0x30 TSS B (available)
    ]
    for i, d in enumerate(gdt):
        mem.ram[GDT + i * 8:GDT + i * 8 + 8] = d
    cpu.gdt_base = GDT
    cpu.gdt_limit = len(gdt) * 8 - 1
    cpu.idt_base = IDT
    cpu.idt_limit = 0x40 * 8 - 1
    # Task A image: runs at ring 0 in the shared code segment.
    tss_image(mem, TSS_A_BASE, ip=0x0100)
    # Task B image: distinct register signature.
    tss_image(mem, TSS_B_BASE, ip=0x0200, ax=0x4242, bx=0x2424,
              cx=0x1111, dx=0x7777, si=0x1234, di=0x5678)
    # Enter PM as task A.
    cpu._set_msw(1)
    cpu._load_sreg('cs', R0_CODE)
    cpu.ip = 0x0100
    cpu._set_ss(R0_STACK)
    cpu.sp = 0x9F00
    cpu._set_ds(R0_DATA)
    cpu.tr_selector = TSS_A
    cpu._desc_cache[TSS_A] = (TSS_A_BASE, 0x67, 0x83, GDT + 5 * 8)
    return cpu, mem


class TestTaskSwitch:
    def test_jmp_to_tss_selector(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        # Task A code: MOV AX, 0xAAAA; JMP FAR 0:0x30 (TSS B)
        write_code(mem, R0_CODE_BASE + 0x0100, [
            0xB8, 0xAA, 0xAA,
            0xEA, 0x00, 0x00, TSS_B, 0x00,
            0xEB, 0xFE,
        ])
        write_code(mem, R0_CODE_BASE + 0x0200, [0xEB, 0xFE])
        cpu.execute()                     # MOV AX
        cpu.execute()                     # JMP FAR → task switch
        # Register set loaded from TSS B.
        assert cpu.ax == 0x4242
        assert cpu.bx == 0x2424
        assert cpu.cx == 0x1111
        assert cpu.dx == 0x7777
        assert cpu.si == 0x1234
        assert cpu.di == 0x5678
        assert cpu.ip == 0x0200
        assert cpu.cs == R0_CODE
        # Task A's state was saved to its TSS.
        assert mem.read_word(TSS_A_BASE + 0x0E) == 0x0108   # IP after JMP
        assert mem.read_word(TSS_A_BASE + 0x12) == 0xAAAA   # AX
        # Busy bits: A (0x28) cleared, B (0x30) set.
        assert mem.ram[GDT + 5 * 8 + 5] == 0x81
        assert mem.ram[GDT + 6 * 8 + 5] == 0x83
        assert cpu.tr_selector == TSS_B

    def test_call_through_task_gate_nests_and_iret_returns(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        # Task A: MOV AX,0xAAAA; CALL FAR 0:0x20 (task gate) ; HLT pad
        write_code(mem, R0_CODE_BASE + 0x0100, [
            0xB8, 0xAA, 0xAA,
            0x9A, 0x00, 0x00, TASK_GATE, 0x00,
            0xEB, 0xFE,
        ])
        # Task B: IRET (with NT) → back to task A
        write_code(mem, R0_CODE_BASE + 0x0200, [0xCF, 0xEB, 0xFE])
        cpu.execute()                     # MOV AX
        cpu.execute()                     # CALL FAR through the gate
        # We are now task B, nested.
        assert cpu.tr_selector == TSS_B
        assert cpu.ax == 0x4242           # B's image
        assert cpu.flags & 0x4000         # NT set
        assert mem.read_word(TSS_B_BASE) == TSS_A          # back-link
        # A stayed busy (nested), B became busy.
        assert mem.ram[GDT + 5 * 8 + 5] == 0x83
        assert mem.ram[GDT + 6 * 8 + 5] == 0x83
        cpu.execute()                     # IRET with NT → task return
        assert cpu.tr_selector == TSS_A
        assert cpu.ax == 0xAAAA           # A's saved AX
        assert cpu.ip == 0x0108           # after the CALL FAR
        assert not (cpu.flags & 0x4000)   # NT clear in A
        # After the return: A busy again (it is running), B available.
        assert mem.ram[GDT + 5 * 8 + 5] == 0x83
        assert mem.ram[GDT + 6 * 8 + 5] == 0x81

    def test_int_through_idt_task_gate(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        # IDT gate 0x22: task gate → TSS B.
        mem.ram[IDT + 0x22 * 8:IDT + 0x22 * 8 + 8] = bytes(
            (0x00, 0x00, TSS_B, 0x00, 0x00, 0x85, 0, 0))
        write_code(mem, R0_CODE_BASE + 0x0100, [
            0xB8, 0xAA, 0xAA,
            0xCD, 0x22,
            0xEB, 0xFE,
        ])
        write_code(mem, R0_CODE_BASE + 0x0200, [0xEB, 0xFE])
        cpu.execute()                     # MOV AX
        cpu.execute()                     # INT 22h → task switch
        assert cpu.tr_selector == TSS_B
        assert cpu.ax == 0x4242
        assert cpu.flags & 0x4000         # nested via interrupt
        assert mem.read_word(TSS_B_BASE) == TSS_A
        assert mem.read_word(TSS_A_BASE + 0x0E) == 0x0105   # saved IP

    def test_jmp_to_busy_tss_faults(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        # Mark TSS B busy, then JMP to it.
        mem.ram[GDT + 6 * 8 + 5] = 0x83
        write_code(mem, R0_CODE_BASE + 0x0100,
                   [0xEA, 0x00, 0x00, TSS_B, 0x00])
        cpu.execute()
        assert cpu.halted                 # #GP: no usable gate 13

    def test_ltr_marks_task_busy(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        write_code(mem, R0_CODE_BASE + 0x0100, [
            0xB8, TSS_B, 0x00,            # MOV AX, TSS_B
            0x0F, 0x00, 0xD8,             # LTR AX
        ])
        cpu.ip = 0x0100
        cpu.execute(); cpu.execute()
        assert cpu.tr_selector == TSS_B
        assert mem.ram[GDT + 6 * 8 + 5] == 0x83   # busy

    def test_iret_without_nt_pops_normally(self):
        cpu, mem = make_cpu()
        build_tasks(cpu, mem)
        assert not (cpu.flags & 0x4000)
        # Build a plain interrupt frame: IRET must return through it
        # (no task switch) even while a TR is loaded.
        write_code(mem, R0_CODE_BASE + 0x0100, [
            0x68, 0x02, 0x00,             # PUSH flags-ish
            0x68, R0_CODE, 0x00,          # PUSH CS
            0x68, 0x20, 0x01,             # PUSH 0x0120
            0xCF,                         # IRET → 0x0120
        ])
        write_code(mem, R0_CODE_BASE + 0x0120, [0xEB, 0xFE])
        for _ in range(4):
            cpu.execute()
        assert cpu.tr_selector == TSS_A   # no task switch happened
        assert cpu.ip == 0x0120
        assert not cpu.halted


class TestWordBoundaryLimits:
    """A word operand must fit entirely inside the segment limit."""

    def _machine_with_tight_data(self, limit):
        cpu, mem = make_cpu()
        mem.ram[0x808:0x810] = desc(0x00500, 0xFFFF, 0x9A)   # code
        mem.ram[0x810:0x818] = desc(0x07000, limit, 0x92)    # data
        cpu.gdt_base = GDT
        cpu.gdt_limit = 3 * 8 - 1
        cpu._set_msw(1)
        cpu._set_cs(0x08)
        cpu.ip = 0
        cpu._set_ss(0x10)          # the data descriptor doubles as stack
        cpu.sp = 0x0100
        cpu._set_ds(0x10)
        return cpu, mem

    def test_word_read_straddling_limit_faults(self):
        cpu, mem = self._machine_with_tight_data(0x00FF)
        write_code(mem, 0x00500,
                   [0xA1, 0xFF, 0x00])          # MOV AX, [0x00FF]
        cpu.execute()
        assert cpu.halted                       # byte 2 at 0x0100 > limit

    def test_word_read_within_limit_succeeds(self):
        cpu, mem = self._machine_with_tight_data(0x00FF)
        mem.write_word(0x07000 + 0x00FE, 0xBEEF)
        write_code(mem, 0x00500,
                   [0xA1, 0xFE, 0x00, 0xEB, 0xFE])
        cpu.execute()
        assert not cpu.halted
        assert cpu.ax == 0xBEEF

    def test_word_write_straddling_limit_faults(self):
        cpu, mem = self._machine_with_tight_data(0x0100)
        write_code(mem, 0x00500,
                   [0xA3, 0x00, 0x01])          # MOV [0x0100], AX
        cpu.execute()
        assert cpu.halted

    def test_movsw_bounds_checked(self):
        cpu, mem = self._machine_with_tight_data(0x00FF)
        cpu.es = 0x10
        cpu.si = 0x00FF                         # byte 2 lands at 0x0100
        cpu.di = 0x0040
        write_code(mem, 0x00500, [0xA5])        # MOVSW
        cpu.execute()
        assert cpu.halted                       # source word straddles
