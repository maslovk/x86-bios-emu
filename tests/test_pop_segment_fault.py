"""Faulting segment POPs must be restartable by a DPMI demand loader."""

import pytest

from tests.test_protected_mode2 import (
    make_cpu, build_machine, desc, GDT, IDT, R0_CODE, R0_CODE_BASE, R0_STACK_BASE,
    R3_CODE, R3_CODE_BASE, R3_DATA, R3_DATA_BASE,
)


@pytest.mark.parametrize('opcode,name', [(0x07, 'es'), (0x1F, 'ds'), (0x17, 'ss')])
@pytest.mark.parametrize('wide', [False, True])
def test_segment_pop_retries_same_selector_after_load_fault(opcode, name, wide):
    cpu, mem = make_cpu()
    build_machine(cpu, mem)
    selector = 0x43
    cpu.gdt_limit = 0x47
    # ES/DS: #NP for a not-present descriptor. SS: #GP for wrong DPL.
    vector, access = (13, 0x92) if name == 'ss' else (11, 0x72)
    mem.ram[GDT + 0x40:GDT + 0x48] = desc(R3_DATA_BASE, 0xFFFF, access)
    mem.ram[IDT + vector * 8:IDT + (vector + 1) * 8] = bytes(
        (0x80, 0, R0_CODE, 0, 0, 0x86, 0, 0))
    code = (b'\x66' if wide else b'') + bytes((opcode,))
    mem.ram[R3_CODE_BASE:R3_CODE_BASE + len(code)] = code
    cpu._set_es(R3_DATA | 3)
    original_segment = getattr(cpu, name)
    cpu.esp = 0x1234FF00
    if wide:
        cpu._pushd(0xABCD0000 | selector)
    else:
        cpu._push(selector)
    original_esp = cpu.esp

    assert cpu.execute()
    assert (cpu.cs, cpu.ip) == (R0_CODE, 0x80)
    frame = R0_STACK_BASE + cpu.sp
    assert mem.read_word(frame) == selector & 0xFFFC
    assert mem.read_word(frame + 2) == 0  # Faulting IP, including prefix.
    assert mem.read_word(frame + 4) == R3_CODE | 3
    assert mem.read_word(frame + 8) == original_esp & 0xFFFF
    assert mem.read_word(frame + 10) == R3_DATA | 3
    if name != 'ss':
        assert getattr(cpu, name) == original_segment

    # Simulate the demand loader: mark the descriptor present, discard its
    # error code, and return. The retried POP consumes the selector once.
    mem.ram[GDT + 0x45] = 0xF2
    mem.ram[R0_CODE_BASE + 0x80] = 0xCF  # IRET
    cpu.sp += 2
    assert cpu.execute()
    assert cpu.sp == original_esp & 0xFFFF
    assert cpu.execute()
    assert getattr(cpu, name) == selector
    assert cpu.esp == 0x1234FF00
    assert cpu.ip == len(code)
    if name == 'ss':
        assert cpu._irq_shadow == 1
