"""Descriptor tables and task state may reside above conventional memory."""

import pytest

from main import Emulator
from tests.test_protected_mode import make_descriptor


def machine():
    emu = Emulator()
    emu.cpu.set_a20(True)
    return emu.cpu, emu.mem.ram


def test_extended_gdt_load_sets_accessed_bit_in_extended_table():
    cpu, ram = machine()
    cpu.gdt_base, cpu.gdt_limit = 0x110000, 23
    ram[0x110008:0x110010] = make_descriptor(0x120000, 0xFFFF, 0x9A)
    ram[0x110010:0x110018] = make_descriptor(0x130000, 0xFFFF, 0x92)
    cpu._set_msw(1)
    cpu._set_cs(8)
    cpu._set_ds(16)
    assert cpu._code_base == 0x120000
    assert cpu._phys(16, 0x1234) == 0x131234
    assert ram[0x11000D] == 0x9B
    assert ram[0x110015] == 0x93
    assert ram[0x1000D] == ram[0x10015] == 0


def test_extended_tss_words_do_not_alias_low_memory():
    cpu, ram = machine()
    cpu.tr_selector = 8
    cpu._desc_cache[8] = (0x140000, 43, 0x83, 0x110008)
    ram[0x14000E:0x140010] = b'\x34\x12'
    ram[0x4000E:0x40010] = b'\x78\x56'
    assert cpu._tss_word(14) == 0x1234
    cpu._write_tss_word(14, 0xABCD)
    assert ram[0x14000E:0x140010] == b'\xCD\xAB'
    assert ram[0x4000E:0x40010] == b'\x78\x56'


@pytest.mark.parametrize('group,field', [(2, 'gdt'), (3, 'idt')])
def test_table_register_operand_in_hma(group, field):
    cpu, ram = machine()
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0xFFFF
    # LGDT/LIDT [0010] at FFFF:0010 = physical 100000.
    ram[0x200:0x205] = bytes((0x0F, 0x01, (group << 3) | 6, 0x10, 0))
    ram[0x100000:0x100006] = b'\xFF\x03\x00\x00\x12\x00'
    assert cpu.execute()
    assert getattr(cpu, field + '_base') == 0x120000
    assert getattr(cpu, field + '_limit') == 0x3FF


def test_real_mode_cs_load_clears_previous_privilege():
    cpu, _ = machine()
    cpu._cpl = 3
    cpu._set_cs(0x115F)
    assert cpu._cpl == 0


@pytest.mark.parametrize('opcode', [0x0D, 0x25, 0x35])
def test_dword_accumulator_logic_matches_unicorn(opcode):
    uc = pytest.importorskip('unicorn')
    from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_EFLAGS
    cpu, ram = machine()
    cpu.cs, cpu.ip = 0, 0x200
    cpu.eax = 0xDEADFFF1
    code = bytes((0x66, opcode)) + (0x7FFFFFF6).to_bytes(4, 'little')
    ram[0x200:0x206] = code
    native = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_16)
    native.mem_map(0, 0x1000)
    native.mem_write(0x200, code)
    native.reg_write(UC_X86_REG_EAX, cpu.eax)
    native.reg_write(UC_X86_REG_EFLAGS, cpu.flags)
    native.emu_start(0x200, 0x206, count=1)
    assert cpu.execute()
    assert cpu.ip == 0x206
    assert cpu.eax == native.reg_read(UC_X86_REG_EAX)
    # AF is undefined for logic operations.
    assert cpu.flags & 0x8C5 == native.reg_read(UC_X86_REG_EFLAGS) & 0x8C5


