"""Hardware interrupts must use protected-mode gates, not BIOS IVT stubs."""

from main import Emulator
from tests.test_protected_mode import make_descriptor


def protected_machine(vector):
    emu = Emulator()
    emu.bios.initialize()
    emu.pic.initialize()
    cpu = emu.cpu
    ram = emu.mem.ram
    cpu.gdt_base, cpu.gdt_limit = 0x1000, 23
    ram[0x1008:0x1010] = make_descriptor(0x20000, 0xFFFF, 0x9A)
    ram[0x1010:0x1018] = make_descriptor(0x30000, 0xFFFF, 0x92)
    cpu._set_msw(1)
    cpu._set_cs(8)
    cpu._set_ss(16)
    cpu.ip, cpu.sp = 0x100, 0xFF00
    cpu.idt_base, cpu.idt_limit = 0x4000, 0x7FF
    gate = cpu.idt_base + vector * 8
    ram[gate:gate + 8] = bytes((0, 2, 8, 0, 0, 0x86, 0, 0))
    ram[0x20200] = 0xCF  # IRET
    return emu


def test_timer_irq_uses_guest_idt_and_iret_frame():
    emu = protected_machine(8)
    cpu = emu.cpu
    cpu.if_flag = True
    saved_flags = cpu.flags
    cpu.halted = True
    emu.pic.raise_irq(0)

    assert emu._check_and_dispatch_irq()
    assert (cpu.cs, cpu.ip, cpu.sp) == (8, 0x200, 0xFEFA)
    assert cpu._pm and not cpu.halted and not cpu.if_flag
    assert emu.mem.read_word(0x3FEFA) == 0x100
    assert emu.mem.read_word(0x3FEFC) == 8
    assert emu.mem.read_word(0x3FEFE) == saved_flags
    assert cpu.execute()
    assert (cpu.cs, cpu.ip, cpu.sp, cpu.flags) == (8, 0x100, 0xFF00, saved_flags)


def test_resident_dpmi_int21_uses_guest_gate():
    emu = protected_machine(0x21)
    cpu = emu.cpu
    emu._install_bios_interrupt_hook()
    cpu.ax, cpu.bx = 0x352F, 0xBEEF
    cpu._do_interrupt(0x21, software=True)
    assert (cpu.cs, cpu.ip, cpu.sp) == (8, 0x200, 0xFEFA)
    assert cpu.bx == 0xBEEF


def test_irq_flags_keep_reserved_bit_after_popf():
    emu = protected_machine(8)
    cpu = emu.cpu
    cpu._pop_flags(0x3200)
    emu.pic.raise_irq(0)
    assert emu._check_and_dispatch_irq()
    # The DPMI IRQ stub examines this bit to distinguish FLAGS from an
    # exception's saved CS (whose RPL can be zero).
    assert emu.mem.read_word(0x30000 + cpu.sp + 4) == 0x3202


def test_privileged_hlt_fault_saves_faulting_ip():
    from tests.test_protected_mode2 import (
        make_cpu, build_machine, IDT, R0_CODE, R0_STACK_BASE, R3_CODE_BASE,
    )
    cpu, mem = make_cpu()
    build_machine(cpu, mem)
    mem.ram[IDT + 13 * 8:IDT + 14 * 8] = bytes(
        (0x80, 0, R0_CODE, 0, 0, 0x86, 0, 0))
    mem.ram[R3_CODE_BASE] = 0xF4
    assert cpu.execute()
    assert not cpu.halted
    assert (cpu.cs, cpu.ip) == (R0_CODE, 0x80)
    assert mem.read_word(R0_STACK_BASE + cpu.sp) == 0
    assert mem.read_word(R0_STACK_BASE + cpu.sp + 2) == 0
