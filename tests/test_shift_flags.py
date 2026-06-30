"""Regression tests for x86 shift/rotate flag semantics (cpu.py::_do_shift).

These pin down a DOS-3.3 boot bug: scalar SHL/SHR/SAR/SAL must update the
SF, ZF, and PF flags based on the result.  Before the fix, _do_shift only
updated CF and OF, leaving SF/ZF/PF stale from prior instructions.  This
caused DOS's OPEN-CON path (which does `MOV BL,AH / SHL BX,1` after
`XOR BH,BH`) to read stale ZF/PF and mis-dispatch, ultimately returning
file_not_found for every device open.

Reference behaviour cross-checked against Unicorn (QEMU-based) emulation
of the identical DOS state.
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


class TestShiftFlags:
    """SHL/SHR/SAR/SAL must set SF/ZF/PF from the result (CF/OF already set)."""

    def test_shl_bx_1_sets_pf_from_result(self):
        """Exact regression: SHL BX,1 with BH=0,BL=0x3D -> BX=0x7A.

        0x7A = 0111 1010 -> 5 set bits -> odd parity -> PF=0, ZF=0, SF=0.
        Before the fix PF/ZF stayed at their pre-XOR values (PF=1, ZF=1).
        """
        cpu = make_cpu([0xD1, 0xE3])  # D1 E3 = SHL BX, 1
        cpu.ax = 0x3D00
        # Simulate prior `XOR BH,BH` which set ZF=1, PF=1 (and BH already 0).
        cpu.bx = 0x0000  # set BH=0 first
        cpu.bx = 0x003D  # then MOV BL,AH -> BL=0x3D
        cpu.zf = True
        cpu.pf = True
        cpu.sf = False
        cpu.cf = False
        cpu.execute()
        assert cpu.bx == 0x007A
        assert cpu.zf is False, "SHL of nonzero must clear ZF"
        assert cpu.pf is False, "SHL result 0x7A has odd parity -> PF=0"
        assert cpu.sf is False, "SHL result MSB clear -> SF=0"
        assert cpu.cf is False, "no bit shifted out of 0x003D -> CF=0"

    def test_shl_result_zero_sets_zf(self):
        """SHL BX,1 of 0x8000 -> 0x0000: ZF must be set, PF set (parity of 0 is even)."""
        cpu = make_cpu([0xD1, 0xE3])  # SHL BX, 1
        cpu.bx = 0x8000
        cpu.zf = False; cpu.pf = False; cpu.sf = True
        cpu.execute()
        assert cpu.bx == 0x0000
        assert cpu.zf is True, "SHL to zero must set ZF"
        assert cpu.pf is True, "result low byte 0 -> even parity -> PF=1"
        assert cpu.sf is False, "result MSB clear -> SF=0"
        assert cpu.cf is True, "bit 15 of 0x8000 shifted out -> CF=1"

    def test_shl_sets_sf_for_negative_word_result(self):
        """SHL BX,1 of 0x4080 -> 0x8100: SF=1, ZF=0, PF=0 (low byte 0x00 even)."""
        cpu = make_cpu([0xD1, 0xE3])
        cpu.bx = 0x4080
        cpu.zf = True; cpu.pf = True; cpu.sf = False
        cpu.execute()
        assert cpu.bx == 0x8100
        assert cpu.sf is True, "result MSB set -> SF=1"
        assert cpu.zf is False
        assert cpu.pf is True, "low byte 0x00 -> even parity -> PF=1"

    def test_shr_sets_zf_and_pf(self):
        """SHR AL,1 of 0x01 -> 0x00: ZF=1, PF=1, CF=1, SF=0."""
        cpu = make_cpu([0xD0, 0xE8])  # D0 E8 = SHR AL, 1
        cpu.ax = 0x0001
        cpu.zf = False; cpu.pf = False; cpu.sf = True
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0x00
        assert cpu.zf is True
        assert cpu.pf is True
        assert cpu.sf is False
        assert cpu.cf is True

    def test_sar_preserves_sf_for_signed_shift(self):
        """SAR AL,1 of 0x80 -> 0xC0: SF=1 (sign bit preserved), CF=0."""
        cpu = make_cpu([0xD0, 0xF8])  # SAR AL,1
        cpu.ax = 0x0080
        cpu.zf = True; cpu.pf = True; cpu.sf = False
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0xC0
        assert cpu.sf is True, "SAR preserves sign -> SF=1"
        assert cpu.zf is False
        assert cpu.cf is False, "low bit of 0x80 was 0 -> CF=0"

    def test_shl_count_2_still_updates_arithmetic_flags(self):
        """SHL AX,CL with CL=2: arithmetic flags reflect final result."""
        cpu = make_cpu([0xD3, 0xE0])  # D3 E0 = SHL AX, CL
        cpu.ax = 0x2010
        cpu.cx = 0x0002  # CL=2
        cpu.zf = True; cpu.pf = True; cpu.sf = False
        cpu.execute()
        assert cpu.ax == 0x8040
        assert cpu.sf is True, "0x8040 MSB set -> SF=1"
        assert cpu.zf is False
        # OF is undefined for count != 1, but the arithmetic flags must match

    def test_rol_does_not_touch_zf_pf_sf(self):
        """ROL only updates CF/OF; it must NOT modify SF/ZF/PF (per Intel SDM).

        This guards against an over-broad fix that would also clobber flags
        on rotates, which would break other callers.
        """
        cpu = make_cpu([0xD1, 0xC0])  # D1 C0 = ROL AX, 1
        cpu.ax = 0x8001
        # Pretend prior instructions set SF=0, ZF=1, PF=0.
        cpu.sf = False; cpu.zf = True; cpu.pf = False
        cpu.execute()
        assert cpu.ax == 0x0003, "ROL of 0x8001 wraps MSB into LSB -> 0x0003"
        assert cpu.cf is True, "ROL shifts MSB into CF"
        # Rotates preserve SF/ZF/PF (undefined per SDM, but our impl leaves them).
        assert cpu.zf is True, "ROL must not clobber ZF"
        assert cpu.sf is False, "ROL must not clobber SF"
        assert cpu.pf is False, "ROL must not clobber PF"


class TestXlatSegmentOverride:
    """XLAT reads AL from [seg:BX+AL]. The default seg is DS, but a
    segment-override prefix (ES:/CS:/SS:/DS:) MUST redirect the read.

    Regression for a DOS-3.3 OPEN-CON bug: a `CS: XLAT` instruction at
    023E:5532 was reading from DS:BX+AL (wrong) instead of CS:BX+AL,
    returning the wrong country-info table byte and corrupting every
    subsequent device/file open.  Cross-checked against Unicorn.
    """

    def _setup_table(self, cpu, seg, base, table_bytes):
        """Write table_bytes at seg:base (mod 0x10 segment granularity)."""
        for i, b in enumerate(table_bytes):
            cpu.mem.write_byte(((seg << 4) + base + i) & 0xFFFFF, b)

    def test_xlat_default_segment_is_ds(self):
        cpu = make_cpu([0xD7])            # XLAT, no prefix
        cpu.ds = 0x0000; cpu.bx = 0x0100; cpu.ax = 0x0003
        self._setup_table(cpu, 0x0000, 0x0100, b'ABCDE')
        cpu.execute()
        assert (cpu.ax & 0xFF) == ord('D'), "XLAT reads DS:[BX+AL] = 'D'"

    def test_cs_override_redirects_xlat_to_cs(self):
        # 2E D7 = CS: prefix + XLAT.  Code placed at CS:IP (CS=0x0100, IP=0x7C00).
        cpu = make_cpu([0x2E, 0xD7], cs=0x0100, ip=0x7C00)
        cpu.ds = 0x0000; cpu.ss = 0x0000; cpu.es = 0x0000
        cpu.bx = 0x0010; cpu.ax = 0x0002
        # Write different bytes at DS:0x0012 and CS:0x0012
        cpu.mem.write_byte(0x0012, 0xAA)   # DS-relative (must NOT be read)
        cpu.mem.write_byte((0x0100 << 4) + 0x0012, 0xBB)  # CS:0x0012 (must be read)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0xBB, f"CS: XLAT must read CS:[BX+AL]; got 0x{cpu.ax&0xFF:02X}"

    def test_es_override_redirects_xlat_to_es(self):
        cpu = make_cpu([0x26, 0xD7])      # ES: prefix + XLAT
        cpu.ds = 0x0000; cpu.ss = 0x0000; cpu.cs = 0x0000
        cpu.es = 0x0200; cpu.bx = 0x0000; cpu.ax = 0x0001
        cpu.mem.write_byte(0x0001, 0x11)              # DS (wrong)
        cpu.mem.write_byte((0x0200 << 4) + 0x0001, 0x22)  # ES (correct)
        cpu.execute()
        assert (cpu.ax & 0xFF) == 0x22
