"""Full-width products used by Turbo Link's allocation-size calculations."""

import pytest


def test_tlink_small_allocation_does_not_request_thousands_of_segments(cpu):
    # Clear stale selector/sentinel bits, multiply element index by four,
    # then split the byte offset into a segment count and an offset.
    cpu.cs, cpu.ip = 0, 0x200
    code = bytes.fromhex('6633c0 b80400 6633ff bf0200 66f7e7 668bf8 668bf7 66c1ee10')
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.eax, cpu.edi, cpu.edx = 0xFFFF0004, 0x029F0002, 0xDEADBEEF
    for _ in range(8):
        assert cpu.execute()
    assert (cpu.edi, cpu.esi, cpu.edx) == (8, 0, 0)
    assert cpu.ip == 0x200 + len(code)


@pytest.mark.parametrize('memory_operand', [False, True])
@pytest.mark.parametrize('a,b', [(0, 0xFFFFFFFF), (4, 17), (0xFFFF0004, 17),
                                (0x10000, 0x10000), (0xFFFFFFFF, 0xFFFFFFFF),
                                (0x80000000, 2), (0xFFFFFFFF, 1)])
def test_mul_dword_matches_unicorn(cpu, memory_operand, a, b):
    uc = pytest.importorskip('unicorn')
    from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_EDX, UC_X86_REG_EDI, UC_X86_REG_EFLAGS

    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0
    cpu.eax, cpu.edi, cpu.edx = a, b, 0xDEADBEEF
    code = bytes.fromhex('66f7260008' if memory_operand else '66f7e7')
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.mem.ram[0x800:0x804] = b.to_bytes(4, 'little')
    native = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_16)
    native.mem_map(0, 0x1000)
    native.mem_write(0x200, code)
    native.mem_write(0x800, b.to_bytes(4, 'little'))
    for reg, value in [(UC_X86_REG_EAX, a), (UC_X86_REG_EDI, b),
                       (UC_X86_REG_EDX, cpu.edx), (UC_X86_REG_EFLAGS, cpu.flags)]:
        native.reg_write(reg, value)
    native.emu_start(0x200, 0x200 + len(code), count=1)
    assert cpu.execute()
    assert cpu.ip == 0x200 + len(code)
    assert cpu.eax == native.reg_read(UC_X86_REG_EAX)
    assert cpu.edx == native.reg_read(UC_X86_REG_EDX)
    assert cpu.flags & 0x801 == native.reg_read(UC_X86_REG_EFLAGS) & 0x801
