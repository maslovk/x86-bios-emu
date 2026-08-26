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


def test_native_backend_routes_ega_vram_writes():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.seq_regs[2] = 0x0F
    # mov ax,A000; mov es,ax; xor di,di; mov al,80h;
    # mov es:[di],al; hlt
    emu.mem.ram[0x100:0x10D] = bytes((
        0xB8, 0x00, 0xA0, 0x8E, 0xC0, 0x31, 0xFF,
        0xB0, 0x80, 0x26, 0x88, 0x05, 0xF4,
    ))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.ss = 0
    emu.cpu.es = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    for _ in range(20):
        emu.cpu.execute_many(1)
        if emu.cpu.halted:
            break
    assert emu.cpu.halted
    assert emu.video.graphics_pixel(0, 0) == 0x0F


def test_native_vga_hook_survives_a_bios_graphics_mode_switch():
    """The hook exports the latch buffer, so mode clears must stay in-place."""
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    if emu.cpu._native_vga_hook is not None:
        emu.video.set_mode(0x10)
        assert emu.video.native_graphics_active[0] == 1
        emu.video.set_mode(3)
        assert emu.video.native_graphics_active[0] == 0


def test_native_backend_uses_reference_path_for_vga_latched_writes():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.seq_regs[2] = 0x0F
    emu.video.gdc_regs[0] = 0x05
    emu.video.gdc_regs[1] = 0x0F
    # mov ax,A000; mov es,ax; xor di,di; mov al,80h;
    # mov es:[di],al; hlt.  Set/reset selects colour 5, not CPU-data 8.
    emu.mem.ram[0x100:0x10D] = bytes((
        0xB8, 0x00, 0xA0, 0x8E, 0xC0, 0x31, 0xFF,
        0xB0, 0x80, 0x26, 0x88, 0x05, 0xF4,
    ))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.ss = 0
    emu.cpu.es = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    for _ in range(20):
        emu.cpu.execute_many(1)
        if emu.cpu.halted:
            break
    assert emu.cpu.halted
    assert emu.video.graphics_pixel(0, 0) == 0x05


def test_native_backend_applies_masked_destination_vga_write():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.seq_regs[2] = 0x0F
    for plane in emu.video.graphics_planes:
        plane[0] = 0xFF
    emu.video.gdc_regs[0] = 0x05
    emu.video.gdc_regs[1] = 0x0F
    emu.video.gdc_regs[8] = 0x80
    # mov ax,A000; mov es,ax; xor di,di; mov al,0; mov es:[di],al; hlt
    emu.mem.ram[0x100:0x10D] = bytes((
        0xB8, 0x00, 0xA0, 0x8E, 0xC0, 0x31, 0xFF,
        0xB0, 0x00, 0x26, 0x88, 0x05, 0xF4,
    ))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.ss = 0
    emu.cpu.es = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    for _ in range(20):
        emu.cpu.execute_many(1)
        if emu.cpu.halted:
            break
    assert emu.cpu.halted
    assert emu.video.graphics_pixel(0, 0) == 0x05
    assert emu.video.graphics_pixel(1, 0) == 0x0F


def test_native_backend_uses_larger_batches_for_safe_graphics():
    emu = Emulator(cpu_backend='c')
    assert emu.cpu.preferred_batch_size() == 128
    emu.video.set_mode(0x10)
    assert emu.cpu.preferred_batch_size() == 1024


def test_native_backend_falls_back_for_vga_latch_copy_mode():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.seq_regs[2] = 0x0F
    for plane, value in enumerate((0xA5, 0x5A, 0x3C, 0xC3)):
        emu.video.graphics_planes[plane][0] = value
    emu.video.gdc_regs[5] = 1  # write mode 1 copies latches
    # mov ax,A000; mov es,ax; xor di,di; mov al,es:[di];
    # inc di; mov es:[di],al; hlt
    emu.mem.ram[0x100:0x10F] = bytes((
        0xB8, 0x00, 0xA0, 0x8E, 0xC0, 0x31, 0xFF,
        0x26, 0x8A, 0x05, 0x47, 0x26, 0x88, 0x05, 0xF4,
    ))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.ss = 0
    emu.cpu.es = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    for _ in range(20):
        emu.cpu.execute_many(1)
        if emu.cpu.halted:
            break
    assert emu.cpu.halted
    assert [plane[1] for plane in emu.video.graphics_planes] == [
        0xA5, 0x5A, 0x3C, 0xC3]


