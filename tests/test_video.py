"""Unit tests for video.py — VGA, Serial, IO, Keyboard, Disk."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from video import Video, IO, Keyboard, Disk, Serial
from hardware import PIT, PIC
from tests.conftest import Mem


# ── Video ───────────────────────────────────────────────────────

class TestVideo:
    def test_init(self, video):
        assert video.width == 80
        assert video.height == 25
        assert video.cur_x == 0
        assert video.cur_y == 0
        assert video.mode == 3
        assert len(video.buffer) == 25
        assert len(video.buffer[0]) == 80

    def test_write_char(self, video):
        video.write(10, 5, ord('A'), 0x0F)
        ch, attr = video.buffer[5][10]
        assert ch == ord('A')
        assert attr == 0x0F

    def test_write_out_of_bounds(self, video):
        video.write(-1, 0, ord('X'))
        video.write(80, 0, ord('X'))
        video.write(0, -1, ord('X'))
        video.write(0, 25, ord('X'))

    def test_putc_normal(self, video):
        video.cur_x = 0; video.cur_y = 0
        video.putc(ord('H'), 0x09)
        ch, attr = video.buffer[0][0]
        assert ch == ord('H')
        assert attr == 0x09
        assert video.cur_x == 1

    def test_putc_newline(self, video):
        video.cur_x = 10; video.cur_y = 5
        video.putc(0x0A)
        assert video.cur_x == 0
        assert video.cur_y == 6

    def test_putc_carriage_return(self, video):
        video.cur_x = 10; video.cur_y = 5
        video.putc(0x0D)
        assert video.cur_x == 0
        assert video.cur_y == 5

    def test_putc_backspace(self, video):
        video.cur_x = 10; video.cur_y = 5
        video.putc(0x08)
        assert video.cur_x == 9

    def test_putc_wrap_line(self, video):
        video.cur_x = 79; video.cur_y = 5
        video.putc(ord('X'))
        assert video.cur_x == 0
        assert video.cur_y == 6

    def test_putc_null(self, video):
        video.cur_x = 5; video.cur_y = 3
        video.putc(0)
        assert video.cur_x == 5

    def test_scroll(self, video):
        # scroll moves buffer[y] = buffer[y+1], so row 1 → row 0
        video.write(0, 1, ord('A'))
        video.write(0, 24, ord('Z'))
        video.scroll()
        ch, _ = video.buffer[0][0]
        assert ch == ord('A')  # was at row 1, now at row 0
        ch, _ = video.buffer[24][0]
        assert ch == 0x20  # last row cleared to space

    # ── scroll + memory sync (regression for GTK/terminal scroll bug) ──
    #
    # video.scroll() used to update only self.buffer, not the VRAM at
    # 0xB8000.  display() and GTK both call _sync_from_memory() first, so
    # the scrolled buffer was overwritten with stale un-scrolled memory --
    # meaning every screen fill (e.g. typing commands at DOS's A> prompt)
    # lost the scroll and left old rows stuck on top.

    def test_scroll_syncs_buffer_to_vram(self):
        mem = Mem()
        video = Video()
        video.attach_memory(mem)
        # Put a marker on row 1 that should move to row 0 after scroll.
        video.write(0, 1, ord('M'), 0x0A)
        # Fill row 0 with junk so we can prove the scroll replaced it.
        for x in range(80):
            video.write(x, 0, ord('X'), 0x07)
        video.scroll()
        # Memory at 0xB8000 (row 0, col 0) must now hold the scrolled-up 'M'.
        addr = 0xB8000
        assert mem.read_byte(addr) == ord('M')
        assert mem.read_byte(addr + 1) == 0x0A
        # Memory at the bottom row must be cleared (space).
        bottom = 0xB8000 + (24 * 80) * 2
        assert mem.read_byte(bottom) == 0x20

    def test_putc_wrapping_triggers_memory_synced_scroll(self):
        # Filling past the bottom via putc must scroll and the scrolled
        # content must reach VRAM (the path GTK display reads from).
        mem = Mem()
        video = Video()
        video.attach_memory(mem)
        video.cur_x = 0; video.cur_y = 0
        video.print_str("TOP")       # row 0
        # Position at the last row, write enough chars to wrap onto a
        # 25th (non-existent) row; putc must scroll to keep the buffer in
        # VRAM synced.
        video.cur_y = video.height - 1
        video.cur_x = 0
        video.print_str("X" * (video.width + 5))   # wraps past the bottom
        # After the wrap+scroll, row 0 of VRAM should no longer be 'TOP'.
        addr = 0xB8000
        assert mem.read_byte(addr) != ord('T'), "scroll did not propagate to VRAM"

    def test_print_str(self, video):
        video.cur_x = 0; video.cur_y = 0
        video.print_str("Hi", 0x09)
        ch, _ = video.buffer[0][0]
        assert ch == ord('H')
        ch, _ = video.buffer[0][1]
        assert ch == ord('i')

    def test_print_str_at_pos(self, video):
        video.print_str("X", 0x0F, x=10, y=5)
        ch, _ = video.buffer[5][10]
        assert ch == ord('X')

    def test_clear(self, video):
        video.write(0, 0, ord('X'))
        video.clear()
        ch, _ = video.buffer[0][0]
        assert ch == 0x20
        assert video.cur_x == 0
        assert video.cur_y == 0

    def test_attributes(self, video):
        assert Video.ATTR_NORMAL == 0x07
        assert Video.ATTR_WHITE == 0x0F
        assert Video.ATTR_GREEN == 0x09
        assert Video.ATTR_RED == 0x0C

    def test_write_mirrors_to_attached_memory(self):
        mem = Mem()
        video = Video()
        video.attach_memory(mem)

        video.write(10, 5, ord('A'), 0x0F)

        addr = 0xB8000 + ((5 * 80 + 10) * 2)
        assert mem.read_byte(addr) == ord('A')
        assert mem.read_byte(addr + 1) == 0x0F

    def test_display_syncs_from_attached_memory(self, monkeypatch, capsys):
        mem = Mem()
        video = Video()
        video.attach_memory(mem)
        addr = 0xB8000
        mem.write_byte(addr, ord('D'))
        mem.write_byte(addr + 1, 0x0A)
        monkeypatch.setattr('os.system', lambda *_: 0)

        video.display()

        ch, attr = video.buffer[0][0]
        assert ch == ord('D')
        assert attr == 0x0A
        assert 'D' in capsys.readouterr().out

    # ── display() rendering invariants (regression for ugly-output bug) ──

    def _display_lines(self, video, capsys, isatty=True):
        """Run display() with a fake isatty() and return the printed lines."""
        import video as video_mod
        orig = sys.stdout.isatty
        sys.stdout.isatty = lambda: isatty
        try:
            video.display()
        finally:
            sys.stdout.isatty = orig
        return capsys.readouterr().out.splitlines()

    def test_display_border_width_is_consistent(self, capsys):
        # Top divider, title row, and body rows must all have the same
        # visible width so the right edge of the box lines up.
        video = Video()
        video.print_str("Hello", Video.ATTR_NORMAL, 0, 0)
        lines = self._display_lines(video, capsys, isatty=False)
        # Strip ANSI (none expected when not a TTY) and measure visible width.
        widths = [len(l) for l in lines if l.startswith('║') or l.startswith('╔') or l.startswith('╚')]
        assert widths, "expected box-drawing lines"
        assert len(set(widths)) == 1, f"box rows have differing widths: {set(widths)}"

    def test_display_body_row_has_expected_padding(self, capsys):
        # Each body row is: ║ + 2 spaces + 80 VGA cols + 2 spaces + ║ = 86 chars.
        video = Video()
        video.print_str("AB", Video.ATTR_NORMAL, 0, 0)
        lines = self._display_lines(video, capsys, isatty=False)
        body = [l for l in lines if 'AB' in l][0]
        assert len(body) == 86
        assert body[0] == '║' and body[-1] == '║'
        assert body[3:5] == 'AB'   # printed at column 0

    def test_display_omits_ansi_when_not_a_tty(self, capsys):
        # Redirected output must be plain text (no escape codes at all,
        # including the clear-screen sequence) so it stays readable in pipes/logs.
        video = Video()
        video.print_str("X", Video.ATTR_RED, 0, 0)
        out = '\n'.join(self._display_lines(video, capsys, isatty=False))
        assert '\033[' not in out
        assert 'X' in out

    def test_display_batches_color_runs_when_tty(self, capsys):
        # A run of same-colour cells must share ONE escape, not one per char.
        video = Video()
        # Row 0: 5 red 'A's, 5 green 'B's, 5 red 'C's, then blank (attr 0x07)
        # for the remaining 65 cells = 4 colour runs total.
        for x in range(5):
            video.write(x, 0, ord('A'), Video.ATTR_RED)
        for x in range(5, 10):
            video.write(x, 0, ord('B'), Video.ATTR_GREEN)
        for x in range(10, 15):
            video.write(x, 0, ord('C'), Video.ATTR_RED)
        lines = self._display_lines(video, capsys, isatty=True)
        body = [l for l in lines if 'AAAAA' in l][0]
        # 4 colour-run escapes (red, green, red, white-blank) + 1 reset = 5.
        # Per-char rendering would emit ~80 escapes; batching keeps it to 5.
        assert body.count('\033[') == 5

    def test_display_renders_blank_cell_as_space(self, capsys):
        # A zero/garbage byte must not emit a control char into the terminal.
        video = Video()
        video.write(0, 0, 0x00, Video.ATTR_NORMAL)        # NUL
        video.write(1, 0, 0x9B, Video.ATTR_NORMAL)        # outside printable range
        lines = self._display_lines(video, capsys, isatty=False)
        body = [l for l in lines if l.startswith('║  ')][0]
        assert body[3:5] == '  '   # both cells rendered as spaces

    def test_display_only_clears_via_ansi_not_os_system(self, monkeypatch, capsys):
        # Must not shell out to `clear`/`cls` (flicker + external dep); the
        # ANSI clear sequence is written to stdout instead (when a TTY).
        called = []
        monkeypatch.setattr('os.system', lambda *a: called.append(a))
        orig = sys.stdout.isatty
        sys.stdout.isatty = lambda: True
        try:
            Video().display()
        finally:
            sys.stdout.isatty = orig
        assert called == []
        assert '\033[2J' in capsys.readouterr().out


# ── Serial ──────────────────────────────────────────────────────

class TestSerial:
    def test_init(self, serial):
        assert serial.baud == 9600
        assert serial.line_ctrl == 0x03
        assert serial.lsr & 0x20

    def test_transmit_char(self, serial, capsys):
        serial.outb(0x00, ord('A'))
        captured = capsys.readouterr()
        assert '[COM1] A' in captured.err

    def test_transmit_newline(self, serial, capsys):
        serial.outb(0x00, 0x0A)
        captured = capsys.readouterr()
        assert '\n' in captured.err

    def test_transmit_cr(self, serial, capsys):
        serial.outb(0x00, 0x0D)
        captured = capsys.readouterr()
        assert '\r' in captured.err

    def test_transmit_nonprintable(self, serial, capsys):
        serial.outb(0x00, 0x01)
        captured = capsys.readouterr()
        assert '[COM1]' not in captured.err

    def test_read_lsr(self, serial):
        val = serial.inb(0x04)
        assert val & 0x20

    def test_read_msr(self, serial):
        val = serial.inb(0x05)
        assert val & 0x20

    def test_read_iir(self, serial):
        val = serial.inb(0x02)
        assert val == 0x01

    def test_write_lcr(self, serial):
        serial.outb(0x04, 0x07)
        assert serial.line_ctrl == 0x07


# ── Keyboard ────────────────────────────────────────────────────

class TestKeyboard:
    def test_empty_buffer(self, kbd):
        assert kbd.key_pressed() is False
        assert kbd.read_key() == 0

    def test_feed_string(self, kbd):
        kbd.feed_string("AB")
        assert kbd.key_pressed() is True
        assert kbd.read_key() == ord('A')
        assert kbd.read_key() == ord('B')

    def test_feed_adds_enter(self, kbd):
        kbd.feed_string("A")
        kbd.read_key()  # A
        assert kbd.read_key() == 0x0D

    def test_multiple_feeds(self, kbd):
        kbd.feed_string("A")
        kbd.feed_string("B")
        keys = []
        while kbd.key_pressed():
            keys.append(kbd.read_key())
        assert keys[0] == ord('A')


# ── Disk ────────────────────────────────────────────────────────

class TestDisk:
    def test_init(self, disk):
        assert len(disk.sectors) == 2880
        assert len(disk.sectors[0]) == 512

    def test_read_empty_sector(self, disk):
        buf = bytearray(512)
        assert disk.read_sector(0, buf) is True
        assert all(b == 0 for b in buf)

    def test_write_read_sector(self, disk):
        data = bytes(range(256)) * 2
        disk.write_sector(10, data)
        buf = bytearray(512)
        disk.read_sector(10, buf)
        assert bytes(buf) == data

    def test_write_boot_sector(self, disk):
        code = bytes([0xEB, 0x3E, 0x90] + [0] * 507 + [0x55, 0xAA])
        disk.write_boot_sector(code)
        buf = bytearray(512)
        disk.read_sector(0, buf)
        assert buf[510] == 0x55
        assert buf[511] == 0xAA

    def test_read_out_of_range(self, disk):
        buf = bytearray(512)
        result = disk.read_sector(9999, buf)
        assert result is False


# ── IO Ports ────────────────────────────────────────────────────

class TestIO:
    def test_keyboard_data_port(self, io_ports):
        io_ports.kbd.feed_string("X")
        val = io_ports.inb(0x60)
        assert val == ord('X')

    def test_keyboard_status_port(self, io_ports):
        val = io_ports.inb(0x64)
        assert val == 0x01

    def test_keyboard_status_with_key(self, io_ports):
        io_ports.kbd.feed_string("A")
        val = io_ports.inb(0x64)
        assert val == 0x00

    def test_unknown_port_returns_zero(self, io_ports):
        assert io_ports.inb(0xFF) == 0x00

    def test_diagnostic_port(self, io_ports):
        io_ports.outb(0x80, 0xAB)
        assert io_ports.inb(0x80) == 0x00

    def test_serial_inb(self, io_ports):
        val = io_ports.inb(0x3F8)
        assert val == 0

    def test_serial_outb(self, io_ports, capsys):
        io_ports.outb(0x3F8, ord('T'))
        captured = capsys.readouterr()
        assert '[COM1] T' in captured.err

    def test_inw(self, io_ports):
        val = io_ports.inw(0x80)
        assert val == 0

    def test_outw(self, io_ports):
        io_ports.outw(0x80, 0x1234)

    def test_tick_routes_only_pit_channel0_to_pic(self):
        video = Video()
        kbd = Keyboard()
        disk = Disk()
        serial = Serial()
        pit = PIT()
        pic = PIC()
        io = IO(video, kbd, disk, serial, pit=pit, pic=pic)

        io.tick(1 / 18.2)

        assert io._pit_pending_irqs == [0]
        assert pic.irr == 0x01
