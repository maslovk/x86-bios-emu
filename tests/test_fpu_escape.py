"""FPU escape (D8-DF) + WAIT (9B) regression tests (Phase D feature 4).

The emulator has no 8087 (equipment word reports none), so D8-DF are skipped
and WAIT is a no-op.  These confirm the skip consumes the full ModR/M +
displacement (so IP lands correctly on the next instruction) and that WAIT
does not halt.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _run(cpu, memory, code, ip=0):
    base = 0x10000
    for i, b in enumerate(code):
        memory.write_byte(base + i, b)
    cpu.cs = 0x1000
    cpu.ip = ip
    cpu.halted = False
    cpu.execute()


def test_wait_is_noop_and_advances(memory, cpu):
    _run(cpu, memory, [0x9B])
    assert not cpu.halted
    assert cpu.ip == 0x0001


def test_fpu_escape_register_operand(memory, cpu):
    # D8 /0 with mod=11 (register operand): opcode + modrm, no displacement.
    _run(cpu, memory, [0xD8, 0xC0])     # modrm = 11_000_000
    assert not cpu.halted
    assert cpu.ip == 0x0002


def test_fpu_escape_mod00_no_disp(memory, cpu):
    # mod=00, rm=000 (BX+SI): no displacement.
    _run(cpu, memory, [0xD8, 0x00])     # modrm = 00_000_000
    assert not cpu.halted
    assert cpu.ip == 0x0002


def test_fpu_escape_mod00_direct_addr(memory, cpu):
    # mod=00, rm=110: a 16-bit direct address (disp16).
    _run(cpu, memory, [0xD8, 0x06, 0x34, 0x12])
    assert not cpu.halted
    assert cpu.ip == 0x0004


def test_fpu_escape_mod01_disp8(memory, cpu):
    # mod=01, rm=101 (DI): one displacement byte.
    _run(cpu, memory, [0xD8, 0x4D, 0x7F])
    assert not cpu.halted
    assert cpu.ip == 0x0003


def test_fpu_escape_mod10_disp16(memory, cpu):
    # mod=10, rm=101 (DI): two displacement bytes.
    _run(cpu, memory, [0xD8, 0x95, 0xCD, 0xAB])
    assert not cpu.halted
    assert cpu.ip == 0x0004


def test_all_d8_df_escapes_consume_modrm_disp16(memory, cpu):
    # Every escape opcode D8..DF with a mod10/displacement ModR/M must advance
    # IP past opcode(1)+modrm(1)+disp16(2) = 4 without halting.
    for opc in range(0xD8, 0xE0):
        cpu.halted = False
        _run(cpu, memory, [opc, 0x95, 0x11, 0x22])
        assert not cpu.halted, f"opcode {opc:#04X} halted the CPU"
        assert cpu.ip == 0x0004, f"opcode {opc:#04X} left IP at {cpu.ip:#06X}"
