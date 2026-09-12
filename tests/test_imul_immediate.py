"""Immediate IMUL drives the Borland loader's table-index arithmetic."""

import pytest


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('opcode', [0x69, 0x6B])
@pytest.mark.parametrize('memory_operand', [False, True])
@pytest.mark.parametrize('source,factor', [(128, 30), (-123, -7), (0x7FFF, 127),
                                        (0x7FFFFFFF, -128)])
def test_imul_matches_unicorn(cpu, wide, opcode, memory_operand, source, factor):
    uc = pytest.importorskip('unicorn')
    from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_EBX, UC_X86_REG_EFLAGS
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0
    cpu.ebx = source
    cpu.eax = 0xABCD1234
    bits = 32 if wide else 16
    width = 1 if opcode == 0x6B else bits // 8
    code = (b'\x66' if wide else b'') + bytes((opcode, 6 if memory_operand else 0xC3))
    if memory_operand:
        code += b'\x00\x08'
    code += (factor & ((1 << (width * 8)) - 1)).to_bytes(width, 'little')
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.mem.ram[0x800:0x804] = (source & 0xFFFFFFFF).to_bytes(4, 'little')
    native = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_16)
    native.mem_map(0, 0x1000)
    native.mem_write(0x200, code)
    native.mem_write(0x800, bytes(cpu.mem.ram[0x800:0x804]))
    native.reg_write(UC_X86_REG_EAX, cpu.eax)
    native.reg_write(UC_X86_REG_EBX, cpu.ebx)
    native.reg_write(UC_X86_REG_EFLAGS, cpu.flags)
    native.emu_start(0x200, 0x200 + len(code), count=1)
    assert cpu.execute()
    assert cpu.ip == 0x200 + len(code)
    assert cpu.eax == native.reg_read(UC_X86_REG_EAX)
    assert cpu.flags & 0x801 == native.reg_read(UC_X86_REG_EFLAGS) & 0x801
