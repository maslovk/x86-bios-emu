"""DPMI distinguishes PC and Tandy BIOSes using INT 1Ah date services."""

from main import Emulator


def test_rtc_date_returns_bcd_century_year_month_day(monkeypatch):
    emu = Emulator()
    monkeypatch.setattr(emu.cmos, 'get_date_bcd', lambda: {
        'century': 0x20, 'year': 0x26, 'month': 0x09, 'day': 0x12,
        'weekday': 0x06,
    })
    emu.cpu.ax = 0x0400
    emu.cpu.cf = True
    emu.bios._int1ah(emu.cpu)
    assert (emu.cpu.cx, emu.cpu.dx) == (0x2026, 0x0912)
    assert not emu.cpu.cf


def test_unsupported_tandy_rtc_service_reports_failure():
    emu = Emulator()
    emu.cpu.ax = 0x1200
    emu.cpu.cf = False
    emu.bios._int1ah(emu.cpu)
    assert emu.cpu.cf
    assert emu.cpu.ah == 0x86
