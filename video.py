"""
Simple BIOS Emulator - Video and I/O
=====================================
VGA text-mode video (80x25) and I/O port emulation.
"""

import os
import sys

try:  # Optional fast path for large EGA/VGA framebuffer expansion.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised on minimal installs
    _np = None


# Power-on DAC entries 0-15 for the standard IBM EGA/VGA palette.  DAC
# components are kept in the renderer's 0-255 range (writes to 3C9 expand
# their native 6-bit values the same way).
_VGA_16_PALETTE = (
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA),
    (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA),
    (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF),
    (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF),
    (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
)


def _ega_rgb(index):
    """Expand an EGA six-bit ``rgbRGB`` palette value to 8-bit RGB."""
    index &= 0x3F
    return (
        (0xAA if index & 0x04 else 0) + (0x55 if index & 0x20 else 0),
        (0xAA if index & 0x02 else 0) + (0x55 if index & 0x10 else 0),
        (0xAA if index & 0x01 else 0) + (0x55 if index & 0x08 else 0),
    )


_EGA_64_PALETTE = tuple(_ega_rgb(index) for index in range(64))
_VGA_DEFAULT_ATTR_PALETTE = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x14, 0x07,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
)

# Expand an eight-pixel planar byte into eight little-endian byte lanes of a
# native 64-bit word.  The renderer combines one value per plane, avoiding a
# Python loop for every individual EGA pixel.
_PLANAR_LANES = tuple(
    sum(((value >> (7 - pixel)) & 1) << (pixel * 8)
        for pixel in range(8))
    for value in range(256)
)


def decode_vga_char(ch):
    """Decode printable ASCII and the CP437 box/shading glyphs used by DOS."""
    if 0x20 <= ch <= 0x7E:
        return chr(ch)
    # DOS text-mode UI frames conventionally use this CP437 range. Keep
    # unrelated high bytes (for example 0x9B) blank rather than leaking
    # arbitrary control/symbol characters into terminal transcripts.
    if 0xB0 <= ch <= 0xDF:
        return bytes((ch,)).decode('cp437')
    return ' '