def test_dpmi_cr0_return_sequence_consumes_dword_mask():
    cpu, ram = machine()
    cpu.gdt_base, cpu.gdt_limit = 0x1000, 15
    ram[0x1008:0x1010] = make_descriptor(0x20000, 0xFFFF, 0x9A)
    cpu._set_msw(1)
    cpu._set_cs(8)
    cpu.ip = 0
    cpu.eax = 0xDEADBEEF
    # MOV EAX,CR0; AND EAX,7FFFFFF6h; MOV CR0,EAX; JMP FAR 3000:0100.
    code = bytes.fromhex('0f20c06625f6ffff7f0f22c0ea00010030')
    ram[0x20000:0x20000 + len(code)] = code
    assert cpu.execute()
    assert cpu.eax == 0xFFF1
    assert cpu.execute()
    assert cpu.eax == 0xFFF0
    assert cpu.execute()
    assert not cpu._pm
    assert cpu.execute()
    assert (cpu.cs, cpu.ip, cpu._code_base) == (0x3000, 0x100, 0x30000)


def test_pushad_popad_preserve_full_registers_and_stack_layout():
    cpu, ram = machine()
    cpu.cs, cpu.ip, cpu.ss, cpu.esp = 0, 0x200, 0, 0x900
    names = ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi')
    for index, name in enumerate(names):
        if name != 'esp':
            setattr(cpu, name, 0xABCD1000 + index)
    values = [getattr(cpu, name) for name in names]
    ram[0x200:0x204] = bytes.fromhex('66606661')
    assert cpu.execute()
    assert cpu.sp == 0x8E0
    assert ram[0x8E0:0x900] == b''.join(
        value.to_bytes(4, 'little') for value in reversed(values))
    for name in names:
        if name != 'esp':
            setattr(cpu, name, 0)
    assert cpu.execute()
    assert [getattr(cpu, name) for name in names] == values


def test_lar_can_inspect_not_present_descriptor():
    uc = pytest.importorskip('unicorn')
    from unicorn.x86_const import UC_X86_REG_AX, UC_X86_REG_EFLAGS
    cpu, ram = machine()
    cpu.gdt_base, cpu.gdt_limit = 0x1000, 23
    ram[0x1008:0x1010] = make_descriptor(0, 0xFFFF, 0x9A)
    ram[0x1010:0x1018] = make_descriptor(0x120000, 0xFFFF, 0x72)
    cpu._set_msw(1)
    cpu._set_cs(8)
    cpu.ip, cpu.ax = 0x200, 16
    code = bytes.fromhex('0f02c0')  # LAR AX,AX
    ram[0x200:0x203] = code
    native = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_16)
    native.mem_map(0, 0x2000)
    native.mem_write(0, bytes(ram[:0x2000]))
    native.mem_write(0x180, bytes.fromhex('170000100000'))
    native.mem_write(0x100, bytes.fromhex(
        '0f01168001b801000f01f0ea00020800'))
    native.mem_write(0x200, bytes.fromhex('b810000f02c0f4'))
    native.emu_start(0x100, 0, count=7)
    assert cpu.execute()
    assert cpu.ax == native.reg_read(UC_X86_REG_AX) == 0x7200
    assert cpu.zf and native.reg_read(UC_X86_REG_EFLAGS) & 0x40


def test_rep_movsb_can_copy_final_byte_of_segment():
    cpu, ram = machine()
    cpu.gdt_base, cpu.gdt_limit = 0x1000, 31
    ram[0x1008:0x1010] = make_descriptor(0x20000, 0xFFFF, 0x9A)
    ram[0x1010:0x1018] = make_descriptor(0x120000, 4, 0x92)
    ram[0x1018:0x1020] = make_descriptor(0x130000, 4, 0x92)
    cpu._set_msw(1)
    cpu._set_cs(8)
    cpu._set_ds(16)
    cpu._set_es(24)
    cpu.ip, cpu.si, cpu.di, cpu.cx = 0, 0, 0, 5
    ram[0x20000:0x20002] = bytes.fromhex('f3a4')
    ram[0x120000:0x120005] = b'hello'
    assert cpu.execute()
    assert ram[0x130000:0x130005] == b'hello'
    assert cpu.cx == 0 and not cpu.halted
