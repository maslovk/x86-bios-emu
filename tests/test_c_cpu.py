"""Smoke tests for the optional Unicorn-backed CPU implementation."""

import pytest

pytest.importorskip('unicorn')

from c_cpu_native import CCPU


def test_native_backend_executes_mov_and_hlt(memory, io_ports):
    memory.ram[0x100:0x104] = bytes((0xB8, 0x34, 0x12, 0xF4))
    cpu = CCPU(memory, io_ports)
    cpu.cs = cpu.ds = cpu.es = cpu.ss = 0
    cpu.ip = 0x100
    cpu.sp = 0x7000

    assert cpu.execute_many(16) > 0
    assert cpu.ax == 0x1234
    assert cpu.ip == 0x104
    assert cpu.halted


def test_native_backend_keeps_python_memory_in_sync(memory, io_ports):
    # MOV [0100h],AX; HLT
    memory.ram[0x200:0x206] = bytes((0xB8, 0xCD, 0xAB, 0xA3, 0x00, 0x01))
    memory.ram[0x206] = 0xF4
    cpu = CCPU(memory, io_ports)
    cpu.cs = cpu.ds = cpu.es = cpu.ss = 0
    cpu.ip = 0x200
    cpu.sp = 0x7000

    cpu.execute_many(16)

    assert memory.read_word(0x100) == 0xABCD
    assert cpu.halted