class Video:
    """VGA text mode plus a practical EGA/VGA graphics framebuffer."""

    MODES = {
        0x0D: (320, 200, 16, True),
        0x0E: (640, 200, 16, True),
        0x0F: (640, 350, 2, True),
        0x10: (640, 350, 16, True),
        0x11: (640, 480, 2, True),
        0x12: (640, 480, 16, True),
        0x13: (320, 200, 256, False),
    }

    # IBM VGA mode-3 defaults for CRTC registers 00h-18h.  The renderer only
    # needs the text geometry, but DOS applications also probe these ports to
    # identify the adapter and save/restore cursor state.
    _MODE3_CRTC = (
        0x5F, 0x4F, 0x50, 0x82, 0x55, 0x81, 0xBF, 0x1F,
        0x00, 0x4F, 0x0D, 0x0E, 0x00, 0x00, 0x00, 0x00,
        0x9C, 0x8E, 0x8F, 0x28, 0x1F, 0x96, 0xB9, 0xA3,
        0xFF,
    )

    ATTR_NORMAL = 0x07
    ATTR_WHITE  = 0x0F
    ATTR_GREEN  = 0x09
    ATTR_CYAN   = 0x0A
    ATTR_RED    = 0x0C
    ATTR_YELLOW = 0x0E

    def __init__(self):
        self.width = 80
        self.height = 25
        self.buffer = [[(0, self.ATTR_NORMAL) for _ in range(self.width)]
                       for _ in range(self.height)]
        self.cur_x = 0
        self.cur_y = 0
        self.mode = 3
        self.graphics_mode = False
        self.graphics_width = 80
        self.graphics_height = 25
        self.graphics_colors = 16
        self.graphics_planes = [bytearray(0x10000) for _ in range(4)]
        self.graphics_vram = bytearray(0x10000)
        # GTK keeps a packed display surface.  Mark it stale only when guest
        # VRAM or palette state changes; merely repainting a window must not
        # re-expand the complete planar framebuffer.
        self.graphics_dirty = True
        self.seq_index = 2
        self.seq_regs = [0, 0, 0, 0, 0]
        self.gdc_index = 8
        self.gdc_regs = [0] * 16
        self.gdc_regs[8] = 0xFF
        # A VGA memory read loads all four plane bytes into these latches.
        # Graphics-controller write modes combine new CPU data with them.
        self.graphics_latches = [0, 0, 0, 0]
        # VGA attribute-controller palette registers.  The 16 EGA pixel
        # values reach the DAC through this extra indirection.
        self.attr_index = 0
        self.attr_flip_flop = False
        self.attr_palette = list(_VGA_DEFAULT_ATTR_PALETTE)
        self.attr_mode_control = 0x0C
        self.attr_color_select = 0
        self.misc_output = 0x67
        self.dac_mask = 0xFF
        self.dac_index = 0
        self.dac_component = 0
        self.palette = (list(_EGA_64_PALETTE)
                        + [(i, i, i) for i in range(64, 256)])
        self.mem = None
        self.text_base = 0xB8000
        self.crtc_index = 0
        self.crtc_registers = list(self._MODE3_CRTC) + [0] * 7
        self._status_reads = 0
        # Optional callback invoked with a stripped text line each time a row
        # scrolls off the top of the visible 80x25 window.  Harnesses use it to
        # accumulate a full scrollback transcript, since output longer than 25
        # rows is otherwise lost (only the final visible screen survives).
        self.on_scroll_line = None

    def set_mode(self, mode):
        """Select a BIOS video mode and clear its visible memory."""
        self.mode = mode & 0xFF
        spec = self.MODES.get(self.mode)
        self.graphics_mode = spec is not None
        if spec:
            self.graphics_width, self.graphics_height, self.graphics_colors, self._planar = spec
            self.width, self.height = self.graphics_width, self.graphics_height
            self.seq_regs[2] = 0x0F
            self.clear_graphics()
        else:
            self.graphics_width, self.graphics_height = 80, 25
            self.graphics_colors = 16
            self._planar = False
            self.width, self.height = 80, 25
            self.clear()

    def clear_graphics(self):
        for plane in self.graphics_planes:
            plane[:] = b'\0' * len(plane)
        self.graphics_vram[:] = b'\0' * len(self.graphics_vram)
        self.graphics_latches[:] = [0, 0, 0, 0]
        self.graphics_dirty = True

    def graphics_read(self, offset):
        offset &= 0xFFFF
        if not self._planar:
            return self.graphics_vram[offset]
        for plane in range(4):
            self.graphics_latches[plane] = self.graphics_planes[plane][offset]
        if self.gdc_regs[5] & 0x08:  # read mode 1: colour compare
            compare = self.gdc_regs[2] & 0x0F
            dont_care = self.gdc_regs[7] & 0x0F
            matches = 0xFF
            for plane, latch in enumerate(self.graphics_latches):
                if dont_care & (1 << plane):
                    matches &= ~(latch ^ (0xFF if compare & (1 << plane) else 0)) & 0xFF
            return matches
        return self.graphics_latches[self.gdc_regs[4] & 3]

    def graphics_write(self, offset, value):
        """Apply the VGA graphics-controller write pipeline to an A0000 write."""
        offset &= 0xFFFF
        value &= 0xFF
        if not self._planar:
            self.graphics_vram[offset] = value
            self.graphics_dirty = True
            return
        bit_mask = self.gdc_regs[8] & 0xFF
        set_reset = self.gdc_regs[0] & 0x0F
        enable_set_reset = self.gdc_regs[1] & 0x0F
        rotate_function = self.gdc_regs[3]
        rotate = rotate_function & 7
        if rotate:
            value = ((value >> rotate) | (value << (8 - rotate))) & 0xFF
        write_mode = self.gdc_regs[5] & 3

        def logical(source, latch):
            operation = (rotate_function >> 3) & 3
            if operation == 1:
                return source & latch
            if operation == 2:
                return source | latch
            if operation == 3:
                return source ^ latch
            return source

        for plane in range(4):
            if not (self.seq_regs[2] & (1 << plane)):
                continue
            latch = self.graphics_latches[plane]
            if write_mode == 1:
                result = latch
            else:
                if write_mode == 0:
                    source = (0xFF if set_reset & (1 << plane) else 0)
                    if not (enable_set_reset & (1 << plane)):
                        source = value
                elif write_mode == 2:
                    source = 0xFF if value & (1 << plane) else 0
                else:  # mode 3: rotated data selects bits from set/reset.
                    source = 0xFF if set_reset & (1 << plane) else 0
                mask = bit_mask & value if write_mode == 3 else bit_mask
                result = ((latch & ~mask) | (logical(source, latch) & mask))
            self.graphics_planes[plane][offset] = result & 0xFF
        self.graphics_dirty = True

    def graphics_copy_mode1(self, source, destination, count):
        """Bulk-copy a forward, non-overlapping planar write-mode-1 blit.

        A ``REP MOVS`` whose source and destination both live in A000h loads
        all four source latches then writes the selected planes at the target.
        This is equivalent to a plane-wise slice copy, avoiding Python work
        for every byte in common BGI sprite blits.  Overlapping forward copies
        intentionally return False: their byte-at-a-time propagation semantics
        must remain on the reference path.
        """
        if (not self._planar or (self.gdc_regs[5] & 3) != 1 or count <= 0
                or source < 0 or destination < 0
                or source + count > 0x10000 or destination + count > 0x10000):
            return False
        if source < destination < source + count:
            return False
        for plane, memory in enumerate(self.graphics_planes):
            if self.seq_regs[2] & (1 << plane):
                memory[destination:destination + count] = \
                    memory[source:source + count]
        final = source + count - 1
        for plane, memory in enumerate(self.graphics_planes):
            self.graphics_latches[plane] = memory[final]
        self.graphics_dirty = True
        return True

    def graphics_pixel(self, x, y):
        if not self.graphics_mode or not (0 <= x < self.graphics_width
                                           and 0 <= y < self.graphics_height):
            return 0
        if not self._planar:
            return self.graphics_vram[y * self.graphics_width + x]
        offset = y * ((self.graphics_width + 7) // 8) + (x >> 3)
        bit = 7 - (x & 7)
        return sum(((self.graphics_planes[p][offset] >> bit) & 1) << p
                   for p in range(4))

    def graphics_pixels(self):
        if not self.graphics_mode:
            return None
        if not self._planar:
            size = self.graphics_width * self.graphics_height
            return bytes(self.graphics_vram[:size])

        # Expand eight planar pixels at once into native byte lanes.  This
        # keeps EGA redraws responsive while a game changes most of the frame.
        width = self.graphics_width
        row_bytes = (width + 7) // 8
        plane_bytes = row_bytes * self.graphics_height
        p0, p1, p2, p3 = self.graphics_planes
        if _np is not None:
            expanded = _np.unpackbits(
                _np.frombuffer(p0, dtype=_np.uint8, count=plane_bytes),
                bitorder='big')
            for plane, shift in ((p1, 1), (p2, 2), (p3, 3)):
                expanded |= (_np.unpackbits(
                    _np.frombuffer(plane, dtype=_np.uint8, count=plane_bytes),
                    bitorder='big') << shift)
            return expanded.tobytes()
        pixels = bytearray(width * self.graphics_height)
        words = memoryview(pixels).cast('Q')
        out = 0
        for y in range(self.graphics_height):
            base = y * row_bytes
            for column in range(row_bytes):
                index = base + column
                words[out] = (_PLANAR_LANES[p0[index]]
                              | (_PLANAR_LANES[p1[index]] << 1)
                              | (_PLANAR_LANES[p2[index]] << 2)
                              | (_PLANAR_LANES[p3[index]] << 3))
                out += 1
        return bytes(pixels)

    def graphics_rgb(self, color):
        """Return the DAC RGB triplet selected by a 4-bit EGA pixel."""
        palette = self.attr_palette[color & 0x0F] & 0x3F
        select = self.attr_color_select & 0x0F
        # Colour Select supplies DAC bits 7-6.  With P54S (attribute mode
        # control bit 7), it also replaces palette-register bits 5-4.
        dac_index = (palette & 0x0F) | ((select & 0x0C) << 4)
        dac_index |= ((select & 0x03) << 4 if self.attr_mode_control & 0x80
                      else palette & 0x30)
        return self.palette[dac_index]

    def reset_attr_flip_flop(self):
        self.attr_flip_flop = False

    def write_attribute(self, value):
        """Handle the address/data flip-flop shared by port 3C0h."""
        value &= 0xFF
        if not self.attr_flip_flop:
            self.attr_index = value & 0x1F
            self.attr_flip_flop = True
            return
        index = self.attr_index
        if index < 16:
            self.attr_palette[index] = value & 0x3F
        elif index == 0x10:
            self.attr_mode_control = value
        elif index == 0x14:
            self.attr_color_select = value & 0x0F
        self.attr_flip_flop = False
        self.graphics_dirty = True

    def read_attribute(self):
        index = self.attr_index
        if index < 16:
            return self.attr_palette[index]
        if index == 0x10:
            return self.attr_mode_control
        if index == 0x14:
            return self.attr_color_select
        return 0

    def read_seq(self):
        return self.seq_regs[self.seq_index & 0x0F]

    def write_seq(self, value):
        if (self.seq_index & 0x0F) < len(self.seq_regs):
            self.seq_regs[self.seq_index & 0x0F] = value & 0xFF

    def read_gdc(self):
        return self.gdc_regs[self.gdc_index & 0x0F]

    def write_gdc(self, value):
        self.gdc_regs[self.gdc_index & 0x0F] = value & 0xFF

    def read_crtc(self):
        """Read the currently selected color CRTC register."""
        index = self.crtc_index & 0x1F
        if index in (0x0E, 0x0F):
            cursor = self.cur_y * self.width + self.cur_x
            self.crtc_registers[0x0E] = (cursor >> 8) & 0xFF
            self.crtc_registers[0x0F] = cursor & 0xFF
        return self.crtc_registers[index]

    def write_crtc(self, value):
        """Write the selected CRTC register and apply a visible cursor move."""
        index = self.crtc_index & 0x1F
        if index in (0x0E, 0x0F):
            cursor = self.cur_y * self.width + self.cur_x
            self.crtc_registers[0x0E] = (cursor >> 8) & 0xFF
            self.crtc_registers[0x0F] = cursor & 0xFF
        self.crtc_registers[index] = value & 0xFF
        if index in (0x0E, 0x0F):
            cursor = ((self.crtc_registers[0x0E] << 8)
                      | self.crtc_registers[0x0F])
            if cursor < self.width * self.height:
                self.cur_y, self.cur_x = divmod(cursor, self.width)

    def input_status_1(self):
        """Return deterministic VGA display-enable/retrace timing signals.

        Guest timing loops care about transitions rather than wall-clock
        accuracy.  Advancing on every read keeps those loops deterministic
        and allows fast native instruction batches to make progress.
        """
        self._status_reads = (self._status_reads + 1) % 8192
        phase = self._status_reads
        display_enable = 0x01 if phase % 64 < 48 else 0x00
        vertical_retrace = 0x08 if phase >= 7680 else 0x00
        return display_enable | vertical_retrace

    def attach_memory(self, mem):
        self.mem = mem
        self._sync_to_memory()

    def _cell_addr(self, x, y):
        return self.text_base + ((y * self.width + x) * 2)

    def _sync_to_memory(self):
        if self.mem is None:
            return
        for y, row in enumerate(self.buffer):
            for x, (ch, attr) in enumerate(row):
                addr = self._cell_addr(x, y)
                self.mem.write_byte(addr, ch)
                self.mem.write_byte(addr + 1, attr)

    def _sync_from_memory(self):
        if self.mem is None:
            return
        if self.graphics_mode:
            return
        for y in range(self.height):
            for x in range(self.width):
                addr = self._cell_addr(x, y)
                ch = self.mem.read_byte(addr)
                attr = self.mem.read_byte(addr + 1)
                self.buffer[y][x] = (ch, attr)

    def write(self, x, y, ch, attr=ATTR_NORMAL):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.buffer[y][x] = (ch, attr)
            if self.mem is not None:
                addr = self._cell_addr(x, y)
                self.mem.write_byte(addr, ch)
                self.mem.write_byte(addr + 1, attr)

    def putc(self, ch, attr=ATTR_NORMAL):
        if ch == 0x0A:
            self.cur_x = 0; self.cur_y += 1
        elif ch == 0x0D:
            self.cur_x = 0
        elif ch == 0x08:
            self.cur_x = max(0, self.cur_x - 1)
        elif 0x20 <= ch <= 0x7E:
            self.write(self.cur_x, self.cur_y, ch, attr)
            self.cur_x += 1
        elif ch == 0:
            return
        if self.cur_x >= self.width:
            self.cur_x = 0; self.cur_y += 1
        if self.cur_y >= self.height:
            self.scroll()

    def scroll(self):
        if self.on_scroll_line is not None:
            top = self.buffer[0]
            line = ''.join(decode_vga_char(ch)
                            for ch, _attr in top).rstrip()
            self.on_scroll_line(line)
        self.buffer = self.buffer[1:]
        self.buffer.append([(0x20, self.ATTR_NORMAL) for _ in range(self.width)])
        self.cur_y = self.height - 1
        # Mirror the scrolled buffer back into VRAM.  Without this, the next
        # display() / GTK _on_draw() call _sync_from_memory() would overwrite
        # self.buffer with the stale (un-scrolled) memory, losing the scroll.
        self._sync_to_memory()

    def print_str(self, s, attr=ATTR_NORMAL, x=-1, y=-1):
        if x >= 0 and y >= 0:
            self.cur_x = x; self.cur_y = y
        for ch in s:
            self.putc(ord(ch), attr)

    def clear(self, attr=ATTR_NORMAL):
        self.buffer = [[(0x20, attr) for _ in range(self.width)]
                       for _ in range(self.height)]
        self.cur_x = 0; self.cur_y = 0
        self._sync_to_memory()

    # ANSI foreground escapes for the 16 CGA colours (low nibble of attr).
    _FG = {
        0: "30",  1: "34",  2: "32",  3: "36",
        4: "31",  5: "35",  6: "33",  7: "37",
        8: "90",  9: "94", 10: "92", 11: "96",
       12: "91", 13: "95", 14: "93", 15: "97",
    }

    def _render_row(self, row, use_color):
        """Render one VGA row (80 cells) as a string.

        Consecutive cells sharing the same foreground colour are batched
        into a single ANSI escape, so output stays readable when redirected
        and is fast to emit on a terminal."""
        out = []
        cur_fg = None
        for ch, attr in row:
            fg = attr & 0xF
            c = decode_vga_char(ch)
            if use_color and fg != cur_fg:
                out.append(f"\033[{self._FG.get(fg, 37)}m")
                cur_fg = fg
            out.append(c)
        if use_color:
            out.append("\033[0m")
        return ''.join(out)

    def display(self):
        self._sync_from_memory()
        use_color = sys.stdout.isatty()
        # Clear screen + home cursor. Only meaningful on a real terminal;
        # skip when redirected so piped output has no stray escape codes.
        if use_color:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        pad = 2                              # spaces of padding each side
        inner = self.width + pad * 2        # content width between the bars
        top = "╔" + "═" * inner + "╗"
        div = "╠" + "═" * inner + "╣"
        bot = "╚" + "═" * inner + "╝"
        title = "Simple BIOS Emulator — VGA Text Mode (80x25)"
        gap = inner - len(title)
        title_row = "║" + (" " * (gap // 2)) + title + (" " * (gap - gap // 2)) + "║"
        print(top)
        print(title_row)
        print(div)
        for row in self.buffer:
            body = self._render_row(row, use_color)
            print(f"║  {body}  ║")
        print(bot)


class Serial:
    """Minimal 8250-compatible COM1 serial port (0x3F8-0x3FF)."""

    def __init__(self, echo=True):
        self.echo = echo
        self.rx_buffer = []
        self.output = []
        self.ier = 0
        self.divisor = 12       # 115200 / 12 = 9600 baud
        self.line_ctrl = 0x03  # 8N1 default
        self.mcr = 0
        self.msr = 0xB0        # DCD + DSR + CTS active
        self.iir = 0x01        # No interrupt pending
        self.lsr = 0x60        # THR empty + transmitter empty
        self.baud = 9600

    def inject_string(self, text):
        """Queue bytes that the guest can receive from COM1."""
        self.rx_buffer.extend(ord(ch) & 0xFF for ch in text)
        if self.rx_buffer:
            self.lsr |= 0x01

    def inb(self, offset):
        dlab = bool(self.line_ctrl & 0x80)
        if offset == 0x00:
            if dlab:           # Divisor latch low byte
                return self.divisor & 0xFF
            if not self.rx_buffer:
                return 0
            value = self.rx_buffer.pop(0)
            if not self.rx_buffer:
                self.lsr &= ~0x01
            return value
        if offset == 0x01:
            return (self.divisor >> 8) & 0xFF if dlab else self.ier
        if offset == 0x02:     # IIR (interrupt id)
            return self.iir
        if offset == 0x03:     # LCR (line control)
            return self.line_ctrl
        if offset == 0x04:     # MCR (modem control)
            return self.mcr
        if offset == 0x05:     # LSR (line status)
            return self.lsr
        if offset == 0x06:     # MSR (modem status)
            return self.msr
        return 0x00

    def outb(self, offset, val):
        val &= 0xFF
        dlab = bool(self.line_ctrl & 0x80)
        if offset == 0x00 and dlab:
            self.divisor = (self.divisor & 0xFF00) | val
            self._update_baud()
        elif offset == 0x01 and dlab:
            self.divisor = (self.divisor & 0x00FF) | (val << 8)
            self._update_baud()
        elif offset == 0x00:   # THR (transmit holding)
            self.output.append(val)
            if self.echo:
                if val >= 0x20:
                    sys.stderr.write(f"[COM1] {chr(val)}")
                elif val == 0x0A:
                    sys.stderr.write('\n')
                elif val == 0x0D:
                    sys.stderr.write('\r')
                sys.stderr.flush()
            self.lsr |= 0x20  # THRE set
        elif offset == 0x01:   # IER (interrupt enable)
            self.ier = val
        elif offset == 0x03:   # LCR (line control)
            self.line_ctrl = val
        elif offset == 0x04:   # MCR (modem control)
            self.mcr = val

    def _update_baud(self):
        if self.divisor:
            self.baud = 115200 // self.divisor


class IO:
    """I/O port emulation (keyboard, PIT, PIC, CMOS, serial, etc.)."""

    def __init__(self, video, keyboard, disk=None, serial=None,
                 pit=None, pic=None, cmos=None, kbd_ctrl=None):
        self.video = video
        self.kbd = keyboard
        self.disk = disk
        self.serial = serial
        self.pit = pit
        self.pic = pic
        self.cmos = cmos
        self.kbd_ctrl = kbd_ctrl  # Keyboard controller (8042)
        self._pit_pending_irqs = []  # IRQs fired since last check
        # Last byte written to port 0x61 (speaker/timer gates).  Bit 4 is
        # the refresh-check toggle: real hardware flips it on every DRAM
        # refresh cycle (~15 us), and legacy timing loops (DOS 5 IO.SYS
        # keyboard init, BIOS beep waits) poll it until it changes.
        self._port61 = 0x00
        # Ports not handled by a modeled device are recorded for diagnosing
        # legacy guests (notably SCP/WD1791 disk drivers).
        self.unhandled_ports = set()

    def inb(self, port):
        # SCP support-card timer/control registers.  Legacy SCP IOSYS polls
        # these during initialization; report an idle/ready value so it does
        # not spin forever when the optional hardware is absent.
        if port == 0xF4:
            return 0x00
        if port == 0xF5:
            return 0xFF
        if port == 0x60:  # Keyboard data port
            if self.kbd_ctrl:
                return self.kbd_ctrl.read_port_data()
            return self.kbd.read_key()
        if port == 0x61:  # PIT control / speaker
            # Toggle the refresh-check bit on each read so DRAM-refresh
            # timing loops observe a change (see __init__).
            self._port61 ^= 0x10
            return self._port61
        if port == 0x64:  # Keyboard controller status
            if self.kbd_ctrl:
                return self.kbd_ctrl.read_status()
            return 0x00 if self.kbd.key_pressed() else 0x01
        if port == 0x80:  # Diagnostic port
            return 0x00
        if port == 0x92:  # Soft config
            return 0x00

        # VGA color CRTC and Input Status Register 1.  Both CPU backends route
        # port instructions through this IO object.
        if port == 0x3D4:
            return self.video.crtc_index
        if port == 0x3D5:
            return self.video.read_crtc()
        if port == 0x3DA:
            self.video.reset_attr_flip_flop()
            return self.video.input_status_1()
        if port == 0x3C1:
            return self.video.read_attribute()
        if port == 0x3CC:
            return self.video.misc_output
        if port == 0x3C6:
            return self.video.dac_mask
        if port == 0x3C7:
            return self.video.dac_index
        if port == 0x3C4:
            return self.video.seq_index
        if port == 0x3C5:
            return self.video.read_seq()
        if port == 0x3CE:
            return self.video.gdc_index
        if port == 0x3CF:
            return self.video.read_gdc()
        if port == 0x3C2:
            return self.video.misc_output
        if port == 0x3C9:
            rgb = self.video.palette[self.video.dac_index]
            value = rgb[self.video.dac_component]
            self.video.dac_component = (self.video.dac_component + 1) % 3
            if self.video.dac_component == 0:
                self.video.dac_index = (self.video.dac_index + 1) & 0xFF
            return value >> 2

        # PIT counters (0x40-0x42)
        if self.pit and 0x40 <= port <= 0x42:
            return self.pit.read_counter(port - 0x40)

        # PIC master data (0x20) — read ISR/IRR
        if port == 0x20 and self.pic:
            return self.pic.ims | self.pic.irr
        # PIC slave data (0xA0)
        if port == 0xA0 and self.pic:
            return self.pic.slave_ims | self.pic.slave_irr

        # CMOS address (0x70) — read returns last address
        if port == 0x70 and self.cmos:
            return self.cmos._addr
        # CMOS data (0x71)
        if port == 0x71 and self.cmos:
            return self.cmos.read_data()

        if self.serial and 0x3F8 <= port <= 0x3FF:
            return self.serial.inb(port - 0x3F8)
        self.unhandled_ports.add(port & 0xFFFF)
        return 0x00

    def inw(self, port):
        lo = self.inb(port)
        hi = self.inb(port + 1)
        return lo | (hi << 8)

    def outb(self, port, val):
        if port in (0xF4, 0xF5):
            return
        if port == 0x61:  # PIT control / speaker
            self._port61 = (self._port61 & ~0x03) | (val & 0x03)
            return
        if port == 0x60:  # Keyboard data port
            if self.kbd_ctrl:
                self.kbd_ctrl.write_data(val)
            return
        if port == 0x64:  # Keyboard controller command port
            if self.kbd_ctrl:
                self.kbd_ctrl.write_command(val)
            return
        if port == 0x80:  # Diagnostic port
            pass

        if port == 0x3D4:
            self.video.crtc_index = val & 0x1F
            return
        if port == 0x3D5:
            self.video.write_crtc(val)
            return
        if port == 0x3C0:
            self.video.write_attribute(val)
            return
        if port == 0x3C4:
            if not self.video.graphics_mode:
                # Borland's EGAVGA BGI driver programs the adapter directly
                # instead of calling INT 10h/AH=00.  Its first sequencer/GDC
                # access is the mode switch in practice.
                self.video.set_mode(0x10)
            self.video.seq_index = val & 0x0F
            return
        if port == 0x3C5:
            self.video.write_seq(val)
            return
        if port == 0x3CE:
            if not self.video.graphics_mode:
                self.video.set_mode(0x10)
            self.video.gdc_index = val & 0x0F
            return
        if port == 0x3CF:
            self.video.write_gdc(val)
            return
        if port == 0x3C2:
            self.video.misc_output = val & 0xFF
            return
        if port == 0x3C6:
            self.video.dac_mask = val & 0xFF
            return
        if port == 0x3C7:
            self.video.dac_index = val & 0xFF
            self.video.dac_component = 0
            return
        if port == 0x3C8:
            self.video.dac_index = val & 0xFF
            self.video.dac_component = 0
            return
        if port == 0x3C9:
            index = self.video.dac_index
            rgb = list(self.video.palette[index])
            rgb[self.video.dac_component] = (val & 0x3F) << 2
            self.video.palette[index] = tuple(rgb)
            self.video.graphics_dirty = True
            self.video.dac_component = (self.video.dac_component + 1) % 3
            if self.video.dac_component == 0:
                self.video.dac_index = (index + 1) & 0xFF
            return

        # PIT counters (0x40-0x42)
        if self.pit and 0x40 <= port <= 0x42:
            self.pit.write_counter(port - 0x40, val)
            return
        # PIT command (0x43)
        if self.pit and port == 0x43:
            self.pit.write_command(val)
            return

        # PIC master (0x20-0x21)
        if self.pic and 0x20 <= port <= 0x21:
            self.pic.write_master(port, val)
            return
        # PIC slave (0xA0-0xA1)
        if self.pic and 0xA0 <= port <= 0xA1:
            self.pic.write_slave(port, val)
            return

        # CMOS address (0x70)
        if self.cmos and port == 0x70:
            self.cmos.write_addr(val)
            return
        # CMOS data (0x71)
        if self.cmos and port == 0x71:
            self.cmos.write_data(val)
            return

        if self.serial and 0x3F8 <= port <= 0x3FF:
            self.serial.outb(port - 0x3F8, val)
            return
        self.unhandled_ports.add(port & 0xFFFF)

    def outw(self, port, val):
        self.outb(port, val & 0xFF)
        self.outb(port + 1, (val >> 8) & 0xFF)

    def tick(self, dt=1/18.2):
        """Advance PIT by dt seconds. Returns list of fired IRQs."""
        if self.pit:
            fired = self.pit.tick(dt)
            routed = []
            for channel in fired:
                if channel != 0:
                    continue
                routed.append(channel)
                self._pit_pending_irqs.append(channel)
                if self.pic:
                    self.pic.raise_irq(channel)
            return routed
        return []

    def get_pending_irq(self):
        """Get highest priority pending IRQ, or -1 if none."""
        if self.pic:
            return self.pic.get_highest_irq()
        return -1

    def get_irq_vector(self, irq):
        """Get interrupt vector for given IRQ."""
        if self.pic:
            return self.pic.get_vector(irq)
        return irq + 8


class Keyboard:
    """Simple keyboard buffer."""

    def __init__(self):
        self.buffer = []

    def key_pressed(self):
        return len(self.buffer) > 0

    def read_key(self):
        return self.buffer.pop(0) if self.buffer else 0

    def feed_string(self, s):
        for ch in s:
            self.buffer.append(ord(ch))
        self.buffer.append(0x0D)


class Disk:
    """Simple disk image (array of 512-byte sectors)."""

    def __init__(self, num_sectors=2880, *, cylinders=None, heads=None,
                 sectors_per_track=None, hard_disk=False):
        self.sectors = [bytearray(512) for _ in range(num_sectors)]
        self.media_type = 0xF9  # Default: 1.44MB 3.5" floppy
        self.cylinders = cylinders
        self.heads = heads
        self.sectors_per_track = sectors_per_track
        self.hard_disk = hard_disk
        self.dirty = False          # set by write_sector; cleared by writeback
        self.read_only = False      # host-folder bridge sets this flag
        self.media_changed = False  # set by swap_disk for AH=16h reporting

    def read_sector(self, lba, buf):
        if not 0 <= lba < len(self.sectors):
            return False
        if len(buf) < 512:
            raise IndexError('sector buffer must contain at least 512 bytes')
        buf[:512] = self.sectors[lba]
        return True

    def write_sector(self, lba, buf):
        if self.read_only:
            return False
        if not 0 <= lba < len(self.sectors):
            return False
        if len(buf) < 512:
            raise IndexError('sector buffer must contain at least 512 bytes')
        self.sectors[lba][:] = buf[:512]
        self.dirty = True
        return True

    def write_boot_sector(self, code):
        """Write boot sector code (bytes) to LBA 0."""
        self.sectors[0][:len(code)] = code
        # Set boot signature
        self.sectors[0][510] = 0x55
        self.sectors[0][511] = 0xAA


class DiskView:
    """Bounded sector-offset view into an existing :class:`Disk`.

    Filesystem helpers can mount a partition through this view while reads,
    writes, and the dirty flag continue to belong to the parent disk.
    """

    def __init__(self, disk, start_sector, sector_count):
        if (start_sector < 0 or sector_count <= 0
                or start_sector + sector_count > len(disk.sectors)):
            raise ValueError("disk view exceeds parent disk bounds")
        self.disk = disk
        self.start_sector = start_sector
        self.sectors = disk.sectors[
            start_sector:start_sector + sector_count]

    def read_sector(self, lba, buf):
        if not 0 <= lba < len(self.sectors):
            return False
        return self.disk.read_sector(self.start_sector + lba, buf)

    def write_sector(self, lba, buf):
        if not 0 <= lba < len(self.sectors):
            return False
        return self.disk.write_sector(self.start_sector + lba, buf)
