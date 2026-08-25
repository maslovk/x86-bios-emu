"""Smoke tests for the optional Unicorn-backed CPU implementation."""

import pytest

pytest.importorskip('unicorn')

from c_cpu_native import CCPU
from main import Emulator


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


def test_native_backend_does_not_treat_f4_branch_offset_as_hlt(
        memory, io_ports):
    # XOR AX,AX; JNZ -12. The untaken branch leaves IP immediately after an
    # F4 displacement byte, which must not be confused with an executed HLT.
    memory.ram[0x200:0x204] = bytes((0x31, 0xC0, 0x75, 0xF4))
    cpu = CCPU(memory, io_ports)
    cpu.cs = cpu.ds = cpu.es = cpu.ss = 0
    cpu.ip = 0x200
    cpu.sp = 0x7000

    cpu.execute_many(2)

    assert cpu.ip == 0x204
    assert not cpu.halted


def test_native_backend_retries_blocking_keyboard_interrupt():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.mem.ram[0x100:0x103] = bytes((0xCD, 0x16, 0xF4))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.es = emu.cpu.ss = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    emu.cpu.flags = 0x0202
    emu.cpu.ax = 0x1000
    emu._install_bios_interrupt_hook()

    emu.cpu.execute_many(1)

    assert emu.cpu.ip == 0x100
    assert emu.cpu.sp == 0x7000
    assert emu.cpu.ax == 0x1000
    assert emu.cpu.retry_software_interrupt is True

    emu.kbd_ctrl.inject_extended_key(0x50)
    emu.cpu.execute_many(1)

    assert emu.cpu.ip == 0x102
    assert emu.cpu.sp == 0x7000
    assert emu.cpu.ax == 0x5000
    assert emu.cpu.retry_software_interrupt is False


def test_native_backend_chained_bios_status_preserves_zf():
    """Execute PUSHF/CALL FAR/stub IRET, as QBASIC chains INT 16h."""
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    stub_cs, stub_ip = emu.bios.ivt_stubs[0x16]
    emu.mem.ram[0x100:0x107] = bytes((
        0x9C,                         # PUSHF
        0x9A, stub_ip & 0xFF, stub_ip >> 8,
        stub_cs & 0xFF, stub_cs >> 8,  # CALL FAR F000:INT16_STUB
        0xF4,                         # HLT after the stub's IRET
    ))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.es = emu.cpu.ss = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    emu.cpu.flags = 0x0202
    emu.cpu.ax = 0x1100               # Check enhanced key status; empty.
    emu._install_bios_interrupt_hook()

    for _ in range(8):
        if emu.cpu.halted:
            break
        emu.cpu.execute_many(1)

    assert emu.cpu.halted
    assert emu.cpu.ip == 0x107
    assert emu.cpu.sp == 0x7000
    assert emu.cpu.zf is True
    assert emu.cpu.if_flag is True
