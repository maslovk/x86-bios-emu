"""
Tests for newly added CPU instructions:
  PUSH imm16/imm8, ENTER, LEAVE, AAM, AAD, SALC, XLAT,
  SETcc, STOSB/W, LODSB/W, SCASB/W, conditional jumps (JO/JNO/JP/JPO/JBE/JA)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from cpu import CPU
from tests.conftest import Mem


def _make_cpu(ram=0x10000):
    m = Mem(ram)
    io = type('IO', (), {'inb': lambda s, p: 0, 'outb': lambda s, p, v: None,
                         'inw': lambda s, p: 0, 'outw': lambda s, p, v: None})()
    return CPU(m, io)


# ── PUSH imm16 (68) ──────────────────────────────────────────────

class TestPushImm16:
    def test_push_imm16_basic(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.ds = 0
        cpu.cs = 0
        cpu.ip = 0
        # 68 00 12 → PUSH 0x1200
        cpu.mem.write_byte(0, 0x68)
        cpu.mem.write_word(1, 0x1200)
        cpu.execute()
        assert cpu.sp == 0x0FFE
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0x1200

    def test_push_imm16_zero(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x68)
        cpu.mem.write_word(1, 0x0000)
        cpu.execute()
        assert cpu.sp == 0x0FFE
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0

    def test_push_imm16_ffff(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x68)
        cpu.mem.write_word(1, 0xFFFF)
        cpu.execute()
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0xFFFF

    def test_push_imm16_sp_wrap(self):
        cpu = _make_cpu()
        cpu.sp = 0x0000  # push wraps: 0 - 2 = 0xFFFE
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x68)
        cpu.mem.write_word(1, 0xBEEF)
        cpu.execute()
        assert cpu.sp == 0xFFFE
        assert cpu.mem.read_word(cpu._phys(cpu.ss, 0xFFFE)) == 0xBEEF


# ── PUSH imm8 (6A) ───────────────────────────────────────────────

class TestPushImm8:
    def test_push_imm8_positive(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        # 6A 05 → PUSH 5
        cpu.mem.write_word(0, 0x056A)
        cpu.execute()
        assert cpu.sp == 0x0FFE
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0x0005

    def test_push_imm8_negative_sign_extend(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        # 6A FF → PUSH -1 (0xFFFF)
        cpu.mem.write_byte(0, 0x6A)
        cpu.mem.write_byte(1, 0xFF)
        cpu.execute()
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0xFFFF

    def test_push_imm8_80(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        # 6A 80 → PUSH -128 (0xFF80)
        cpu.mem.write_word(0, 0x806A)
        cpu.execute()
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0xFF80

    def test_push_imm8_zero(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_word(0, 0x006A)
        cpu.execute()
        assert cpu.mem.read_word(cpu._phys(cpu.ss, cpu.sp)) == 0


# ── ENTER / LEAVE ────────────────────────────────────────────────

class TestEnterLeave:
    def test_enter_level0(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.bp = 0x0500
        cpu.cs = 0; cpu.ip = 0
        # C8 10 00 00 → ENTER 0x0010, 0
        cpu.mem.write_byte(0, 0xC8)
        cpu.mem.write_word(1, 0x0010)
        cpu.mem.write_byte(3, 0x00)
        cpu.execute()
        # BP pushed → SP=0x0FFE, BP=0x0FFE, then SP -= 0x10
        assert cpu.bp == 0x0FFE
        assert cpu.sp == 0x0FEE  # 0x0FFE - 0x10

    def test_enter_level0_no_local(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.bp = 0x0500
        cpu.cs = 0; cpu.ip = 0
        # C8 00 00 00 → ENTER 0, 0
        cpu.mem.write_byte(0, 0xC8)
        cpu.mem.write_word(1, 0x0000)
        cpu.mem.write_byte(3, 0x00)
        cpu.execute()
        assert cpu.bp == 0x0FFE
        assert cpu.sp == 0x0FFE

    def test_leave_restores_bp_and_sp(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.bp = 0x0FFE
        # Store old BP on stack at BP (which becomes SP for the pop)
        cpu.mem.write_word(cpu._phys(cpu.ss, 0x0FFE), 0x0500)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xC9)  # LEAVE
        cpu.execute()
        # LEAVE: SP=BP (0x0FFE), then BP=pop() → reads at 0x0FFE, SP+=2 → 0x1000
        assert cpu.sp == 0x1000
        assert cpu.bp == 0x0500

    def test_enter_leave_roundtrip(self):
        cpu = _make_cpu()
        old_sp = 0x1000
        old_bp = 0x0500
        cpu.sp = old_sp
        cpu.bp = old_bp
        cpu.cs = 0; cpu.ip = 0
        # ENTER 8, 0
        cpu.mem.write_byte(0, 0xC8)
        cpu.mem.write_word(1, 0x0008)
        cpu.mem.write_byte(3, 0x00)
        cpu.execute()
        enter_bp = cpu.bp
        enter_sp = cpu.sp
        # LEAVE at IP=4
        cpu.mem.write_byte(4, 0xC9)
        cpu.execute()
        assert cpu.bp == old_bp
        assert cpu.sp == old_sp

    def test_enter_level1(self):
        cpu = _make_cpu()
        cpu.sp = 0x1000
        cpu.bp = 0x0500
        cpu.cs = 0; cpu.ip = 0
        # C8 00 00 01 → ENTER 0, 1
        cpu.mem.write_byte(0, 0xC8)
        cpu.mem.write_word(1, 0x0000)
        cpu.mem.write_byte(3, 0x01)
        cpu.execute()
        # BP pushed → SP=0x0FFE, BP=0x0FFE
        # Level 1: bp_var = MEM[SS:0x0FFE] (old BP=0x0500), push(0x0500)
        # SP=0x0FFC
        assert cpu.bp == 0x0FFE
        assert cpu.sp == 0x0FFC


# ── AAM (D4) ─────────────────────────────────────────────────────

class TestAAM:
    def test_aam_hex(self):
        """AAM 0x10: split AL into high/low nibbles."""
        cpu = _make_cpu()
        cpu.ax = 0x00FF  # 255 decimal
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD4)
        cpu.mem.write_byte(1, 0x10)
        cpu.execute()
        # 255 / 16 = 15 remainder 15 → AH=0x0F, AL=0x0F
        assert (cpu.ax >> 8) == 0x0F
        assert (cpu.ax & 0xFF) == 0x0F

    def test_aam_decimal(self):
        """AAM 0x0A: convert BCD."""
        cpu = _make_cpu()
        cpu.ax = 0x005A  # 90 decimal
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD4)
        cpu.mem.write_byte(1, 0x0A)
        cpu.execute()
        # 90 / 10 = 9 remainder 0 → AH=0x09, AL=0x00
        assert (cpu.ax >> 8) == 0x09
        assert (cpu.ax & 0xFF) == 0x00

    def test_aam_small(self):
        cpu = _make_cpu()
        cpu.ax = 0x0005
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD4)
        cpu.mem.write_byte(1, 0x10)
        cpu.execute()
        # 5 / 16 = 0 remainder 5 → AH=0, AL=5
        assert (cpu.ax >> 8) == 0
        assert (cpu.ax & 0xFF) == 5

    def test_aam_zero(self):
        cpu = _make_cpu()
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD4)
        cpu.mem.write_byte(1, 0x10)
        cpu.execute()
        assert cpu.ax == 0

    def test_aam_div_by_zero_halts(self):
        cpu = _make_cpu()
        cpu.ax = 0x00FF
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD4)
        cpu.mem.write_byte(1, 0x00)
        cpu.execute()
        assert cpu.cf == True


# ── AAD (D5) ─────────────────────────────────────────────────────

class TestAAD:
    def test_aad_hex(self):
        """AAD 0x10: combine AH:AL with factor 16."""
        cpu = _make_cpu()
        cpu.ax = 0x020A  # AH=2, AL=10 → 2*16+10 = 42
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD5)
        cpu.mem.write_byte(1, 0x10)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0x2A
        assert (cpu.ax >> 8) == 0  # AH cleared

    def test_aad_decimal(self):
        """AAD 0x0A: combine BCD digits."""
        cpu = _make_cpu()
        cpu.ax = 0x0305  # AH=3, AL=5 → 3*10+5 = 35
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD5)
        cpu.mem.write_byte(1, 0x0A)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 35
        assert (cpu.ax >> 8) == 0

    def test_aad_zero(self):
        cpu = _make_cpu()
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD5)
        cpu.mem.write_byte(1, 0x10)
        cpu.execute()
        assert cpu.ax == 0

    def test_aad_div_by_zero_halts(self):
        cpu = _make_cpu()
        cpu.ax = 0x0101
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD5)
        cpu.mem.write_byte(1, 0x00)
        cpu.execute()
        assert cpu.cf == True


# ── SALC (D6) ────────────────────────────────────────────────────

class TestSALC:
    def test_salc_carry_set(self):
        cpu = _make_cpu()
        cpu.cf = True
        cpu.ax = 0x1234
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD6)
        cpu.execute()
        assert cpu.ax == 0x00FF

    def test_salc_carry_clear(self):
        cpu = _make_cpu()
        cpu.cf = False
        cpu.ax = 0x1234
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD6)
        cpu.execute()
        assert cpu.ax == 0x0000


class TestDirectDisp16Addressing:
    def test_mov_bl_from_direct_disp16_uses_ds_not_bp(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ds = 0
        cpu.bp = 0x7BE2
        cpu.bx = 0
        cpu.mem.write_byte(0, 0x8A)      # MOV BL, [disp16]
        cpu.mem.write_byte(1, 0x1E)
        cpu.mem.write_word(2, 0x7C0D)
        cpu.mem.write_byte(0x7C0D, 0x02)

        cpu.execute()

        assert cpu.bx == 0x0002


# ── XLAT (D7) ────────────────────────────────────────────────────

class TestXLAT:
    def test_xlat_basic(self):
        """XLAT: AL = DS:(BX+AL)"""
        cpu = _make_cpu()
        cpu.ax = 0x0003  # AL=3
        cpu.bx = 0x0100
        cpu.ds = 0
        # Table at 0x0100: [10, 20, 30, 40, 50]
        for i, v in enumerate([10, 20, 30, 40, 50]):
            cpu.mem.write_byte(0x0100 + i, v)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD7)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 40  # table[3]
        assert (cpu.ax >> 8) == 0  # AH unchanged

    def test_xlat_al_zero(self):
        cpu = _make_cpu()
        cpu.ax = 0x0000
        cpu.bx = 0x0200
        cpu.ds = 0
        cpu.mem.write_byte(0x0200, 0xAB)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD7)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0xAB

    def test_xlat_preserves_ah(self):
        cpu = _make_cpu()
        cpu.ax = 0xFF00
        cpu.bx = 0x0100
        cpu.ds = 0
        cpu.mem.write_byte(0x0100, 0x42)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xD7)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0x42
        assert (cpu.ax >> 8) == 0xFF


# ── STOSB (AA) / STOSW (AB) ──────────────────────────────────────

class TestSTOS:
    def test_stosb_forward(self):
        cpu = _make_cpu()
        cpu.ax = 0x00DE
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAA)
        cpu.execute()
        assert cpu.mem.read_byte(0x0100) == 0xDE
        assert cpu.di == 0x0101

    def test_stosb_backward(self):
        cpu = _make_cpu()
        cpu.ax = 0x00AB
        cpu.di = 0x00FF  # store at current DI, then DI--
        cpu.es = 0
        cpu.df = True
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAA)
        cpu.execute()
        assert cpu.mem.read_byte(0x00FF) == 0xAB
        assert cpu.di == 0x00FE

    def test_stosw_forward(self):
        cpu = _make_cpu()
        cpu.ax = 0xBEEF
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAB)
        cpu.execute()
        assert cpu.mem.read_word(0x0100) == 0xBEEF
        assert cpu.di == 0x0102

    def test_stosw_backward(self):
        cpu = _make_cpu()
        cpu.ax = 0xCAFE
        cpu.di = 0x00FE  # store at current DI, then DI-=2
        cpu.es = 0
        cpu.df = True
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAB)
        cpu.execute()
        assert cpu.mem.read_word(0x00FE) == 0xCAFE
        assert cpu.di == 0x00FC


# ── LODSB (AC) / LODSW (AD) ──────────────────────────────────────

class TestLODS:
    def test_lodsb_forward(self):
        cpu = _make_cpu()
        cpu.si = 0x0100
        cpu.ds = 0
        cpu.df = False
        cpu.mem.write_byte(0x0100, 0x55)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAC)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0x55
        assert cpu.si == 0x0101

    def test_lodsb_backward(self):
        cpu = _make_cpu()
        cpu.si = 0x00FF  # load at current SI, then SI--
        cpu.ds = 0
        cpu.df = True
        cpu.mem.write_byte(0x00FF, 0xAA)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAC)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0xAA
        assert cpu.si == 0x00FE

    def test_lodsw_forward(self):
        cpu = _make_cpu()
        cpu.si = 0x0100
        cpu.ds = 0
        cpu.df = False
        cpu.mem.write_word(0x0100, 0x1234)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAD)
        cpu.execute()
        assert cpu.ax == 0x1234
        assert cpu.si == 0x0102

    def test_lodsw_backward(self):
        cpu = _make_cpu()
        cpu.si = 0x00FE  # load at current SI, then SI-=2
        cpu.ds = 0
        cpu.df = True
        cpu.mem.write_word(0x00FE, 0x5678)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAD)
        cpu.execute()
        assert cpu.ax == 0x5678
        assert cpu.si == 0x00FC


# ── SCASB (AE) / SCASW (AF) ──────────────────────────────────────

class TestSCAS:
    def test_scasb_found(self):
        cpu = _make_cpu()
        cpu.ax = 0x00AA
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.mem.write_byte(0x0100, 0xAA)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAE)
        cpu.execute()
        assert cpu.zf == True
        assert cpu.di == 0x0101

    def test_scasb_not_found(self):
        cpu = _make_cpu()
        cpu.ax = 0x00AA
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.mem.write_byte(0x0100, 0xBB)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAE)
        cpu.execute()
        assert cpu.zf == False

    def test_scasb_backward(self):
        cpu = _make_cpu()
        cpu.ax = 0x00FF
        cpu.di = 0x00FF  # compare at current DI, then DI--
        cpu.es = 0
        cpu.df = True
        cpu.mem.write_byte(0x00FF, 0xFF)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAE)
        cpu.execute()
        assert cpu.zf == True
        assert cpu.di == 0x00FE

    def test_scasw_found(self):
        cpu = _make_cpu()
        cpu.ax = 0x1234
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.mem.write_word(0x0100, 0x1234)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAF)
        cpu.execute()
        assert cpu.zf == True
        assert cpu.di == 0x0102

    def test_scasw_not_found(self):
        cpu = _make_cpu()
        cpu.ax = 0x1234
        cpu.di = 0x0100
        cpu.es = 0
        cpu.df = False
        cpu.mem.write_word(0x0100, 0x5678)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAF)
        cpu.execute()
        assert cpu.zf == False

    def test_scasw_backward(self):
        cpu = _make_cpu()
        cpu.ax = 0xDEAD
        cpu.di = 0x00FE  # compare at current DI, then DI-=2
        cpu.es = 0
        cpu.df = True
        cpu.mem.write_word(0x00FE, 0xDEAD)
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0xAF)
        cpu.execute()
        assert cpu.zf == True
        assert cpu.di == 0x00FC


# ── SETcc (0F 90-9F) ─────────────────────────────────────────────

class TestSETcc:
    def _setup_setcc(self, cpu, target_addr=0x0200):
        """Write SETcc AL (0F 9x 80) at IP, target byte at target_addr."""
        cpu.cs = 0; cpu.ip = 0
        cpu.ds = 0
        # 0F 9x C0 → SETcc AL (mod=3, reg=x, rm=0)
        # Using C0 = mod=3, reg=0, rm=0 → AL
        return target_addr

    def test_seto_true(self):
        """SETO: set if OF=1"""
        cpu = _make_cpu()
        cpu.of = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        # 0F 90 C0 → SETO AL
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x90)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_seto_false(self):
        cpu = _make_cpu()
        cpu.of = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x90)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_setc_true(self):
        """SETC: set if CF=1"""
        cpu = _make_cpu()
        cpu.cf = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x92)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setc_false(self):
        cpu = _make_cpu()
        cpu.cf = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x92)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_setz_true(self):
        cpu = _make_cpu()
        cpu.zf = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x94)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setz_false(self):
        cpu = _make_cpu()
        cpu.zf = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x94)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_setnz_true(self):
        cpu = _make_cpu()
        cpu.zf = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x95)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_sets_true(self):
        cpu = _make_cpu()
        cpu.sf = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x98)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setpe_true(self):
        cpu = _make_cpu()
        cpu.pf = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9A)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setl_true(self):
        """SETL: SF != OF"""
        cpu = _make_cpu()
        cpu.sf = True; cpu.of = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9C)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setl_false(self):
        cpu = _make_cpu()
        cpu.sf = True; cpu.of = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9C)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_setge_true(self):
        """SETGE: SF == OF"""
        cpu = _make_cpu()
        cpu.sf = True; cpu.of = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9D)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setg_true(self):
        """SETG: !ZF && SF==OF (signed greater)"""
        cpu = _make_cpu()
        cpu.zf = False; cpu.sf = False; cpu.of = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9F)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setg_zf_false(self):
        """SETG: false when ZF=1 (equal is not greater)"""
        cpu = _make_cpu()
        cpu.zf = True; cpu.sf = False; cpu.of = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x9F)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_setcc_to_memory(self):
        """SETcc to memory operand [disp16]."""
        cpu = _make_cpu()
        cpu.zf = True
        cpu.ds = 0
        cpu.mem.write_byte(0x0200, 0x00)
        cpu.cs = 0; cpu.ip = 0
        # 0F 94 06 00 02 → SETZ [0x0200] (mod=0, rm=6=[disp16])
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x94)
        cpu.mem.write_byte(2, 0x06)
        cpu.mem.write_word(3, 0x0200)
        cpu.execute()
        assert cpu.mem.read_byte(0x0200) == 1

    def test_setcc_cl(self):
        """SETcc to CL (rm=1)."""
        cpu = _make_cpu()
        cpu.cf = True
        cpu.cx = 0x0000
        cpu.cs = 0; cpu.ip = 0
        # 0F 92 C1 → SETC CL (mod=3, reg=2, rm=1)
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x92)
        cpu.mem.write_byte(2, 0xC1)
        cpu.execute()
        assert (cpu.cx & 0xFF) == 1

    def test_setbe_true(self):
        """SETBE: ZF || CF"""
        cpu = _make_cpu()
        cpu.zf = False; cpu.cf = True
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x96)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1

    def test_setbe_false(self):
        cpu = _make_cpu()
        cpu.zf = False; cpu.cf = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x96)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0

    def test_seta_true(self):
        """SETA: !(ZF || CF)"""
        cpu = _make_cpu()
        cpu.zf = False; cpu.cf = False
        cpu.ax = 0x0000
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x0F)
        cpu.mem.write_byte(1, 0x97)
        cpu.mem.write_byte(2, 0xC0)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 1


# ── Conditional Jumps (JO/JNO/JP/JPO/JBE/JA) ─────────────────────

class TestConditionalJumps:
    def test_jo_taken(self):
        """JO: jump if OF=1"""
        cpu = _make_cpu()
        cpu.of = True
        cpu.cs = 0; cpu.ip = 0
        # 70 05 → JO +5
        cpu.mem.write_byte(0, 0x70)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jo_not_taken(self):
        cpu = _make_cpu()
        cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x70)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_jno_taken(self):
        cpu = _make_cpu()
        cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        # 71 05 → JNO +5
        cpu.mem.write_byte(0, 0x71)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jno_not_taken(self):
        cpu = _make_cpu()
        cpu.of = True
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x71)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_jpe_taken(self):
        """JPE: jump if PF=1"""
        cpu = _make_cpu()
        cpu.pf = True
        cpu.cs = 0; cpu.ip = 0
        # 7A 05 → JPE +5
        cpu.mem.write_byte(0, 0x7A)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jpo_taken(self):
        """JPO: jump if PF=0"""
        cpu = _make_cpu()
        cpu.pf = False
        cpu.cs = 0; cpu.ip = 0
        # 7B 0JPO +5
        cpu.mem.write_byte(0, 0x7B)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jbe_taken_cf(self):
        """JBE: jump if CF=1 or ZF=1"""
        cpu = _make_cpu()
        cpu.cf = True; cpu.zf = False
        cpu.cs = 0; cpu.ip = 0
        # 76 05 → JBE +5
        cpu.mem.write_byte(0, 0x76)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jbe_taken_zf(self):
        cpu = _make_cpu()
        cpu.cf = False; cpu.zf = True
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x76)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jbe_not_taken(self):
        cpu = _make_cpu()
        cpu.cf = False; cpu.zf = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x76)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_ja_taken(self):
        """JA: jump if !CF && !ZF"""
        cpu = _make_cpu()
        cpu.cf = False; cpu.zf = False
        cpu.cs = 0; cpu.ip = 0
        # 77 05 → JA +5
        cpu.mem.write_byte(0, 0x77)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_ja_not_taken_cf(self):
        cpu = _make_cpu()
        cpu.cf = True; cpu.zf = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x77)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_ja_not_taken_zf(self):
        cpu = _make_cpu()
        cpu.cf = False; cpu.zf = True
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x77)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_jl_taken(self):
        """JL: SF != OF"""
        cpu = _make_cpu()
        cpu.sf = True; cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        # 7C 05 → JL +5
        cpu.mem.write_byte(0, 0x7C)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jge_taken(self):
        """JGE: SF == OF"""
        cpu = _make_cpu()
        cpu.sf = True; cpu.of = True
        cpu.cs = 0; cpu.ip = 0
        # 7D 05 → JGE +5
        cpu.mem.write_byte(0, 0x7D)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jle_taken(self):
        """JLE: ZF || (SF != OF)"""
        cpu = _make_cpu()
        cpu.zf = False; cpu.sf = True; cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        # 7E 05 → JLE +5
        cpu.mem.write_byte(0, 0x7E)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jg_taken(self):
        """JG: !ZF && SF==OF"""
        cpu = _make_cpu()
        cpu.zf = False; cpu.sf = False; cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        # 7F 05 → JG +5
        cpu.mem.write_byte(0, 0x7F)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0007

    def test_jg_not_taken_zf(self):
        cpu = _make_cpu()
        cpu.zf = True; cpu.sf = False; cpu.of = False
        cpu.cs = 0; cpu.ip = 0
        cpu.mem.write_byte(0, 0x7F)
        cpu.mem.write_byte(1, 0x05)
        cpu.execute()
        assert cpu.ip == 0x0002

    def test_jb_negative_offset(self):
        """JB with negative offset (backward jump)."""
        cpu = _make_cpu()
        cpu.cf = True
        cpu.cs = 0; cpu.ip = 0x0010
        # 72 F8 → JB -8 (to 0x000A)
        cpu.mem.write_byte(0x0010, 0x72)
        cpu.mem.write_byte(0x0011, 0xF8)
        cpu.execute()
        assert cpu.ip == 0x000A


class TestFarPointerLoads:
    def test_les_loads_register_and_es(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.mem.write_byte(0x0010, 0x34)
        cpu.mem.write_byte(0x0011, 0x12)
        cpu.mem.write_byte(0x0012, 0x78)
        cpu.mem.write_byte(0x0013, 0x56)
        cpu.mem.write_byte(0x0000, 0xC4)  # LES AX, [0x0010]
        cpu.mem.write_byte(0x0001, 0x06)
        cpu.mem.write_byte(0x0002, 0x10)
        cpu.mem.write_byte(0x0003, 0x00)

        cpu.execute()

        assert cpu.ax == 0x1234
        assert cpu.es == 0x5678

    def test_lds_honors_segment_override(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ss = 0x0020
        base = (cpu.ss << 4) + 0x0010
        cpu.mem.write_byte(base + 0, 0xCD)
        cpu.mem.write_byte(base + 1, 0xAB)
        cpu.mem.write_byte(base + 2, 0x34)
        cpu.mem.write_byte(base + 3, 0x12)
        cpu.mem.write_byte(0x0000, 0x36)  # SS:
        cpu.mem.write_byte(0x0001, 0xC5)  # LDS SI, [0x0010]
        cpu.mem.write_byte(0x0002, 0x36)
        cpu.mem.write_byte(0x0003, 0x10)

        cpu.execute()

        assert cpu.si == 0xABCD
        assert cpu.ds == 0x1234

    def test_lds_bx_addressing_uses_bx_not_bp(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.bx = 0x0078
        cpu.bp = 0x0037
        cpu.mem.write_word(0x0078, 0xEFC7)
        cpu.mem.write_word(0x007A, 0xF000)
        cpu.mem.write_byte(0x0000, 0xC5)  # LDS SI, [BX]
        cpu.mem.write_byte(0x0001, 0x37)

        cpu.execute()

        assert cpu.si == 0xEFC7
        assert cpu.ds == 0xF000


class TestRepeatPrefixes:
    def test_repe_cmpsb_repeats_until_cx_zero(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ds = 0
        cpu.es = 0
        cpu.si = 0x0100
        cpu.di = 0x0200
        cpu.cx = 3
        cpu.mem.write_byte(0x0100, ord('A'))
        cpu.mem.write_byte(0x0101, ord('B'))
        cpu.mem.write_byte(0x0102, ord('C'))
        cpu.mem.write_byte(0x0200, ord('A'))
        cpu.mem.write_byte(0x0201, ord('B'))
        cpu.mem.write_byte(0x0202, ord('C'))
        cpu.mem.write_byte(0x0000, 0xF3)  # REPE
        cpu.mem.write_byte(0x0001, 0xA6)  # CMPSB

        cpu.execute()

        assert cpu.cx == 0
        assert cpu.si == 0x0103
        assert cpu.di == 0x0203
        assert cpu.zf is True


class TestShiftWidths:
    def test_d2_shift_register_byte_uses_8bit_operand(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.dx = 0x0201
        cpu.cx = 0x0006
        cpu.mem.write_byte(0x0000, 0xD2)  # SHL r/m8, CL
        cpu.mem.write_byte(0x0001, 0xE6)  # mod=3, /4, rm=DH

        cpu.execute()

        assert cpu.dh == 0x80
        assert cpu.dl == 0x01
        assert cpu.dx == 0x8001

    def test_d0_shr_and_sar_decode_distinct_operations(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 0x0081
        cpu.mem.write_byte(0x0000, 0xD0)  # SHR AL, 1
        cpu.mem.write_byte(0x0001, 0xE8)  # mod=3, /5, rm=AL
        cpu.mem.write_byte(0x0002, 0xD0)  # SAR AL, 1
        cpu.mem.write_byte(0x0003, 0xF8)  # mod=3, /7, rm=AL

        cpu.execute()
        shr_val = cpu.al
        shr_cf = cpu.cf
        cpu.ax = 0x0081
        cpu.execute()

        assert shr_val == 0x40
        assert shr_cf is True
        assert cpu.al == 0xC0
        assert cpu.cf is True

    def test_fe_inc_register_byte(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.dx = 0x0001
        cpu.mem.write_byte(0x0000, 0xFE)  # INC DL
        cpu.mem.write_byte(0x0001, 0xC2)

        cpu.execute()

        assert cpu.dl == 0x02


class TestArithmeticModRM:
    def test_xor_ah_ah_clears_high_byte_only(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 0x12FF
        cpu.mem.write_byte(0x0000, 0x32)  # XOR r8, r/m8
        cpu.mem.write_byte(0x0001, 0xE4)  # AH, AH

        cpu.execute()

        assert cpu.ah == 0
        assert cpu.al == 0xFF

    def test_or_dh_mem_updates_target_register(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ds = 0
        cpu.dx = 0x0000
        cpu.mem.write_byte(0x0100, 0x01)
        cpu.mem.write_byte(0x0000, 0x0A)  # OR r8, r/m8
        cpu.mem.write_byte(0x0001, 0x36)  # DH, [disp16]
        cpu.mem.write_word(0x0002, 0x0100)

        cpu.execute()

        assert cpu.dh == 0x01
        assert cpu.dl == 0x00

    def test_add_mem_ax_updates_same_effective_address(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 7
        cpu.mem.write_word(0x0100, 5)
        cpu.mem.write_byte(0x0000, 0x01)  # ADD [0x0100], AX
        cpu.mem.write_byte(0x0001, 0x06)
        cpu.mem.write_word(0x0002, 0x0100)

        cpu.execute()

        assert cpu.mem.read_word(0x0100) == 12


class TestMulDivGroups:
    def test_f7_mul_word_uses_group4(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 2
        cpu.mem.write_word(0x0100, 2)
        cpu.mem.write_byte(0x0000, 0xF7)  # MUL word ptr [0x0100]
        cpu.mem.write_byte(0x0001, 0x26)  # /4, disp16
        cpu.mem.write_word(0x0002, 0x0100)

        cpu.execute()

        assert cpu.ax == 4
        assert cpu.dx == 0

    def test_f6_div_byte_returns_quotient_in_al_remainder_in_ah(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 0x0011
        cpu.mem.write_byte(0x0100, 4)
        cpu.mem.write_byte(0x0000, 0xF6)  # DIV byte ptr [0x0100]
        cpu.mem.write_byte(0x0001, 0x36)  # /6, disp16
        cpu.mem.write_word(0x0002, 0x0100)

        cpu.execute()

        assert cpu.al == 4
        assert cpu.ah == 1


class TestMovModRM8:
    def test_88_stores_dl_to_memory(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.dx = 0x1234
        cpu.mem.write_byte(0x0000, 0x88)  # MOV [0x2000], DL
        cpu.mem.write_byte(0x0001, 0x16)
        cpu.mem.write_word(0x0002, 0x2000)

        cpu.execute()

        assert cpu.mem.read_byte(0x2000) == 0x34

    def test_8a_loads_dh_from_memory(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.dx = 0x0011
        cpu.mem.write_byte(0x2000, 0x7A)
        cpu.mem.write_byte(0x0000, 0x8A)  # MOV DH, [0x2000]
        cpu.mem.write_byte(0x0001, 0x36)
        cpu.mem.write_word(0x0002, 0x2000)

        cpu.execute()

        assert cpu.dh == 0x7A
        assert cpu.dl == 0x11


class TestSegmentOverrides:
    def test_cs_override_applies_to_moffs_load(self):
        cpu = _make_cpu()
        cpu.cs = 0x0100
        cpu.ip = 0
        cpu.ds = 0x0000
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0000), 0x2E)  # CS:
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0001), 0xA0)  # MOV AL, moffs8
        cpu.mem.write_word(cpu._phys(cpu.cs, 0x0002), 0x0040)
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0040), 0x5A)
        cpu.mem.write_byte(cpu._phys(cpu.ds, 0x0040), 0x11)

        cpu.execute()

        assert cpu.al == 0x5A

    def test_cs_override_applies_to_rep_cmpsw_source(self):
        cpu = _make_cpu()
        cpu.cs = 0x0100
        cpu.ip = 0
        cpu.ds = 0x0200
        cpu.es = 0x0300
        cpu.si = 0x0010
        cpu.di = 0x0020
        cpu.cx = 1
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0000), 0x2E)  # CS:
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0001), 0xF3)  # REP
        cpu.mem.write_byte(cpu._phys(cpu.cs, 0x0002), 0xA7)  # CMPSW
        cpu.mem.write_word(cpu._phys(cpu.cs, 0x0010), 0xBEEF)
        cpu.mem.write_word(cpu._phys(cpu.ds, 0x0010), 0xABCD)
        cpu.mem.write_word(cpu._phys(cpu.es, 0x0020), 0xBEEF)

        cpu.execute()

        assert cpu.zf is True
        assert cpu.cx == 0


class TestFarIndirectTransfers:
    def test_ff_call_far_reads_segment_from_memory(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ss = 0
        cpu.sp = 0x8000
        cpu.mem.write_byte(0x0000, 0xFF)  # CALL FAR [0x0100]
        cpu.mem.write_byte(0x0001, 0x1E)
        cpu.mem.write_word(0x0002, 0x0100)
        cpu.mem.write_word(0x0100, 0x3456)
        cpu.mem.write_word(0x0102, 0x789A)

        cpu.execute()

        assert cpu.ip == 0x3456
        assert cpu.cs == 0x789A
        assert cpu.mem.read_word(0x7FFC) == 0x0004
        assert cpu.mem.read_word(0x7FFE) == 0x0000

    def test_ff_jmp_far_reads_segment_from_memory(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.mem.write_byte(0x0000, 0xFF)  # JMP FAR [0x0100]
        cpu.mem.write_byte(0x0001, 0x2E)
        cpu.mem.write_word(0x0002, 0x0100)
        cpu.mem.write_word(0x0100, 0x1357)
        cpu.mem.write_word(0x0102, 0x2468)

        cpu.execute()

        assert cpu.ip == 0x1357
        assert cpu.cs == 0x2468


class TestGroup1ByteOpcodes:
    def test_80_and_ah_imm8_does_not_modify_sp(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 0xABCD
        cpu.sp = 0x06E6
        cpu.mem.write_byte(0x0000, 0x80)  # AND AH, 0x0F
        cpu.mem.write_byte(0x0001, 0xE4)
        cpu.mem.write_byte(0x0002, 0x0F)

        cpu.execute()

        assert cpu.ah == 0x0B
        assert cpu.al == 0xCD
        assert cpu.sp == 0x06E6

    def test_80_cmp_ah_imm8_sets_flags_from_byte_compare(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.ax = 0x0A00
        cpu.sp = 0x1234
        cpu.mem.write_byte(0x0000, 0x80)  # CMP AH, 0x0A
        cpu.mem.write_byte(0x0001, 0xFC)
        cpu.mem.write_byte(0x0002, 0x0A)

        cpu.execute()

        assert cpu.zf is True
        assert cpu.cf is False
        assert cpu.sp == 0x1234


class TestImmediatePortIO:
    def test_e4_in_al_imm8_reads_byte_port(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        cpu.io.inb = lambda port: 0x5A
        cpu.mem.write_byte(0x0000, 0xE4)
        cpu.mem.write_byte(0x0001, 0x20)

        cpu.execute()

        assert cpu.al == 0x5A

    def test_e6_out_imm8_al_writes_byte_port(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        calls = []
        cpu.io.outb = lambda port, value: calls.append((port, value))
        cpu.al = 0x34
        cpu.mem.write_byte(0x0000, 0xE6)
        cpu.mem.write_byte(0x0001, 0x20)

        cpu.execute()

        assert calls == [(0x20, 0x34)]

    def test_e7_out_imm8_ax_writes_word_port(self):
        cpu = _make_cpu()
        cpu.cs = 0
        cpu.ip = 0
        calls = []
        cpu.io.outw = lambda port, value: calls.append((port, value))
        cpu.ax = 0xBEEF
        cpu.mem.write_byte(0x0000, 0xE7)
        cpu.mem.write_byte(0x0001, 0x21)

        cpu.execute()

        assert calls == [(0x21, 0xBEEF)]