def test_native_backend_bulk_copies_rep_movs_in_vga_latch_mode():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.gdc_regs[5] = 1
    emu.video.seq_regs[2] = 0x0F
    for plane, values in enumerate(((0xA5, 0x5A), (0x3C, 0xC3),
                                    (0x0F, 0xF0), (0x96, 0x69))):
        emu.video.graphics_planes[plane][0:2] = bytes(values)
    emu.mem.ram[0x100:0x103] = bytes((0xF3, 0xA4, 0xF4))
    emu.cpu.cs = emu.cpu.ss = 0
    emu.cpu.ds = emu.cpu.es = 0xA000
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    emu.cpu.cx = 2
    emu.cpu.si = 0
    emu.cpu.di = 4

    emu.cpu.execute_many(4)

    assert emu.cpu.cx == 0
    assert emu.cpu.si == 2
    assert emu.cpu.di == 6
    assert [list(plane[4:6]) for plane in emu.video.graphics_planes] == [
        [0xA5, 0x5A], [0x3C, 0xC3], [0x0F, 0xF0], [0x96, 0x69]]


def test_native_backend_keeps_overlapping_mode1_movs_on_reference_path():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.gdc_regs[5] = 1
    emu.video.seq_regs[2] = 0x0F
    for plane, values in enumerate(((0xA5, 0x5A, 0x3C), (1, 2, 3),
                                    (4, 5, 6), (7, 8, 9))):
        emu.video.graphics_planes[plane][0:3] = bytes(values)
    emu.mem.ram[0x100:0x103] = bytes((0xF3, 0xA4, 0xF4))
    emu.cpu.cs = emu.cpu.ss = 0
    emu.cpu.ds = emu.cpu.es = 0xA000
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    emu.cpu.cx = 2
    emu.cpu.si = 0
    emu.cpu.di = 1

    emu.cpu.execute_many(4)

    assert [list(plane[:3]) for plane in emu.video.graphics_planes] == [
        [0xA5, 0xA5, 0xA5], [1, 1, 1], [4, 4, 4], [7, 7, 7]]


def test_native_backend_bulk_fills_rep_stos_in_vga_latch_mode():
    emu = Emulator(cpu_backend='c')
    emu.bios.initialize()
    emu.video.set_mode(0x10)
    emu.video.gdc_regs[5] = 1
    emu.video.seq_regs[2] = 0x0F
    emu.video.graphics_latches[:] = [0xA5, 0x3C, 0x0F, 0x96]
    emu.mem.ram[0x100:0x103] = bytes((0xF3, 0xAA, 0xF4))
    emu.cpu.cs = emu.cpu.ss = 0
    emu.cpu.es = 0xA000
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    emu.cpu.cx = 2
    emu.cpu.di = 4

    emu.cpu.execute_many(4)

    assert emu.cpu.cx == 0
    assert emu.cpu.di == 6
    assert [list(plane[4:6]) for plane in emu.video.graphics_planes] == [
        [0xA5, 0xA5], [0x3C, 0x3C], [0x0F, 0x0F], [0x96, 0x96]]


def test_native_backend_recovers_from_unicorn_invalid_legacy_encoding():
    emu = Emulator(cpu_backend='c')
    # F6 /1 is reserved on later x86 documentation but appears in some DOS
    # boot paths; the reference decoder tolerates it as a one-byte operation.
    emu.mem.ram[0x100:0x103] = bytes((0xF6, 0x0F, 0xF4))
    emu.cpu.cs = emu.cpu.ds = emu.cpu.ss = 0
    emu.cpu.ip = 0x100
    emu.cpu.sp = 0x7000
    assert emu.cpu.execute_many(4) == 1
    assert emu.cpu.ip == 0x102
    assert emu.cpu.execute_many(1) == 1
    assert emu.cpu.halted
