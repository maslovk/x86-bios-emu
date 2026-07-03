"""Trap-flag single-step tests (Phase D feature 3).

Verifies: TF set -> the next instruction executes then vectors through IVT 1;
a POPF that sets TF is deferred one instruction (the SDM delay); INT clears
TF in the handler (so no spurious INT 1 follows the INT).  Uses the bare CPU
whose _do_interrupt reads the IVT directly.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


HANDLER_SEG = 0xF000
HANDLER_OFF = 0x0100
CODE_SEG = 0x1000


def _install_iret_handler(mem, int_n):
    """Put IRET at 0xF000:0100 and point IVT[int_n] at it."""
    mem.write_byte((HANDLER_SEG << 4) + HANDLER_OFF, 0xCF)   # IRET
    mem.write_word(int_n * 4, HANDLER_OFF)
    mem.write_word(int_n * 4 + 2, HANDLER_SEG)


def _run_one(cpu, mem, code_bytes, ip=0):
    for i, b in enumerate(code_bytes):
        mem.write_byte((CODE_SEG << 4) + ip + i, b)
    cpu.cs = CODE_SEG
    cpu.ip = ip
    cpu.execute()


def test_tf_vectors_through_int1(cpu, memory):
    _install_iret_handler(memory, 1)
    cpu.tf = True
    _run_one(cpu, memory, [0x90])          # NOP
    assert cpu.cs == HANDLER_SEG
    assert cpu.ip == HANDLER_OFF


def test_tf_popf_delays_one_instruction(cpu, memory):
    _install_iret_handler(memory, 1)
    # Stack a flags word with TF set; POPF will load it.
    cpu.ss = 0
    cpu.sp = 0xFFFC
    memory.write_word(0xFFFC, 0x0102)     # bit1 (reserved-1) | TF
    # POPF sets TF but must NOT trap itself (one-instruction delay).
    _run_one(cpu, memory, [0x9D])         # POPF
    assert cpu.tf is True
    assert cpu.cs == CODE_SEG and cpu.ip == 0x0001   # not vectored yet
    # The FOLLOWING instruction is where the trap fires.
    cpu.execute()                         # NOP at CODE_SEG:0001
    assert cpu.cs == HANDLER_SEG
    assert cpu.ip == HANDLER_OFF


def test_int_clears_tf_in_handler(cpu, memory):
    _install_iret_handler(memory, 3)
    cpu.tf = True
    _run_one(cpu, memory, [0xCC])         # INT 3
    assert cpu.tf is False                # INT cleared TF
    assert cpu.cs == HANDLER_SEG          # vectored to INT 3 handler
    assert cpu.ip == HANDLER_OFF
