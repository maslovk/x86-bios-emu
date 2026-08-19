"""Regression tests for the CPU flag/operand bugs found booting MS-DOS 6.22.

DOS 6.22's kernel is shipped EXEPACK-compressed; SYSINIT's decompressor
(decoder at 3864:0060-01CB on the reference image) is a dense bit-stream
consumer that exercises DEC/SHR/TEST flag corners far harder than DOS 3/4/5.
Four divergences from real x86 semantics (all verified against Unicorn via
probe_dos6_diff.py differential tracing) made the decompressor terminate
early, so the relocated kernel image was mostly zeros and the boot slid into
garbage:

1. INC/DEC (r16, r/m8, r/m16) never touched AF.  AF must reflect the
   nibble carry/borrow: DEC 0x0000 -> 0xFFFF sets AF; INC 0x000F sets AF.
2. Logic ops (AND/OR/XOR/TEST via _flags_logic*) left AF stale instead of
   clearing it.
3. SHR r/m, 1 computed OF from the *result*; OF is the MSB of the
   *original* operand (0x8000 >> 1 sets OF).
4. TEST r/m, r (opcodes 84h/85h) ignored the ModRM reg field and always
   tested AL/AX -- ``TEST BX,BX`` with AX=0 always reported ZF=1.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cpu import CPU
from tests.conftest import Mem


def make_cpu(code, cs=0, ip=0x7C00):
    mem = Mem()
    io = type('IO', (), {
        'inb': lambda self, p: 0,
        'outb': lambda self, p, v: None,
        'inw': lambda self, p: 0,
        'outw': lambda self, p, v: None,
        'tick': lambda self, dt=0: None,
        'get_pending_irq': lambda self: -1,
        'get_irq_vector': lambda self, irq: 0,
    })()
    cpu = CPU(mem, io)
    cpu.ss = 0; cpu.sp = 0x7C00
    for i, b in enumerate(code):
        mem.write_byte((cs << 4) + ip + i, b)
    cpu.cs = cs; cpu.ip = ip
    return cpu


class TestIncDecAuxCarry:
    """AF on INC/DEC reflects the borrow/carry out of bit 3."""

    def test_dec_zero_wraps_sets_af(self):
        # DEC DX with DX=0 -> 0xFFFF: nibble borrow sets AF (EXEPACK bit counter)
        cpu = make_cpu([0x4A])  # DEC DX
        cpu.dx = 0x0000
        cpu.af = False
        cpu.execute()
        assert cpu.dx == 0xFFFF
        assert cpu.af is True

    def test_dec_no_borrow_clears_af(self):
        cpu = make_cpu([0x4A])
        cpu.dx = 0x0005
        cpu.af = True
        cpu.execute()
        assert cpu.dx == 0x0004
        assert cpu.af is False

    def test_dec_nibble_boundary_sets_af(self):
        # 0x0010 -> 0x000F borrows out of the low nibble
        cpu = make_cpu([0x4A])
        cpu.dx = 0x0010
        cpu.af = False
        cpu.execute()
        assert cpu.dx == 0x000F
        assert cpu.af is True

    def test_inc_nibble_wrap_sets_af(self):
        cpu = make_cpu([0x42])  # INC DX
        cpu.dx = 0x000F
        cpu.af = False
        cpu.execute()
        assert cpu.dx == 0x0010
        assert cpu.af is True

    def test_inc_no_carry_clears_af(self):
        cpu = make_cpu([0x42])
        cpu.dx = 0x0004
        cpu.af = True
        cpu.execute()
        assert cpu.af is False

    def test_dec_rm8_sets_af(self):
        cpu = make_cpu([0xFE, 0xCB])  # DEC BL
        cpu.bx = 0x0000  # BL=0
        cpu.af = False
        cpu.execute()
        assert cpu.bl == 0xFF
        assert cpu.af is True

    def test_inc_rm16_sets_af(self):
        cpu = make_cpu([0xFF, 0xC3])  # INC BX
        cpu.bx = 0x000F
        cpu.af = False
        cpu.execute()
        assert cpu.bx == 0x0010
        assert cpu.af is True

    def test_dec_rm16_sets_af(self):
        cpu = make_cpu([0xFF, 0xCB])  # DEC BX
        cpu.bx = 0x0000
        cpu.af = False
        cpu.execute()
        assert cpu.bx == 0xFFFF
        assert cpu.af is True


class TestLogicClearsAuxCarry:
    """AND/OR/XOR/TEST clear AF (real x86 behaviour; Unicorn-verified)."""

    def test_xor_reg_clears_stale_af(self):
        # Exact regression: EXEPACK decoder does XOR CX,CX after a DEC that
        # set AF; stale AF changed the following SAHF-observed flag image.
        cpu = make_cpu([0x33, 0xC9])  # XOR CX, CX
        cpu.cx = 0x0042
        cpu.af = True
        cpu.execute()
        assert cpu.cx == 0
        assert cpu.zf is True
        assert cpu.af is False

    def test_and_rm16_clears_af(self):
        cpu = make_cpu([0x23, 0xD9])  # AND BX, CX
        cpu.bx = 0x00FF; cpu.cx = 0x0F0F
        cpu.af = True
        cpu.execute()
        assert cpu.bx == 0x000F
        assert cpu.af is False


class TestShrOverflowFlag:
    """SHR r/m,1 sets OF from the MSB of the ORIGINAL operand."""

    def test_shr_msb_set_sets_of(self):
        cpu = make_cpu([0xD1, 0xED])  # SHR BP, 1
        cpu.bp = 0x8000
        cpu.of = False
        cpu.execute()
        assert cpu.bp == 0x4000
        assert cpu.of is True, "SHR of a value with MSB set must set OF"

    def test_shr_msb_clear_clears_of(self):
        cpu = make_cpu([0xD1, 0xED])
        cpu.bp = 0x4000
        cpu.of = True
        cpu.execute()
        assert cpu.bp == 0x2000
        assert cpu.of is False

    def test_shr_by_cl_ignores_of(self):
        # OF is undefined for count > 1; we clear it (matches Unicorn)
        cpu = make_cpu([0xD3, 0xEB])  # SHR BX, CL
        cpu.bx = 0x8000; cpu.cx = 2
        cpu.of = True
        cpu.execute()
        assert cpu.bx == 0x2000
        assert cpu.of is False


class TestTestUsesModrmReg:
    """TEST r/m, r (84h/85h) must use the ModRM reg field, not AL/AX."""

    def test_test_bx_bx_nonzero_clears_zf(self):
        # Exact regression: EXEPACK decoder tests BX,BX after building a
        # displacement; with AX=0 the old code always reported ZF=1 and the
        # decoder walked the wrong branch for every record.
        cpu = make_cpu([0x85, 0xDB])  # TEST BX, BX
        cpu.ax = 0x0000  # must not participate
        cpu.bx = 0x0001
        cpu.zf = True
        cpu.execute()
        assert cpu.zf is False, "TEST BX,BX with BX=1 must clear ZF"

    def test_test_bx_bx_zero_sets_zf(self):
        cpu = make_cpu([0x85, 0xDB])
        cpu.ax = 0xFFFF
        cpu.bx = 0x0000
        cpu.zf = False
        cpu.execute()
        assert cpu.zf is True

    def test_test_two_different_registers(self):
        # TEST DX, BX: result is DX & BX regardless of AX
        cpu = make_cpu([0x85, 0xDA])  # TEST DX, BX (reg=DX, rm=BX)
        cpu.ax = 0x0000
        cpu.dx = 0x0FF0
        cpu.bx = 0x00FF
        cpu.zf = True
        cpu.execute()
        assert cpu.zf is False, "0x0FF0 & 0x00FF = 0x00F0 -> ZF=0"

    def test_test_rm8_reg8_uses_reg(self):
        cpu = make_cpu([0x84, 0xDB])  # TEST BL, BL
        cpu.ax = 0x0000
        cpu.bx = 0x0002
        cpu.zf = True
        cpu.execute()
        assert cpu.zf is False
