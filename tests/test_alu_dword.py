"""Full-width ALU operands in Turbo Link's symbol and allocation tables."""

import pytest


@pytest.mark.parametrize('operation', range(8))
@pytest.mark.parametrize('form', ['reg', 'mem_source', 'mem_dest', 'imm_acc',
                                  'imm_reg', 'imm_mem', 'byte_reg', 'byte_mem'])
@pytest.mark.parametrize('a,b,carry', [(0xFFFF0004, 17, 0), (0xFFFFFFFF, 1, 1),
                                     (0x80000000, 0x7FFFFFFF, 1),
                                     (0x12340000, 0xFFFFFFFF, 1)])
def test_alu_dword_matches_unicorn(cpu, operation, form, a, b, carry):
    uc = pytest.importorskip('unicorn')
    from unicorn import x86_const as x
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0
    cpu.eax, cpu.ebx = a, b
    cpu.flags = 2 | carry
    mem_value = b
    if form == 'reg':
        body = bytes([operation * 8 + 3, 0xC3])
    elif form == 'mem_source':
        body = bytes([operation * 8 + 3, 6, 0, 8])
    elif form == 'mem_dest':
        body = bytes([operation * 8 + 1, 0x1E, 0, 8])
        mem_value = a
    elif form == 'imm_acc':
        body = bytes([operation * 8 + 5]) + b.to_bytes(4, 'little')
    else:
        memory = form.endswith('mem')
        byte = form.startswith('byte')
        body = bytes([0x83 if byte else 0x81, operation << 3 | (6 if memory else 0xC0)])
        if memory:
            body += b'\x00\x08'
            mem_value = a
        body += bytes([b & 255]) if byte else b.to_bytes(4, 'little')
    code = b'\x66' + body
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.mem.ram[0x800:0x804] = mem_value.to_bytes(4, 'little')
    native = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_16)
    native.mem_map(0, 0x1000)
    native.mem_write(0x200, code)
    native.mem_write(0x800, mem_value.to_bytes(4, 'little'))
    for reg, value in [(x.UC_X86_REG_EAX, a), (x.UC_X86_REG_EBX, b),
                       (x.UC_X86_REG_EFLAGS, cpu.flags)]:
        native.reg_write(reg, value)
    native.emu_start(0x200, 0x200 + len(code), count=1)
    assert cpu.execute()
    assert cpu.ip == 0x200 + len(code)
    assert cpu.eax == native.reg_read(x.UC_X86_REG_EAX)
    assert cpu.ebx == native.reg_read(x.UC_X86_REG_EBX)
    assert cpu._readd(0x800) == int.from_bytes(native.mem_read(0x800, 4), 'little')
    mask = 0x8D5 if operation not in (1, 4, 6) else 0x8C5  # Logical AF undefined.
    assert cpu.flags & mask == native.reg_read(x.UC_X86_REG_EFLAGS) & mask


@pytest.mark.parametrize('opcode', [0x40, 0x48])
@pytest.mark.parametrize('value', [0, 0xFFFF, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000000])
@pytest.mark.parametrize('form', ['short', 'modrm_reg', 'modrm_mem'])
def test_inc_dec_dword_preserves_carry(cpu, opcode, value, form):
    cpu.cs, cpu.ip = 0, 0x200
    if form == 'short':
        code = bytes([0x66, opcode])
    else:
        modrm = (8 if opcode == 0x48 else 0) | (6 if form == 'modrm_mem' else 0xC0)
        code = bytes([0x66, 0xFF, modrm])
        if form == 'modrm_mem':
            code += b'\x00\x08'
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.ds = 0
    cpu._writed(0x800, value)
    cpu.eax, cpu.cf = value, True
    assert cpu.execute()
    result = cpu._readd(0x800) if form == 'modrm_mem' else cpu.eax
    assert result == (value + (1 if opcode == 0x40 else -1)) & 0xFFFFFFFF
    assert cpu.cf
    assert cpu.ip == 0x200 + len(code)


@pytest.mark.parametrize('memory_operand', [False, True])
def test_xchg_dword_preserves_pointer_selectors(cpu, memory_operand):
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0
    cpu.ebx, cpu.edx = 0x032700EA, 0x02370017
    code = bytes.fromhex('66871e0008' if memory_operand else '6687da')
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu._writed(0x800, cpu.edx)
    flags = cpu.flags
    assert cpu.execute()
    assert cpu.ebx == 0x02370017
    assert (cpu._readd(0x800) if memory_operand else cpu.edx) == 0x032700EA
    assert cpu.flags == flags
    assert cpu.ip == 0x200 + len(code)


@pytest.mark.parametrize('memory_operand', [False, True])
@pytest.mark.parametrize('a,b', [(0x80000000, 0x80000000), (0x10000, 0xFFFF)])
def test_test_dword_checks_high_bits_without_storing(cpu, memory_operand, a, b):
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0
    cpu.eax, cpu.ebx = a, b
    code = bytes.fromhex('6685060008' if memory_operand else '6685d8')
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu._writed(0x800, b)
    assert cpu.execute()
    assert (cpu.eax, cpu.ebx, cpu._readd(0x800)) == (a, b, b)
    assert cpu.zf == ((a & b) == 0)
    assert cpu.sf == bool(a & b & 0x80000000)
    assert not cpu.cf and not cpu.of


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('store', [False, True])
def test_accumulator_direct_memory_width(cpu, wide, store):
    cpu.cs, cpu.ip, cpu.ds = 0, 0x200, 0x100
    code = (b'\x66' if wide else b'') + bytes([0xA3 if store else 0xA1, 0, 8])
    cpu.mem.ram[0x200:0x200 + len(code)] = code
    cpu.eax = 0x032700EA
    cpu._writed(0x1800, 0xFFFF1234)
    assert cpu.execute()
    if store:
        assert cpu._readd(0x1800) == (0x032700EA if wide else 0xFFFF00EA)
        assert cpu.eax == 0x032700EA
    else:
        assert cpu.eax == (0xFFFF1234 if wide else 0x03271234)
        assert cpu._readd(0x1800) == 0xFFFF1234
    assert cpu.ip == 0x200 + len(code)
