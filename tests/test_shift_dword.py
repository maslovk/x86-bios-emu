"""32-bit shifts used to construct Turbo Link's protected-mode pointers."""

import pytest

from tests.test_shift_flags import make_cpu


def test_tlink_selector_offset_pointer():
    # MOV AX,ES / SHL EAX,16 / MOV AX,DI / MOV EDI,EAX
    cpu = make_cpu(bytes.fromhex('8cc0 66c1e010 8bc7 668bf8'))
    cpu.eax = 0xFFFF0011
    cpu.es = 0x327
    cpu.di = 0xEA
    for _ in range(4):
        assert cpu.execute()
    assert cpu.edi == 0x032700EA


@pytest.mark.parametrize('group', range(8))
@pytest.mark.parametrize('count', [0, 1, 2, 16, 31, 32, 33])
@pytest.mark.parametrize('memory', [False, True])
@pytest.mark.parametrize('source', ['immediate', 'cl', 'one'])
def test_dword_shift_rotate_against_unicorn(group, count, memory, source):
    unicorn = pytest.importorskip('unicorn')
    from unicorn import x86_const as x

    # Immediate count must be decoded AFTER the memory displacement.
    operand = bytes([0x40 | group << 3, 0x12]) if memory else bytes([0xC0 | group << 3])
    opcode = {'immediate': 0xC1, 'cl': 0xD3, 'one': 0xD1}[source]
    code = bytes([0x66, opcode]) + operand
    if source == 'immediate':
        code += bytes([count])
    cpu = make_cpu(code)
    cpu.eax = 0xA1230327
    cpu.bx, cpu.si = 0x2000, 0x20
    cpu.cl = count
    cpu.flags = 0x803
    addr = 0x2032
    cpu._writed(addr, cpu.eax)

    ref = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_16)
    ref.mem_map(0, 0x10000)
    ref.mem_write(0x7C00, code)
    ref.mem_write(addr, cpu.eax.to_bytes(4, 'little'))
    for reg, value in [(x.UC_X86_REG_EAX, cpu.eax), (x.UC_X86_REG_BX, cpu.bx),
                       (x.UC_X86_REG_SI, cpu.si), (x.UC_X86_REG_CL, count),
                       (x.UC_X86_REG_EFLAGS, cpu.flags)]:
        ref.reg_write(reg, value)
    ref.emu_start(0x7C00, 0x7C00 + len(code), count=1)
    assert cpu.execute()
    assert cpu.ip == 0x7C00 + len(code)
    assert cpu.eax == ref.reg_read(x.UC_X86_REG_EAX)
    assert cpu._readd(addr) == int.from_bytes(ref.mem_read(addr, 4), 'little')
    # OF is undefined for counts >1; AF is undefined for nonzero shifts.
    effective_count = 1 if source == 'one' else count & 31
    flag_mask = 0xC5 | (0x800 if effective_count <= 1 else 0)
    assert cpu.flags & flag_mask == ref.reg_read(x.UC_X86_REG_EFLAGS) & flag_mask
